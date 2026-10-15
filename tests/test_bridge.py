import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge

SESSION = '01a14055-7b04-7483-9c0a-e01f79e7f738'
MESSAGE = '01a140e2-9b71-74b3-9018-01e4a57de06e'
OTHER = '01a14055-7b04-7483-9c0a-e01f79e7f739'
CONFIG = dict(repo='norman8823/codex-bridge', session=SESSION,
              authorized_senders=['norman8823'], poll_seconds=15)


def issue():
    return dict(number=7, title='Example', body='Full instructions: $(touch /tmp/never)\n`false`',
                state='open', user={'login': 'norman8823'},
                repository_url='https://api.github.com/repos/norman8823/codex-bridge',
                labels=[{'name': 'codex-send'}])


class FakeGitHub:
    def __init__(self):
        self.item = issue()
        self.actor = 'norman8823'
        self.labels = []
        self.offline = False
        self.fail_sync = False
        self.private_ok = True

    def private(self):
        if self.offline or not self.private_ok:
            raise RuntimeError('GitHub unavailable or public')

    def pending(self):
        return [copy.deepcopy(self.item)]

    def issue(self, n):
        return copy.deepcopy(self.item)

    def approver(self, n):
        return self.actor

    def label(self, n, label):
        if self.fail_sync and label == 'codex-queued':
            raise RuntimeError('GitHub unavailable')
        self.labels.append((n, label))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gh = FakeGitHub()
        self.b = bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh)
        self.runner = patch('bridge.subprocess.run').start()
        self.addCleanup(patch.stopall)
        self.runner.return_value = subprocess.CompletedProcess([], 0, f'Queued message {MESSAGE}\n', '')

    def receipt(self):
        return next(iter(self.b.state.values()))

    def test_valid_correct_session_complete_body_no_shell(self):
        self.b.poll()
        args = self.runner.call_args.args[0]
        self.assertEqual(args[:5], ['codex', 'queue', '--thread', SESSION, '--message'])
        self.assertIn(self.gh.item['body'], args[5])
        self.assertNotIn('shell', self.runner.call_args.kwargs)
        self.assertEqual(self.receipt()['message_id'], MESSAGE)
        self.assertEqual(self.receipt()['exit_code'], 0)
        self.assertIn((7, 'codex-queued'), self.gh.labels)

    def test_missing_label(self):
        self.gh.item['labels'] = []
        self.b.poll()
        self.runner.assert_not_called()

    def test_unauthorized_author(self):
        self.gh.item['user']['login'] = 'attacker'
        self.b.poll()
        self.runner.assert_not_called()

    def test_unauthorized_label_actor(self):
        self.gh.actor = 'attacker'
        self.b.poll()
        self.runner.assert_not_called()

    def test_wrong_repo_closed_pr_and_remote_markers(self):
        for changes in [dict(repository_url='https://api.github.com/repos/someone/else'),
                        dict(state='closed'), dict(pull_request={}),
                        dict(labels=[{'name': 'codex-send'}, {'name': 'codex-queued'}]),
                        dict(labels=[{'name': 'codex-send'}, {'name': 'codex-uncertain'}]),
                        dict(labels=[{'name': 'codex-send'}, {'name': 'codex-dispatching'}])]:
            self.gh.item = issue() | changes
            self.b.poll()
        self.runner.assert_not_called()

    def test_already_delivered(self):
        self.b.poll()
        self.b.poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_restart(self):
        self.b.poll()
        bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_queue_failure(self):
        self.runner.return_value = subprocess.CompletedProcess([], 1, '', 'session unavailable SECRET')
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'failed')
        self.assertEqual(self.receipt()['exit_code'], 1)
        self.b.poll()
        self.assertEqual(self.runner.call_count, 1)
        self.assertNotIn('SECRET', (self.root / '.runtime/receipts.json').read_text())

    def test_timeout_is_uncertain_and_never_retried(self):
        self.runner.side_effect = subprocess.TimeoutExpired(['codex'], 45)
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'uncertain')
        bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_crash_after_submission_intent(self):
        self.runner.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.b.poll()
        b2 = bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh)
        b2.poll()
        self.assertEqual(next(iter(b2.state.values()))['status'], 'uncertain')
        self.assertEqual(self.runner.call_count, 1)

    def test_missing_session_never_creates_agent(self):
        self.runner.return_value = subprocess.CompletedProcess([], 1, '', 'No session')
        self.b.poll()
        self.b.poll()
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.runner.call_args.args[0][:4], ['codex', 'queue', '--thread', SESSION])

    def test_github_unavailable_then_recovers(self):
        self.gh.offline = True
        with self.assertRaises(RuntimeError):
            self.b.poll()
        self.runner.assert_not_called()
        self.gh.offline = False
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'queued')

    def test_public_repo_rejected(self):
        self.gh.private_ok = False
        with self.assertRaises(RuntimeError):
            self.b.poll()
        self.runner.assert_not_called()

    def test_sync_failure_retries_labels_only(self):
        self.gh.fail_sync = True
        with self.assertRaises(RuntimeError):
            self.b.poll()
        self.assertEqual(self.receipt()['status'], 'queued')
        self.gh.fail_sync = False
        bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)
        self.assertIn((7, 'codex-queued'), self.gh.labels)

    def test_unrecognized_success_is_uncertain(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0, 'unknown success format', '')
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'uncertain')

    def test_mark_dispatching_before_queue_and_durable_intent(self):
        def run(*args, **kwargs):
            self.assertIn((7, 'codex-dispatching'), self.gh.labels)
            disk = json.loads((self.root / '.runtime/receipts.json').read_text())
            self.assertEqual(next(iter(disk.values()))['status'], 'dispatching')
            return subprocess.CompletedProcess([], 0, f'Queued message {MESSAGE}', '')
        self.runner.side_effect = run
        self.b.poll()

    def test_label_failure_never_invokes_queue(self):
        def failed_label(*args):
            raise RuntimeError('GitHub unavailable')
        self.gh.label = failed_label
        with self.assertRaises(RuntimeError):
            self.b.poll()
        self.runner.assert_not_called()
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_missing_executable_records_failure(self):
        self.runner.side_effect = FileNotFoundError()
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_killed_submission_is_uncertain(self):
        self.runner.return_value = subprocess.CompletedProcess([], -15, '', '')
        self.b.poll()
        self.assertEqual(self.receipt()['status'], 'uncertain')

    def test_stop_does_not_dispatch_another_issue(self):
        self.b.poll(stopping=lambda: True)
        self.runner.assert_not_called()

    def test_changed_body_during_authorization_deferred(self):
        calls = [issue(), issue() | {'body': 'changed'}]
        self.gh.issue = lambda n: calls.pop(0)
        self.b.poll()
        self.runner.assert_not_called()

    def test_fail_closed_on_corrupt_state(self):
        (self.root / '.runtime/receipts.json').write_text('invalid')
        with self.assertRaises(ValueError):
            bridge.Bridge(copy.deepcopy(CONFIG), self.root, self.gh)
        self.runner.assert_not_called()

    def test_session_change_requires_stop_and_preserves_receipts(self):
        bridge.atomic_json(self.root / 'config.json', CONFIG)
        self.b.poll()
        receipts_before = (self.root / '.runtime/receipts.json').read_text()
        with bridge.lock(self.root / '.runtime/listener.lock'):
            with self.assertRaises(RuntimeError):
                bridge.set_session(self.root, OTHER)
        self.assertEqual(bridge.config(self.root)['session'], SESSION)
        bridge.set_session(self.root, OTHER)
        self.assertEqual(bridge.config(self.root)['session'], OTHER)
        self.assertEqual((self.root / '.runtime/receipts.json').read_text(), receipts_before)
        bridge.Bridge(bridge.config(self.root), self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_session_lock_blocks_other_installations(self):
        with bridge.lock(bridge.session_lock(SESSION)):
            with self.assertRaises(RuntimeError):
                with bridge.lock(bridge.session_lock(SESSION)):
                    self.fail('Must never acquire a duplicate target lock')

    def test_invalid_session_change_keeps_config(self):
        bridge.atomic_json(self.root / 'config.json', CONFIG)
        with self.assertRaises(ValueError):
            bridge.set_session(self.root, 'not-a-uuid')
        self.assertEqual(bridge.config(self.root)['session'], SESSION)


class ApiTests(unittest.TestCase):
    @patch('bridge.subprocess.run')
    def test_paginated_api_and_json_stdin(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '[[{"number": 1}], [{"number": 2}]]', '')
        gh = bridge.GitHub(CONFIG['repo'])
        self.assertEqual([i['number'] for i in gh.pending()], [1, 2])
        self.assertIn('--paginate', run.call_args.args[0])
        run.return_value.stdout = '{}'
        gh.label(1, 'codex-queued')
        self.assertEqual(json.loads(run.call_args.kwargs['input']), {'labels': ['codex-queued']})
        self.assertIn('--hostname', run.call_args.args[0])

    @patch('bridge.subprocess.run')
    def test_approval_event_relabel_and_revocation(self, run):
        gh = bridge.GitHub(CONFIG['repo'])
        def events(items):
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps([items]), '')
        events([{'event': 'labeled', 'label': {'name': 'codex-send'}, 'actor': {'login': 'norman8823'}}])
        self.assertEqual(gh.approver(1), 'norman8823')
        events([{'event': 'unlabeled', 'label': {'name': 'codex-send'}, 'actor': {'login': 'norman8823'}}])
        self.assertIsNone(gh.approver(1))

    @patch('bridge.subprocess.run')
    def test_api_failure_sanitized(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'credential=SECRET')
        with self.assertRaises(RuntimeError) as err:
            bridge.GitHub(CONFIG['repo']).private()
        self.assertNotIn('SECRET', str(err.exception))


if __name__ == '__main__':
    unittest.main()
