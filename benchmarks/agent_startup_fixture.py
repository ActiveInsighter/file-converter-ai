"""Run the real text entry point against isolated, deterministic HTTP fixtures."""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

import gemini_text

JOB_ID = 'startupfixture1'
MESSAGE_ID = 'startupmessage1'
EXECUTION = '123:1'


async def benchmark():
    checkpoints = {}
    creates = []
    saves = []

    async def route(request):
        if request.url.host == 'pb.fixture.invalid':
            payload = json.loads(request.content)
            assert payload['executionId'] == EXECUTION
            action = request.url.path.rsplit('/', 1)[-1]
            await asyncio.sleep(0.1)
            if action == 'claim':
                return httpx.Response(200, json={
                    'id': JOB_ID, 'model': 'Antigravity', 'titleSource': 'manual',
                    'prompt': 'Reply exactly OK.', 'options': {}, 'agentContext': None,
                    'messages': [{'id': MESSAGE_ID, 'index': 1, 'inputText': '', 'status': 'queued'}],
                })
            if action == 'start':
                return httpx.Response(200, json={'send': True})
            if action == 'checkpoint':
                checkpoints.update(payload.get('state') or {})
                return httpx.Response(200, json={'state': checkpoints, 'canceled': False})
            if action == 'complete':
                saves.append(payload)
            return httpx.Response(200, json={'titleUpdated': False})
        assert request.url.host == 'generativelanguage.googleapis.com'
        if request.method == 'POST' and request.url.path == '/v1beta/interactions':
            creates.append(json.loads(request.content))
            print('startup_fixture_model_send=' + datetime.now(timezone.utc).isoformat(timespec='milliseconds'), flush=True)
            return httpx.Response(200, json={'id': 'fixture-interaction', 'status': 'completed',
                'environment_id': 'fixture-sandbox', 'output_text': 'OK'})
        if request.method == 'GET':
            return httpx.Response(200, text='event: interaction.completed\ndata: {"event_type":"interaction.completed"}\n\n')
        return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP',
            'content': {'parts': [{'text': '{"summary":"fixture reply"}'}]}}]})

    env = {
        'REQUEST_ID': JOB_ID, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
        'GEMINI_PB_URL': 'https://pb.fixture.invalid', 'POCKETBASE_SERVER_TOKEN': 'fixture-token',
        'GEMINI_API_KEYS': 'fixture-key',
    }
    original = os.getcwd()
    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, env, clear=True):
        os.chdir(work)
        try:
            with patch('gemini_text.httpx.AsyncClient', return_value=client):
                assert await gemini_text.main() == 0
        finally:
            os.chdir(original)
    assert len(creates) == 1 and creates[0]['input'] == 'Reply exactly OK.'
    assert len(saves) == 1 and saves[0]['reply'] == 'OK'
    print('startup_fixture_validated=one_model_post_and_saved_reply', flush=True)


if __name__ == '__main__':
    asyncio.run(benchmark())
