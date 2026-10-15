#!/usr/bin/env python3
"""Private GitHub Issues -> existing Codex CLI session. No shell evaluation."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parent
LABELS = {"codex-send": "1d76db", "codex-dispatching": "fbca04",
          "codex-queued": "0e8a16", "codex-uncertain": "d93f0b", "codex-failed": "b60205"}
TERMINAL = set(LABELS) - {"codex-send"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Bridge is running or another listener owns this session') from None
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def config(root=ROOT):
    c = json.loads((root / 'config.json').read_text())
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', c['repo']):
        raise ValueError('Invalid repository')
    c['session'] = str(uuid.UUID(c['session']))
    if not isinstance(c['authorized_senders'], list) or not c['authorized_senders'] or any(not isinstance(s, str) or not s for s in c['authorized_senders']):
        raise ValueError('Explicit authorized_senders required')
    if type(c['poll_seconds']) is not int or not 5 <= c['poll_seconds'] <= 3600:
        raise ValueError('poll_seconds must be 5..3600')
    return c


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self.base = 'repos/' + repo

    def api(self, path, method='GET', payload=None, pages=False):
        args = ['gh', 'api', '--hostname', 'github.com', self.base + path, '--method', method]
        if pages:
            args += ['--paginate', '--slurp']
        if payload is not None:
            args += ['--input', '-']
        try:
            p = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError('GitHub unavailable; credentials/network may need attention') from None
        if p.returncode:
            # Never log raw stderr, payloads or credential-bearing diagnostics.
            raise RuntimeError(f'GitHub request failed (exit {p.returncode}); check gh auth status/network')
        return json.loads(p.stdout) if p.stdout.strip() else None

    def private(self):
        r = self.api('')
        if r.get('private') is not True or r.get('full_name', '').lower() != self.repo.lower():
            raise RuntimeError('Refusing relay: repository must match configuration and be private')

    def pending(self):
        pages = self.api('/issues?state=open&labels=codex-send&per_page=100&sort=created&direction=asc', pages=True)
        return [issue for page in pages for issue in page]

    def issue(self, number):
        return self.api(f'/issues/{number}')

    def approver(self, number):
        pages = self.api(f'/issues/{number}/events?per_page=100', pages=True)
        events = [e for page in pages for e in page
                  if e.get('event') in ('labeled', 'unlabeled') and e.get('label', {}).get('name') == 'codex-send']
        if not events or events[-1]['event'] != 'labeled':
            return None
        return (events[-1].get('actor') or {}).get('login', '').lower()

    def label(self, number, label):
        # Additive; never replace unrelated labels or remove an approval automatically.
        self.api(f'/issues/{number}/labels', 'POST', {'labels': [label]})

    def comment(self, number, body):
        self.api(f'/issues/{number}/comments', 'POST', {'body': body})


class Bridge:
    def __init__(self, c, root=ROOT, github=None):
        self.c, self.root = c, root
        self.runtime = root / '.runtime'
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.runtime / 'receipts.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(self.state, dict):
            raise ValueError('Invalid receipt store; refusing delivery')
        self.gh = github or GitHub(c['repo'])
        self.allowed = {s.lower() for s in c['authorized_senders']}
        for receipt in self.state.values():
            if receipt['status'] == 'dispatching':
                receipt.update(status='uncertain', reason='Interrupted submission; investigate manually', updated=now(), synced=False)
        self.save()

    def save(self):
        atomic_json(self.path, self.state)

    def log(self, text):
        line = now() + ' ' + text
        print(line, flush=True)
        with (self.runtime / 'activity.log').open('a') as out:
            out.write(line + '\n')

    def eligible(self, issue):
        labels = {x['name'] for x in issue.get('labels', [])}
        return (issue.get('state') == 'open' and 'pull_request' not in issue
                and issue.get('repository_url', '').lower() == 'https://api.github.com/repos/' + self.c['repo'].lower()
                and (issue.get('user') or {}).get('login', '').lower() in self.allowed
                and 'codex-send' in labels and not labels.intersection(TERMINAL)
                and isinstance(issue.get('body'), str) and bool(issue['body'].strip()))

    def sync(self, key, receipt):
        if receipt.get('synced') or receipt['status'] == 'dispatching':
            return
        label = {'queued': 'codex-queued', 'failed': 'codex-failed', 'uncertain': 'codex-uncertain'}[receipt['status']]
        # Idempotent label update only. A receipt never causes another queue submission.
        self.gh.label(receipt['issue'], label)
        receipt['synced'] = True
        self.save()

    def message(self, issue):
        return (f"GitHub relay: {self.c['repo']}#{issue['number']}\n"
                f"Title: {issue['title']}\n\n"
                'Delivery authorization only. Follow your normal execution permissions and approval requirements.\n'
                'Do not automatically approve privileged actions, merge, deploy, or incur paid API workloads.\n\n'
                '--- Complete task instructions ---\n' + issue['body'] + '\n--- End task instructions ---\n\n'
                'Completion convention (only if permitted by the task): post a concise result to this originating issue '
                'using gh issue comment with --repo and --body-file. Include outcome (completed/blocked/failed), '
                'summary, changed files, tests/results, and remaining decisions or blockers. '
                'Queueing alone is not completion. If external actions are prohibited, respond in this session only.')

    @staticmethod
    def queue_id(output):
        # CLI currently prints a UUID; retain only the identifier, never arbitrary output.
        match = re.search(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b', output)
        return match.group(0) if match else None

    def dispatch(self, issue):
        n = issue['number']
        key = self.c['repo'].lower() + '#' + str(n)
        if key in self.state:
            return
        # Fetch full current body; list responses and comments are not executable commands.
        issue = self.gh.issue(n)
        if not self.eligible(issue) or self.gh.approver(n) not in self.allowed:
            return
        # Re-read after authorization query to catch revocations/edits during inspection.
        fresh = self.gh.issue(n)
        if fresh != issue:
            return  # reconsider on a later poll
        receipt = dict(issue=n, repo=self.c['repo'], session=self.c['session'], status='dispatching',
                       updated=now(), body_sha256=hashlib.sha256(issue['body'].encode()).hexdigest(), synced=False)
        self.state[key] = receipt
        self.save()  # must be durable BEFORE attempting submission
        try:
            self.gh.label(n, 'codex-dispatching')
        except RuntimeError:
            receipt.update(status='failed', reason='Could not mark dispatching; queue not invoked', updated=now())
            self.save()
            raise
        self.log(f'issue #{n} dispatching to {self.c["session"]}')
        try:
            result = subprocess.run(['codex', 'queue', '--thread', self.c['session'], '--message', self.message(issue)],
                                    capture_output=True, text=True, timeout=45)
        except FileNotFoundError:
            receipt.update(status='failed', reason='Codex executable missing; queue not invoked')
        except (subprocess.TimeoutExpired, OSError):
            receipt.update(status='uncertain', reason='Submission timed out/interrupted; no automatic retry')
        else:
            message_id = self.queue_id(result.stdout)
            receipt['exit_code'] = result.returncode
            if result.returncode == 0 and message_id:
                receipt.update(status='queued', message_id=message_id)
            elif result.returncode > 0 and not message_id:
                receipt.update(status='failed', reason='Queue returned nonzero; no automatic retry')
            else:
                receipt.update(status='uncertain', reason='Unrecognized or contradictory queue response; investigate manually')
                if message_id:
                    receipt['message_id'] = message_id
        receipt['updated'] = now()
        self.save()
        self.log(f'issue #{n} {receipt["status"]}' + (f' message {receipt["message_id"]}' if 'message_id' in receipt else ''))
        self.sync(key, receipt)

    def poll(self, stopping=lambda: False):
        self.gh.private()  # fail closed on missing access or changed visibility
        for key, receipt in self.state.items():
            if stopping():
                return
            if receipt['repo'].lower() == self.c['repo'].lower():
                self.sync(key, receipt)
        for issue in self.gh.pending():
            if stopping():
                return
            if self.eligible(issue):
                self.dispatch(issue)


def session_lock(session):
    # Shared across installations for this macOS user; files are never unlinked.
    return Path('/tmp') / f'codex-bridge-{os.getuid()}' / (session + '.lock')


def set_session(root, session):
    session = str(uuid.UUID(session))
    with lock(root / '.runtime' / 'listener.lock'):
        with lock(session_lock(session)):
            c = config(root)
            c['session'] = session
            atomic_json(root / 'config.json', c)
    return session


def active(root):
    try:
        with lock(root / '.runtime' / 'listener.lock'):
            return False
    except RuntimeError:
        return True


def start(root, once=False):
    with lock(root / '.runtime' / 'listener.lock'):
        c = config(root)
        with lock(session_lock(c['session'])):
            bridge = Bridge(c, root)
            stop = threading.Event()
            def request_stop(_signum, _frame):
                stop.set()  # let a queue call finish and persist its receipt
            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            # stop uses a file, avoiding PID reuse races. Stale requests removed under lock.
            stop_file = bridge.runtime / 'stop-request'
            stop_file.unlink(missing_ok=True)
            atomic_json(bridge.runtime / 'running.json', dict(pid=os.getpid(), session=c['session'], started=now()))
            bridge.log(f'listening on {c["repo"]}, session {c["session"]}; Ctrl-C to stop')
            try:
                while not stop.is_set() and not stop_file.exists():
                    try:
                        bridge.poll(lambda: stop.is_set() or stop_file.exists())
                    except RuntimeError as exc:
                        bridge.log(str(exc))
                        if once:
                            return 1
                    if once:
                        break
                    # Check file once per second; no background service required.
                    for _ in range(c['poll_seconds']):
                        if stop.wait(1) or stop_file.exists():
                            break
            finally:
                (bridge.runtime / 'running.json').unlink(missing_ok=True)
                bridge.log('stopped')
    return 0


def setup_relay(c):
    # Existing repo only: never fall back to a public or application repository.
    gh = GitHub(c['repo'])
    gh.private()
    for name, color in LABELS.items():
        existing = gh.api('/labels?per_page=100', pages=True)
        if not any(label['name'] == name for page in existing for label in page):
            gh.api('/labels', 'POST', {'name': name, 'color': color})
    print('Private relay and labels ready: https://github.com/' + c['repo'])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('start', 'once', 'stop', 'status', 'activity', 'setup-relay'):
        sub.add_parser(name)
    sub.add_parser('set-session').add_argument('session')
    args = parser.parse_args()
    runtime = ROOT / '.runtime'
    if args.command in ('start', 'once'):
        return start(ROOT, args.command == 'once')
    if args.command == 'set-session':
        print('Target:', set_session(ROOT, args.session))
    elif args.command == 'setup-relay':
        setup_relay(config())
    elif args.command == 'status':
        print('Running' if active(ROOT) else 'Stopped')
        print('Configured target:', config()['session'])
        if active(ROOT) and (runtime / 'running.json').exists():
            print((runtime / 'running.json').read_text())
    elif args.command == 'stop':
        if active(ROOT):
            (runtime / 'stop-request').touch(mode=0o600)
            print('Stop requested; current submission will finish before exit')
        else:
            print('Already stopped')
    elif args.command == 'activity':
        log = runtime / 'activity.log'
        print('\n'.join(log.read_text().splitlines()[-40:]) if log.exists() else 'No activity yet')
        receipts = runtime / 'receipts.json'
        if receipts.exists():
            print(receipts.read_text())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        # No subprocess output is included in exceptions.
        print(f'Bridge stopped: {exc}', file=sys.stderr)
        sys.exit(1)
