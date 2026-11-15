import asyncio
import unittest

import httpx

from gemini_models import list_models, parse_keys


class ModelsTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(parse_keys(' a,b;c\na '), ['a', 'b', 'c'])

    def test_paginated_authenticated_catalog(self):
        async def run():
            requests = []
            def handler(request):
                requests.append(request)
                self.assertEqual(request.headers['x-goog-api-key'], 'test-secret')
                self.assertNotIn('key', request.url.params)
                identifier = 'gemini-next' if 'pageToken' in request.url.params else 'gemini-current'
                return httpx.Response(200, json={'models': [{'name': 'models/' + identifier, 'supportedGenerationMethods': ['generateContent']}], **({} if len(requests) == 2 else {'nextPageToken': 'next'})})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                models = await list_models(client, 'test-secret')
                self.assertEqual([m['id'] for m in models], ['gemini-current', 'gemini-next'])
                self.assertEqual(len(requests), 2)
        asyncio.run(run())

    def test_errors_do_not_include_secrets(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, text='test-secret'))) as client:
                with self.assertRaisesRegex(RuntimeError, '^Models API returned HTTP 403$'):
                    await list_models(client, 'test-secret')
        asyncio.run(run())

    def test_repeated_page_rejected(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'models': [], 'nextPageToken': 'loop'}))) as client:
                with self.assertRaisesRegex(RuntimeError, 'pagination is invalid'):
                    await list_models(client, 'test-secret')
        asyncio.run(run())
