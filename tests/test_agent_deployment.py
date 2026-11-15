import json
from pathlib import Path
import tempfile
import unittest

from agent_deploy_gate import decision
from agent_workspace_github import deploy, watch


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.posts = []
        self.ambiguous = False
        self.accepted = False
        self.rows = []

    def request(self, path, method='GET', body=None):
        if method == 'POST':
            # Intent must already exist, even before a response or transport error.
            self.assertEqual(len(list(self.directory.glob('*.json'))), 1)
            self.posts.append(body); self.accepted = True
            self.rows = [{'id': 42, 'head_sha': 'a' * 40, 'event': 'workflow_dispatch', 'head_branch': 'main'}]
            if self.ambiguous:
                raise TimeoutError('response lost')
            return {}
        if '/commits/' in path:
            return {'sha': 'a' * 40}
        if '/runs?' in path:
            return {'workflow_runs': self.rows}
        if path.endswith('/runs/42'):
            return {'id': 42, 'status': 'completed', 'conclusion': 'success', 'html_url': 'https://github.test/42'}
        return {'id': 7}

    def run_deploy(self, inputs=None):
        return deploy('owner/repo', 'deploy.yml', 'main', inputs or {}, request=self.request,
                      directory=self.directory, sleep=lambda _: None)

    def test_accepted_dispatch_and_success_are_reused_without_second_post(self):
        first = self.run_deploy({'deploy': True})
        second = self.run_deploy({'deploy': 'true'})
        self.assertEqual(first['id'], 42)
        self.assertTrue(second['reused'])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['inputs'], {'deploy': 'true'})

    def test_lost_response_reconciles_without_reposting(self):
        self.ambiguous = True
        with self.assertRaises(TimeoutError):
            self.run_deploy()
        self.assertEqual(self.run_deploy()['id'], 42)
        self.assertEqual(len(self.posts), 1)

    def test_changed_inputs_do_not_create_an_accidental_second_run(self):
        self.run_deploy({'deploy': True})
        with self.assertRaisesRegex(RuntimeError, 'different inputs'):
            self.run_deploy({'deploy': False})
        self.assertEqual(len(self.posts), 1)

    def test_hidden_accepted_run_can_be_reconciled_later(self):
        original = self.request
        visible = [False]
        def request(path, method='GET', body=None):
            data = original(path, method, body)
            return {'workflow_runs': []} if '/runs?' in path and not visible[0] else data
        with self.assertRaisesRegex(RuntimeError, 'not yet visible'):
            deploy('owner/repo', 'deploy.yml', 'main', {}, request=request, directory=self.directory, sleep=lambda _: None)
        visible[0] = True
        self.assertEqual(self.run_deploy()['id'], 42)
        self.assertEqual(len(self.posts), 1)

    def test_ambiguous_matching_runs_stop_instead_of_guessing_or_redispatching(self):
        self.run_deploy()
        path = next(self.directory.glob('*.json'))
        state = json.loads(path.read_text()); state.pop('run_id'); path.write_text(json.dumps(state))
        self.rows.append({**self.rows[0], 'id': 43})
        with self.assertRaisesRegex(RuntimeError, 'Multiple matching runs'):
            self.run_deploy()
        self.assertEqual(len(self.posts), 1)

    def test_watch_only_queries_exact_run_and_stops_on_success(self):
        paths = []
        def request(path):
            paths.append(path)
            return {'id': 42, 'status': 'completed', 'conclusion': 'success'}
        self.assertEqual(watch('owner/repo', 42, 300, request=request)['conclusion'], 'success')
        self.assertEqual(paths, ['/repos/owner/repo/actions/runs/42'])

    def test_direct_dispatch_is_routed_through_journalled_helper(self):
        for command in ['curl -X POST https://api.github.com/repos/o/r/actions/workflows/deploy.yml/dispatches',
                        'gh workflow run deploy.yml']:
            self.assertEqual(decision({'tool_call': {'args': {'code': command}}})['decision'], 'deny')
        self.assertEqual(decision({'tool_call': {'args': {'code': 'python3 /workspace/.anyworkflow/github.py deploy --repo o/r'}}})['decision'], 'allow')
        self.assertEqual(decision({'tool_call': {'args': {'code': 'npm run preflight'}}})['decision'], 'allow')
