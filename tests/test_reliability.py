import copy
import datetime
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bridge
import service
from test_bridge import CONFIG, SESSION, OTHER, MESSAGE, DELIVERY, FakeGitHub, issue

CODE = 'ABCD1234EFGH5678IJKL9012MNOP3456'


class PairGitHub(FakeGitHub):
    repo = CONFIG['repo']

    def __init__(self):
        super().__init__()
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
        self.item.update(number=8, created_at=created.isoformat(), labels=[{'name': 'codex-pair'}],
                         body=json.dumps(dict(route='test-one', code=CODE,
                                              expires_at=(created + datetime.timedelta(minutes=15)).isoformat())))
        self.items = None
        self.comments = []

    def pending(self, label='codex-send'):
        return copy.deepcopy(self.items if self.items is not None else [self.item])

    def approver(self, n, label='codex-send'):
        return self.actor

    def comment(self, n, body):
        if self.offline:
            raise RuntimeError('GitHub unavailable')
        self.comments.append((n, body))


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bridge.atomic_json(self.root / 'config.json', CONFIG)
        self.gh = PairGitHub()

    def pair(self, code=CODE, session=None, env=None):
        return bridge.pair(self.root, code, session, self.gh,
                           {'CODEX_THREAD_ID': SESSION} if env is None else env)

    def body(self, **changes):
        body = json.loads(self.gh.item['body'])
        body.update(changes)
        self.gh.item['body'] = json.dumps(body)

    def test_pair_captures_session_and_posts_no_code_uuid_or_paths(self):
        self.assertEqual(self.pair(), SESSION)
        record = bridge.Routes(self.root).data['routes']['test-one']
        self.assertEqual(record['session'], SESSION)
        self.assertEqual(record['status'], 'paired')
        comment = self.gh.comments[0][1]
        self.assertIn('test-one', comment)
        for secret in (CODE, SESSION, str(self.root)):
            self.assertNotIn(secret, comment)
        self.assertIn((8, 'codex-paired'), self.gh.labels)

    def test_expired_code_refused(self):
        self.body(expires_at='2000-01-01T00:00:00Z')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.pair()
        self.assertFalse((self.root / '.runtime/routes.json').exists())

    def test_overlong_and_naive_expiry_refused(self):
        for expiry in ('2099-01-01T00:00:00Z', '2099-01-01T00:00:00'):
            self.body(expires_at=expiry)
            with self.assertRaises(ValueError):
                self.pair()

    def test_future_creation_refused(self):
        self.gh.item['created_at'] = '2099-01-01T00:00:00Z'
        with self.assertRaises(ValueError):
            self.pair()

    def test_single_use_even_if_issue_is_edited(self):
        self.pair()
        self.body(route='stolen-route')
        with self.assertRaisesRegex(ValueError, 'consumed'):
            self.pair()
        self.assertNotIn('stolen-route', bridge.Routes(self.root).data['routes'])

    def test_unauthorized_author(self):
        self.gh.item['user']['login'] = 'attacker'
        with self.assertRaisesRegex(ValueError, 'unauthorized'):
            self.pair()

    def test_unauthorized_label_actor(self):
        self.gh.actor = 'attacker'
        with self.assertRaisesRegex(ValueError, 'unauthorized'):
            self.pair()

    def test_wrong_repo_closed_pr_and_execution_labels_refused(self):
        original = copy.deepcopy(self.gh.item)
        for fields in (dict(repository_url='https://api.github.com/repos/other/repo'),
                       dict(state='closed'), dict(pull_request={}),
                       dict(labels=[{'name': 'codex-pair'}, {'name': 'codex-send'}]),
                       dict(labels=[{'name': 'codex-pair'}, {'name': 'codex-paired'}])):
            self.gh.item = copy.deepcopy(original) | fields
            with self.assertRaises(ValueError):
                self.pair()

    def test_ambiguous_code_refused(self):
        self.gh.items = [self.gh.item, self.gh.item | {'number': 9}]
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.pair()

    def test_missing_env_requires_manual_uuid(self):
        with self.assertRaisesRegex(ValueError, 'No CODEX_THREAD_ID'):
            self.pair(env={})
        self.assertEqual(self.pair(session=OTHER, env={}), OTHER)

    def test_manual_uuid_overrides_inherited_nested_context(self):
        self.assertEqual(self.pair(session=OTHER), OTHER)
        self.assertEqual(bridge.Routes(self.root).data['routes']['test-one']['method'], 'manual UUID confirmation')

    def test_invalid_env_never_guesses(self):
        with self.assertRaises(ValueError):
            self.pair(env={'CODEX_THREAD_ID': 'invalid'})

    def test_changed_request_refused(self):
        self.gh.issue = lambda n: self.gh.item | {'body': self.gh.item['body'] + ' ' * n}
        calls = [self.gh.item, self.gh.item | {'title': 'changed'}]
        self.gh.issue = lambda n: calls.pop(0)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.pair()

    def test_claim_failure_consumes_code_without_binding(self):
        self.gh.label = lambda *args: (_ for _ in ()).throw(RuntimeError('unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            self.pair()
        routes = bridge.Routes(self.root)
        self.assertFalse(routes.data['routes'])
        self.assertEqual(next(iter(routes.data['pairs'].values()))['status'], 'uncertain')
        with self.assertRaisesRegex(ValueError, 'consumed'):
            self.pair()

    def test_confirmation_outage_retries_without_rebinding(self):
        def fail_comment(*args):
            raise RuntimeError('unavailable')
        self.gh.comment = fail_comment
        self.pair()
        before = bridge.Routes(self.root).data['routes']
        self.gh.comment = lambda n, body: self.gh.comments.append((n, body))
        routes = bridge.Routes(self.root)
        routes.sync(self.gh)
        self.assertEqual(routes.data['routes'], before)
        self.assertEqual(len(self.gh.comments), 1)

    def test_clear_requires_new_code_and_updates_only_selected_route(self):
        self.pair()
        routes = bridge.Routes(self.root)
        routes.data['routes']['test-two'] = dict(repo=CONFIG['repo'], session=OTHER, status='paired')
        routes.hold('test-one', 'Operator is clearing session')
        with self.assertRaises(ValueError):
            routes.destination('test-one', CONFIG['repo'])
        self.gh.item['number'] = 9
        self.body(code=CODE + 'X')
        self.pair(code=CODE + 'X', session=MESSAGE)
        routes = bridge.Routes(self.root)
        self.assertEqual(routes.destination('test-one', CONFIG['repo']), MESSAGE)
        self.assertEqual(routes.destination('test-two', CONFIG['repo']), OTHER)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bridge.atomic_json(self.root / '.runtime/routes.json', dict(routes={
            'test-one': dict(repo=CONFIG['repo'], session=SESSION, status='paired'),
            'test-two': dict(repo=CONFIG['repo'], session=OTHER, status='paired')}, pairs={}))
        self.gh = FakeGitHub()
        self.b = bridge.Bridge(CONFIG, self.root, self.gh)
        self.runner = patch('bridge.subprocess.run', return_value=subprocess.CompletedProcess(
            [], 0, f'Queued message {MESSAGE}', '')).start()
        self.addCleanup(patch.stopall)

    def test_two_routes_dispatch_to_two_explicit_sessions(self):
        self.b.poll()
        self.gh.item['number'] = 8
        self.gh.item['body'] = json.dumps(dict(route='test-two', delivery_id=OTHER, instructions='Second task'))
        self.b.poll()
        self.assertEqual([call.args[0][3] for call in self.runner.call_args_list], [SESSION, OTHER])

    def test_missing_unknown_or_duplicate_route_fields_never_dispatch(self):
        for body in ('Legacy plain text', '{}',
                     json.dumps(dict(route='unknown', delivery_id=DELIVERY, instructions='test')),
                     '{"route":"test-one","route":"test-two"}'):
            self.gh.item['body'] = body
            self.b.poll()
            self.assertTrue(self.b.blocked)
        self.runner.assert_not_called()
        self.assertFalse(self.b.state)

    def test_unbound_never_uses_legacy_session(self):
        (self.root / '.runtime/routes.json').unlink()
        self.b.poll()
        self.runner.assert_not_called()

    def test_pairing_request_cannot_be_executed(self):
        self.gh.item['labels'].append({'name': 'codex-pair'})
        self.b.poll()
        self.runner.assert_not_called()

    def test_known_stale_target_never_uses_other_route(self):
        routes = bridge.Routes(self.root)
        routes.hold('test-one', 'Closed session')
        self.b.poll()
        self.runner.assert_not_called()

    def test_no_rollout_holds_route_and_never_retries_after_repair(self):
        self.runner.return_value = subprocess.CompletedProcess([], 1, '', 'thread/queue/add failed: no rollout found for thread id')
        self.b.poll()
        receipt = next(iter(self.b.state.values()))
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('No rollout found', receipt['reason'])
        self.assertEqual(bridge.Routes(self.root).data['routes']['test-one']['status'], 'stale')
        routes = bridge.Routes(self.root)
        routes.data['routes']['test-one'].update(status='paired', session=OTHER)
        routes.save()
        bridge.Bridge(CONFIG, self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_duplicate_delivery_id_on_new_issue_refused(self):
        self.b.poll()
        self.gh.item['number'] = 8
        self.b.poll()
        self.assertEqual(self.runner.call_count, 1)
        self.assertIn('already attempted', next(iter(self.b.blocked.values())))

    def test_uncertain_attempt_holds_route_and_restart_never_replays(self):
        self.runner.side_effect = subprocess.TimeoutExpired('codex', 45)
        self.b.poll()
        self.assertEqual(bridge.Routes(self.root).data['routes']['test-one']['status'], 'uncertain')
        bridge.Bridge(CONFIG, self.root, self.gh).poll()
        self.assertEqual(self.runner.call_count, 1)

    def test_crash_recovers_uncertain_and_holds_only_its_route(self):
        self.runner.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.b.poll()
        bridge.Bridge(CONFIG, self.root, self.gh)
        routes = bridge.Routes(self.root)
        self.assertEqual(routes.data['routes']['test-one']['status'], 'uncertain')
        self.assertEqual(routes.data['routes']['test-two']['status'], 'paired')

    def test_success_remains_queued_not_acknowledged_or_completed(self):
        self.b.poll()
        receipt = next(iter(self.b.state.values()))
        self.assertEqual(receipt['status'], 'queued')
        self.assertNotIn('completed', receipt)
        self.assertNotIn('acknowledged', receipt)

    def test_historical_receipts_and_labels_never_replay_on_new_route(self):
        historical = {CONFIG['repo'] + '#' + str(n): dict(issue=n, repo=CONFIG['repo'], session=SESSION,
                       status='queued', synced=True, message_id=MESSAGE) for n in (1, 2, 3)}
        bridge.atomic_json(self.root / '.runtime/receipts.json', historical)
        self.b = bridge.Bridge(CONFIG, self.root, self.gh)
        for n in (1, 2, 3):
            self.gh.item['number'] = n
            self.b.poll()
        self.runner.assert_not_called()
        self.assertEqual(self.b.state, historical)

    def test_corrupt_route_registry_fails_closed(self):
        (self.root / '.runtime/routes.json').write_text('broken')
        with self.assertRaises(ValueError):
            self.b.poll()
        self.runner.assert_not_called()

    def test_activity_log_rotation_is_bounded(self):
        path = self.root / '.runtime/activity.log'
        path.write_text('x' * 1_000_000)
        self.b.log('bounded')
        self.assertLess(path.stat().st_size, 1000)
        self.assertEqual(path.with_name('activity.log.1').stat().st_size, 1_000_000)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'LaunchAgents/test.plist'
        patch('service.plist_path', return_value=self.path).start()
        patch('service.sys.platform', 'darwin').start()
        patch('service.shutil.which', side_effect=lambda name: '/usr/local/bin/' + name).start()
        self.run = patch('service.subprocess.run').start()
        self.addCleanup(patch.stopall)
        self.is_loaded = False
        self.disabled = False

        def run(args, **kwargs):
            command = args[1]
            rc = 0
            if command == 'print':
                rc = 0 if self.is_loaded else 113
            elif command == 'bootstrap':
                self.is_loaded = True
            elif command == 'bootout':
                self.is_loaded = False
            elif command == 'disable':
                self.disabled = True
            elif command == 'enable':
                self.disabled = False
            return subprocess.CompletedProcess(args, rc, '', '')
        self.run.side_effect = run

    def test_install_login_start_and_crash_restart_policy(self):
        service.install(self.root)
        data = plistlib.loads(self.path.read_bytes())
        self.assertTrue(data['RunAtLoad'])
        self.assertTrue(data['KeepAlive'])
        self.assertEqual(data['ThrottleInterval'], 10)
        self.assertEqual(data['ProgramArguments'][1:], [str(self.root / 'bridge.py'), 'listen'])
        self.assertIn('/usr/local/bin', data['EnvironmentVariables']['PATH'])
        self.assertTrue(self.is_loaded)

    def test_stop_disables_before_unloading_and_stays_stopped(self):
        service.install(self.root)
        self.run.reset_mock()
        service.stop(self.root)
        commands = [call.args[0][1] for call in self.run.call_args_list]
        self.assertEqual(commands, ['disable', 'print', 'bootout'])
        self.assertTrue(self.disabled)
        self.assertFalse(self.is_loaded)
        self.assertTrue((self.root / '.runtime/stop-request').exists())
        self.assertNotIn('bootstrap', commands)

    def test_start_and_restart_do_not_bootstrap_duplicate(self):
        service.install(self.root)
        self.run.reset_mock()
        service.start()
        self.assertNotIn('bootstrap', [call.args[0][1] for call in self.run.call_args_list])
        service.stop(self.root)
        service.start()
        self.assertTrue(self.is_loaded)
        self.assertFalse(self.disabled)

    def test_uninstall_preserves_receipts_and_config(self):
        marker = self.root / 'config.json'
        marker.write_text('preserved')
        service.install(self.root)
        service.uninstall(self.root)
        self.assertFalse(self.path.exists())
        self.assertEqual(marker.read_text(), 'preserved')

    def test_never_persist_environment_tokens(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'SECRET', 'GITHUB_TOKEN': 'SECRET'}):
            service.install(self.root)
        self.assertNotIn('SECRET', self.path.read_text())

    def test_foreign_plist_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_bytes(plistlib.dumps(dict(ProgramArguments=['python', '/other/bridge.py'])))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'another installation'):
            service.install(self.root)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
