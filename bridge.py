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
import service

ROOT = Path(__file__).resolve().parent
LABELS = {"codex-send": "1d76db", "codex-dispatching": "fbca04",
          "codex-queued": "0e8a16", "codex-uncertain": "d93f0b", "codex-failed": "b60205"}
TERMINAL = set(LABELS) - {"codex-send"}
LABELS.update({'codex-pair': '5319e7', 'codex-paired': '0e8a16'})


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
            raise RuntimeError('Bridge lock busy; another listener or pairing operation owns this resource') from None
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def config(root=ROOT):
    c = json.loads((root / 'config.json').read_text())
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', c['repo']):
        raise ValueError('Invalid repository')
    if 'session' in c:  # preserved legacy setting; never used as a routing default
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

    def pending(self, label='codex-send'):
        pages = self.api(f'/issues?state=open&labels={label}&per_page=100&sort=created&direction=asc', pages=True)
        return [issue for page in pages for issue in page]

    def issue(self, number):
        return self.api(f'/issues/{number}')

    def approver(self, number, label='codex-send'):
        pages = self.api(f'/issues/{number}/events?per_page=100', pages=True)
        events = [e for page in pages for e in page
                  if e.get('event') in ('labeled', 'unlabeled') and e.get('label', {}).get('name') == label]
        if not events or events[-1]['event'] != 'labeled':
            return None
        return (events[-1].get('actor') or {}).get('login', '').lower()

    def label(self, number, label):
        # Additive; never replace unrelated labels or remove an approval automatically.
        self.api(f'/issues/{number}/labels', 'POST', {'labels': [label]})

    def comment(self, number, body):
        self.api(f'/issues/{number}/comments', 'POST', {'body': body})


def json_object(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def route_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', value):
        raise ValueError('Route must be 1..48 lowercase letters, digits or hyphens')
    return value


def task_envelope(body):
    data = json_object(body)
    route_name(data['route'])
    data['delivery_id'] = str(uuid.UUID(data['delivery_id']))
    if not isinstance(data['instructions'], str) or not data['instructions'].strip():
        raise ValueError('Full instructions required')
    return data


def confirmed_session(explicit=None, environ=None):
    env = os.environ if environ is None else environ
    inherited = env.get('CODEX_THREAD_ID')
    if explicit:
        # Explicit UUID is operator confirmation from /status, including nested sessions.
        return str(uuid.UUID(explicit)), 'manual UUID confirmation'
    if not inherited:
        raise ValueError('No CODEX_THREAD_ID. Read /status in the intended Codex session, then pair CODE --session UUID')
    return str(uuid.UUID(inherited)), 'Codex shell-tool context'


class Routes:
    """Small, locked local route registry. Never infer terminal identity."""
    def __init__(self, root):
        self.path = root / '.runtime' / 'routes.json'
        self.lock_path = root / '.runtime' / 'routes.lock'
        self.data = json_object(self.path.read_text()) if self.path.exists() else {'routes': {}, 'pairs': {}}
        if set(self.data) != {'routes', 'pairs'} or not all(isinstance(v, dict) for v in self.data.values()):
            raise ValueError('Invalid route registry; refusing delivery')
        for name, record in self.data['routes'].items():
            route_name(name)
            uuid.UUID(record['session'])
            if record['status'] not in ('paired', 'stale', 'uncertain') or not record.get('repo'):
                raise ValueError('Invalid route record')

    def save(self):
        atomic_json(self.path, self.data)

    def destination(self, route, repo):
        record = self.data['routes'].get(route)
        if not record or record['repo'].lower() != repo.lower():
            raise ValueError('Unknown/unbound route; create a pairing request and confirm inside the intended session')
        if record['status'] != 'paired':
            raise ValueError('Route ' + record['status'] + '; reconcile prior deliveries and explicitly re-pair')
        return record['session']

    def hold(self, route, reason, status='stale'):
        self.data['routes'][route].update(status=status, reason=reason, updated=now())
        self.save()

    def sync(self, gh):
        for record in self.data['pairs'].values():
            if record['status'] == 'paired' and not record.get('synced') and record['repo'].lower() == gh.repo.lower():
                # No session UUID, code or local path is posted. Duplicate comments after
                # an interrupted HTTP response are harmless; they never dispatch tasks.
                gh.comment(record['issue'], f"Bridge pairing confirmed: route `{record['route']}`. "
                           'Future approved task envelopes must explicitly name this route. '
                           'After /clear, confirm a new pairing request in the new session. '
                           'This is a user-selected association, not verification of a ChatGPT conversation identity.')
                record['synced'] = True
                self.save()


def pair(root, code, explicit=None, github=None, environ=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', code):
        raise ValueError('Pairing code must contain 16..64 letters, digits, underscores or hyphens')
    session, method = confirmed_session(explicit, environ)
    c = config(root)
    gh = github or GitHub(c['repo'])
    allowed = {s.lower() for s in c['authorized_senders']}
    gh.private()
    matches = []
    for item in gh.pending('codex-pair'):
        try:
            body = json_object(item.get('body', ''))
        except (ValueError, TypeError):
            continue
        if body.get('code') == code:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError('Pairing code missing or ambiguous; create one new owner-authored request')
    issue = gh.issue(matches[0]['number'])
    body = json_object(issue['body'])
    labels = {x['name'] for x in issue.get('labels', [])}
    if (body.get('code') != code or issue.get('state') != 'open' or 'pull_request' in issue
            or issue.get('repository_url', '').lower() != 'https://api.github.com/repos/' + c['repo'].lower()
            or issue.get('user', {}).get('login', '').lower() not in allowed
            or 'codex-pair' not in labels or labels.intersection({'codex-send', 'codex-paired'})
            or gh.approver(issue['number'], 'codex-pair') not in allowed):
        raise ValueError('Pairing request unauthorized, already used, or marked as executable')
    route = route_name(body['route'])
    created = datetime.datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00'))
    expiry = datetime.datetime.fromisoformat(body['expires_at'].replace('Z', '+00:00'))
    current = datetime.datetime.now(datetime.timezone.utc)
    if (created.tzinfo is None or expiry.tzinfo is None or created > current
            or not created < expiry <= created + datetime.timedelta(minutes=15) or current >= expiry):
        raise ValueError('Pairing expired or invalid; expiry must be within 15 minutes of issue creation')
    if gh.issue(issue['number']) != issue:
        raise ValueError('Pairing request changed during verification; try again')
    with lock(root / '.runtime' / 'routes.lock'):
        routes = Routes(root)
        digest = hashlib.sha256(code.encode()).hexdigest()
        key = c['repo'].lower() + '#' + str(issue['number'])
        if key in routes.data['pairs'] or any(r['code_hash'] == digest for r in routes.data['pairs'].values()):
            raise ValueError('Pairing code already consumed; create a new request')
        record = dict(issue=issue['number'], repo=c['repo'], route=route, code_hash=digest,
                      status='claiming', updated=now(), synced=False)
        routes.data['pairs'][key] = record
        routes.save()  # consume before the network claim; interrupted claims fail closed
        try:
            gh.label(issue['number'], 'codex-paired')
        except RuntimeError:
            record.update(status='uncertain', updated=now())
            routes.save()
            raise RuntimeError('Pairing claim uncertain; route unchanged. Use a new request, never reuse this code') from None
        routes.data['routes'][route] = dict(session=session, repo=c['repo'], status='paired',
                                           paired_at=now(), issue=issue['number'], method=method)
        record.update(status='paired', updated=now())
        routes.save()
        print(f'Paired {route} -> {session} ({method}). Receipt by the destination is still unverified.')
        try:
            routes.sync(gh)
        except RuntimeError:
            print('GitHub confirmation pending; listener will retry confirmation without re-pairing')
    return session


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
        self.blocked = {}
        for receipt in self.state.values():
            if receipt['status'] == 'dispatching':
                receipt.update(status='uncertain', reason='Interrupted submission; investigate manually', updated=now(), synced=False)
                if receipt.get('route'):
                    with lock(self.runtime / 'routes.lock'):
                        routes = Routes(root)
                        record = routes.data['routes'].get(receipt['route'])
                        if record and record['session'] == receipt['session']:
                            routes.hold(receipt['route'], 'Interrupted submission; reconcile and explicitly re-pair', 'uncertain')
        self.save()

    def save(self):
        atomic_json(self.path, self.state)

    def log(self, text):
        line = now() + ' ' + text
        print(line, flush=True)
        path = self.runtime / 'activity.log'
        if path.exists() and path.stat().st_size >= 1_000_000:
            os.replace(path, self.runtime / 'activity.log.1')
        with path.open('a') as out:
            out.write(line + '\n')

    def eligible(self, issue):
        labels = {x['name'] for x in issue.get('labels', [])}
        return (issue.get('state') == 'open' and 'pull_request' not in issue
                and issue.get('repository_url', '').lower() == 'https://api.github.com/repos/' + self.c['repo'].lower()
                and (issue.get('user') or {}).get('login', '').lower() in self.allowed
                and 'codex-send' in labels and not labels.intersection(TERMINAL)
                and 'codex-pair' not in labels and 'codex-paired' not in labels
                and isinstance(issue.get('body'), str) and bool(issue['body'].strip()))

    def sync(self, key, receipt):
        if receipt.get('synced') or receipt['status'] == 'dispatching':
            return
        label = {'queued': 'codex-queued', 'failed': 'codex-failed', 'uncertain': 'codex-uncertain'}[receipt['status']]
        # Idempotent label update only. A receipt never causes another queue submission.
        self.gh.label(receipt['issue'], label)
        receipt['synced'] = True
        self.save()

    def message(self, issue, envelope):
        return (f"GitHub relay: {self.c['repo']}#{issue['number']}\n"
                f"Title: {issue['title']}\n\n"
                f"Route: {envelope['route']}\nDelivery ID: {envelope['delivery_id']}\n\n"
                'Delivery authorization only. Follow your normal execution permissions and approval requirements.\n'
                'Do not automatically approve privileged actions, merge, deploy, or incur paid API workloads.\n\n'
                '--- Complete task instructions ---\n' + envelope['instructions'] + '\n--- End task instructions ---\n\n'
                'Completion convention (only if permitted by the task): post a concise result to this originating issue '
                'using gh issue comment with --repo and --body-file. Include outcome (completed/blocked/failed), '
                'summary, changed files, tests/results, and remaining decisions or blockers. '
                'Queueing alone is not completion. If external actions are prohibited, respond in this session only.')

    @staticmethod
    def queue_id(output):
        # CLI currently prints a UUID; retain only the identifier, never arbitrary output.
        match = re.search(r'\bQueued message\s+([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b', output)
        return match.group(1) if match else None

    def dispatch(self, issue):
        key = self.c['repo'].lower() + '#' + str(issue['number'])
        if key in self.state:
            return
        try:
            envelope = task_envelope(issue['body'])
            with lock(self.runtime / 'routes.lock'):
                routes = Routes(self.root)
                session = routes.destination(envelope['route'], self.c['repo'])
                with lock(session_lock(session)):
                    self._dispatch(issue, envelope, session, routes)
        except (ValueError, KeyError, TypeError):
            self.blocked[key] = 'Invalid/missing task envelope, unknown/unbound or held route; inspect routes and explicitly re-pair'

    def _dispatch(self, issue, envelope, session, routes):
        n = issue['number']
        key = self.c['repo'].lower() + '#' + str(n)
        if key in self.state:
            return
        # Fetch full current body; list responses and comments are not executable commands.
        issue = self.gh.issue(n)
        if not self.eligible(issue) or self.gh.approver(n) not in self.allowed:
            self.blocked[key] = 'Authorization refused; owner author and latest codex-send label actor required'
            return
        if task_envelope(issue['body']) != envelope:
            return
        if any(r.get('delivery_id') == envelope['delivery_id'] and r['repo'].lower() == self.c['repo'].lower()
               for r in self.state.values()):
            self.blocked[key] = 'Delivery ID already attempted; reconcile the original issue, never resend'
            return
        # Re-read after authorization query to catch revocations/edits during inspection.
        fresh = self.gh.issue(n)
        if fresh != issue:
            return  # reconsider on a later poll
        receipt = dict(issue=n, repo=self.c['repo'], session=session, status='dispatching',
                       route=envelope['route'], delivery_id=envelope['delivery_id'],
                       updated=now(), body_sha256=hashlib.sha256(issue['body'].encode()).hexdigest(), synced=False)
        self.state[key] = receipt
        self.save()  # must be durable BEFORE attempting submission
        try:
            self.gh.label(n, 'codex-dispatching')
        except RuntimeError:
            receipt.update(status='failed', reason='Could not mark dispatching; queue not invoked', updated=now())
            self.save()
            raise
        self.log(f'issue #{n} dispatching via {envelope["route"]} to {session}')
        try:
            result = subprocess.run(['codex', 'queue', '--thread', session, '--message', self.message(issue, envelope)],
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
                if 'no rollout found' in (result.stderr + result.stdout).lower():
                    receipt['reason'] = 'No rollout found; explicitly re-pair, then reconcile this issue before authorizing any new delivery'
            else:
                receipt.update(status='uncertain', reason='Unrecognized or contradictory queue response; investigate manually')
                if message_id:
                    receipt['message_id'] = message_id
        receipt['updated'] = now()
        self.save()
        if receipt['status'] in ('failed', 'uncertain'):
            routes.hold(envelope['route'], 'Queue failed/uncertain; re-pair and reconcile the attempted issue',
                        'uncertain' if receipt['status'] == 'uncertain' else 'stale')
        self.log(f'issue #{n} {receipt["status"]}' + (f' message {receipt["message_id"]}' if 'message_id' in receipt else ''))
        self.sync(key, receipt)

    def poll(self, stopping=lambda: False):
        self.blocked = {}
        self.gh.private()  # fail closed on missing access or changed visibility
        with lock(self.runtime / 'routes.lock'):
            Routes(self.root).sync(self.gh)
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
        atomic_json(self.runtime / 'health.json', dict(checked=now(), relay='reachable', blocked=self.blocked))


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
        bridge = Bridge(c, root)
        stop = threading.Event()
        def request_stop(_signum, _frame):
            stop.set()
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        stop_file = bridge.runtime / 'stop-request'
        stop_file.unlink(missing_ok=True)
        atomic_json(bridge.runtime / 'running.json', dict(pid=os.getpid(), started=now()))
        bridge.log(f'listening on {c["repo"]}; explicit named routes only')
        try:
            while not stop.is_set() and not stop_file.exists():
                try:
                    bridge.poll(lambda: stop.is_set() or stop_file.exists())
                except RuntimeError as exc:
                    bridge.log(str(exc))
                    atomic_json(bridge.runtime / 'health.json',
                                dict(checked=now(), relay='unavailable or local lock busy', error=str(exc)))
                    if once:
                        return 1
                if once:
                    break
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


def status(root):
    print('Listener:', 'running' if active(root) else 'stopped')
    print('LaunchAgent:', 'loaded' if service.loaded() else 'not loaded')
    c = config(root)
    try:
        GitHub(c['repo']).private()
        print('GitHub relay: reachable and private')
    except RuntimeError as exc:
        print('GitHub relay: unavailable:', exc)
    show_routes(root)
    runtime = root / '.runtime'
    if (runtime / 'health.json').exists():
        print('Last poll (timestamp matters):', (runtime / 'health.json').read_text())
    if (runtime / 'receipts.json').exists():
        receipts = json_object((runtime / 'receipts.json').read_text())
        counts = {}
        for record in receipts.values():
            counts[record['status']] = counts.get(record['status'], 0) + 1
        print('Delivery receipts:', json.dumps(counts))
        print('Queued means accepted by Codex; destination receipt/execution/completion unverified.')
        for key, record in receipts.items():
            if record['status'] in ('failed', 'uncertain', 'dispatching'):
                print(key, json.dumps(record))


def show_routes(root):
    routes = Routes(root)
    if not routes.data['routes']:
        print('Routes: none; pair from inside the intended Codex session')
    for name, record in routes.data['routes'].items():
        print(f"Route {name}: {record['status']}, session {record['session']}; "
              'live terminal availability unverified. Explicitly re-pair after /clear.')
    held = [r for r in routes.data['pairs'].values() if r['status'] != 'paired']
    if held:
        print('Interrupted/uncertain pairing claims:', len(held), '; use new pairing requests')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('install', 'uninstall', 'start', 'restart', 'listen', 'once', 'stop',
                 'status', 'activity', 'logs', 'routes', 'setup-relay'):
        sub.add_parser(name)
    sub.add_parser('set-session').add_argument('session')
    sub.add_parser('unpair').add_argument('route')
    pairing = sub.add_parser('pair')
    pairing.add_argument('code')
    pairing.add_argument('--session', help='Explicit UUID confirmed using /status in the intended terminal')
    args = parser.parse_args()
    runtime = ROOT / '.runtime'
    if args.command in ('listen', 'once'):
        return start(ROOT, args.command == 'once')
    if args.command == 'pair':
        pair(ROOT, args.code, args.session)
    elif args.command == 'unpair':
        route_name(args.route)
        with lock(runtime / 'routes.lock'):
            routes = Routes(ROOT)
            if args.route not in routes.data['routes']:
                raise ValueError('Unknown route')
            routes.hold(args.route, 'Operator invalidated route; explicitly re-pair')
        print('Route held. Reconcile prior deliveries before re-pairing; no task will be resent.')
    elif args.command == 'set-session':
        print('Legacy setting preserved:', set_session(ROOT, args.session))
        print('New tasks require explicit paired routes; this setting is not a default route.')
    elif args.command == 'setup-relay':
        setup_relay(config())
    elif args.command == 'install':
        if active(ROOT):
            raise RuntimeError('Stop the existing foreground listener before installation')
        setup_relay(config())
        print('Installed:', service.install(ROOT))
    elif args.command in ('start', 'restart'):
        if args.command == 'restart':
            service.stop(ROOT)
        elif active(ROOT) and not service.loaded():
            raise RuntimeError('Foreground listener active; stop it before starting the service')
        service.start()
        print('LaunchAgent enabled and started; use status to inspect relay and route health')
    elif args.command == 'stop':
        service.stop(ROOT)
        print('Stop requested; LaunchAgent disabled until explicit start, including across logins')
    elif args.command == 'uninstall':
        service.uninstall(ROOT)
    elif args.command == 'status':
        status(ROOT)
    elif args.command == 'routes':
        show_routes(ROOT)
    elif args.command in ('activity', 'logs'):
        path = runtime / 'activity.log'
        print('\n'.join(path.read_text().splitlines()[-80:]) if path.exists() else 'No activity yet')
        if args.command == 'activity' and (runtime / 'receipts.json').exists():
            print((runtime / 'receipts.json').read_text())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # No raw subprocess output, task text, or credentials in diagnostics.
        detail = str(exc) if isinstance(exc, (RuntimeError, ValueError, KeyError, OSError)) else type(exc).__name__
        print(f'Bridge stopped: {detail}', file=sys.stderr)
        try:
            runtime = ROOT / '.runtime'
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = runtime / 'activity.log'
            if path.exists() and path.stat().st_size >= 1_000_000:
                os.replace(path, runtime / 'activity.log.1')
            with path.open('a') as out:
                out.write(now() + ' command failed: ' + detail[:1000] + '\n')
        except OSError:
            pass
        sys.exit(1)
