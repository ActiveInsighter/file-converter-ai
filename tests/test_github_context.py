import unittest

import httpx

from github_context import git_write_access, prepare_github_context, repository_candidates


REPOS = [{'full_name': 'owner/Anyworkflow-remote', 'default_branch': 'main'},
         {'full_name': 'owner/exam-docs', 'default_branch': 'main'},
         {'full_name': 'owner/Anyworkflow-remote-drive', 'default_branch': 'main'}]


class RepositoryTests(unittest.TestCase):
    def test_user_project_names_resolve_without_global_search(self):
        for text, expected in [('重新推送部署下我的exam docs项目', 'owner/exam-docs'),
                               ('看看工作流remote项目，重构后推送分支', 'owner/Anyworkflow-remote'),
                               ('修改 https://github.com/owner/exam-docs', 'owner/exam-docs')]:
            self.assertEqual(repository_candidates(text, REPOS)[0]['full_name'], expected)

    def test_ambiguous_names_are_not_silently_selected(self):
        repos = [{'full_name': owner + '/remote'} for owner in ['a', 'b']]
        self.assertEqual(len(repository_candidates('重构remote项目', repos)), 2)


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_check_uses_receive_pack_get_not_mutating_refs(self):
        calls = []
        async def outbound(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            if request.url.path == '/user':
                return httpx.Response(200, json={'login': 'owner'})
            if request.url.path == '/user/repos':
                return httpx.Response(200, json=REPOS)
            self.assertEqual(request.url.params['service'], 'git-receive-pack')
            return httpx.Response(200, content=b'001f# service=git-receive-pack\n',
                headers={'content-type': 'application/x-git-receive-pack-advertisement'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            context = await prepare_github_context(client, 'fixture', '重新推送部署下我的exam docs项目')
        self.assertEqual(context['selected']['full_name'], 'owner/exam-docs')
        self.assertTrue(context['write_verified'])
        self.assertEqual(len(calls), 3)

    async def test_denied_push_fails_before_remote_agent_starts(self):
        async def outbound(request):
            if request.url.path == '/user':
                return httpx.Response(200, json={'login': 'owner'})
            if request.url.path == '/user/repos':
                return httpx.Response(200, json=REPOS)
            return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            with self.assertRaisesRegex(RuntimeError, '无法写入 owner/Anyworkflow-remote'):
                await prepare_github_context(client, 'fixture', '重构我的remote项目并推送分支')

    async def test_explicit_repository_skips_inventory(self):
        calls = []
        async def outbound(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={'login': 'owner'} if request.url.path == '/user' else REPOS[1])
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            result = await prepare_github_context(client, 'fixture', '读取owner/exam-docs')
        self.assertEqual(result['selected']['full_name'], 'owner/exam-docs')
        self.assertEqual(calls, ['/user', '/repos/owner/exam-docs'])

    async def test_generic_message_needs_no_github_or_credentials(self):
        async def outbound(request):
            self.fail('generic message must not make GitHub calls')
        async with httpx.AsyncClient(transport=httpx.MockTransport(outbound)) as client:
            self.assertIsNone(await prepare_github_context(client, '', '你是谁？'))
