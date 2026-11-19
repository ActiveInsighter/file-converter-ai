"""Measure a warm Agent executor using an isolated PocketBase and a fake model."""
import argparse
import asyncio
import contextlib
import json
import socket
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_trace import sse_events
from gemini_text import PocketBase, run_job
from key_pool import KeyPool


async def measure(base, count, delay):
    async def add_database_delay(request):
        if delay and request.url.host == '127.0.0.1':
            await asyncio.sleep(delay)

    posted_at = []
    async def provider(request):
        if request.method == 'POST' and request.url.path == '/v1beta/interactions':
            posted_at.append(time.perf_counter())
            assert json.loads(request.content)['input'] == 'Reply exactly OK.'
            return httpx.Response(200, json={
                'id': 'fixture-' + str(len(posted_at)), 'environment_id': 'isolated-fixture',
                'status': 'completed', 'output_text': 'OK',
            })
        assert request.method == 'GET' and request.url.params.get('stream') == 'true'
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              text='event: interaction.completed\ndata: {"event_type":"interaction.completed"}\n\n')

    async with httpx.AsyncClient(
        mounts={'https://generativelanguage.googleapis.com': httpx.MockTransport(provider)},
        event_hooks={'request': [add_database_delay]},
        timeout=10,
        trust_env=False,
    ) as client:
        async def api(method, path, token='', **kwargs):
            response = await client.request(method, base + path, headers={'Authorization': token}, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        for _ in range(150):
            try:
                await api('GET', '/api/health')
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError('Isolated PocketBase did not start')

        password = 'isolated-fixture-password'
        async def login(collection, identity):
            auth = await api('POST', f'/api/collections/{collection}/auth-with-password',
                             json={'identity': identity, 'password': password})
            return auth['token']

        admin = await login('_superusers', 'admin@startup.invalid')
        async def fixture_account(collection, identity):
            await api('POST', f'/api/collections/{collection}/records', admin, json={
                'email': identity, 'password': password, 'passwordConfirm': password})
            return await login(collection, identity)

        owner = await fixture_account('aw_clients', 'owner@startup.invalid')
        service = await fixture_account('aw_gemini_services', 'executor@startup.invalid')
        pb = PocketBase(client, base)
        await pb.authenticate(token=service)

        ready = asyncio.Event()
        queued = asyncio.Queue()
        async def listen():
            async with client.stream('GET', base + '/api/realtime', timeout=None) as stream:
                stream.raise_for_status()
                async for data, event, _ in sse_events(stream):
                    if event == 'PB_CONNECT':
                        await api('POST', '/api/realtime', admin, json={
                            'clientId': data['clientId'], 'subscriptions': ['aw_gemini_jobs']})
                        ready.set()
                    elif event == 'aw_gemini_jobs' and data['action'] == 'create':
                        await queued.put(data['record']['id'])

        async def pool_factory(_options):
            return KeyPool(['fixture-key'], rpm_per_key=1e6, rpd_per_key=100)

        listener = asyncio.create_task(listen())
        samples = []
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            with tempfile.TemporaryDirectory(prefix='warm-agent-replies-') as replies:
                for index in range(count):
                    job_id = uuid.uuid4().hex[:15]

                    async def execute():
                        assert await asyncio.wait_for(queued.get(), timeout=5) == job_id
                        # Model the dispatcher hand-off inside the isolated test database.
                        await api('PATCH', f'/api/collections/aw_gemini_jobs/records/{job_id}', admin,
                                  json={'status': 'dispatching'})
                        assert await run_job(pb, client, job_id, f'{index + 1}:1', pool_factory, Path(replies)) == 0

                    executor = asyncio.create_task(execute())
                    submitted_at = time.perf_counter()
                    await api('POST', '/api/anyworkflow/gemini/jobs', owner, json={
                        'id': job_id, 'model': 'Antigravity', 'prompt': 'Reply exactly OK.'})
                    await asyncio.wait_for(executor, timeout=15)
                    assert len(posted_at) == index + 1
                    samples.append(posted_at[-1] - submitted_at)
                    saved = await api('GET', f'/api/anyworkflow/gemini/jobs/{job_id}', owner)
                    assert saved['job']['status'] == 'succeeded'
                    message_id = saved['messages'][0]['id']
                    final_message = await api('GET', f'/api/anyworkflow/gemini/jobs/{job_id}/messages/{message_id}', owner)
                    assert final_message['reply'] == 'OK'
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pocketbase', type=Path, required=True)
    parser.add_argument('--pocketbase-source', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--pocketbase-delay-ms', type=float, default=0)
    args = parser.parse_args()
    assert args.samples > 0 and args.pocketbase_delay_ms >= 0
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='warm-pocketbase-') as data, open(Path(data) / 'server.log', 'w+') as log:
        options = ['--dir', data, '--hooksDir', str(args.pocketbase_source / 'pb_hooks'),
                   '--migrationsDir', str(args.pocketbase_source / 'pb_migrations'), '--automigrate=false']
        cmd = [str(args.pocketbase.resolve())]
        for command in (
            ['superuser', 'upsert', 'admin@startup.invalid', 'isolated-fixture-password'],
            ['migrate', 'up'],
        ):
            subprocess.run(cmd + command + options, stdout=log, stderr=log, check=True)
        process = subprocess.Popen(cmd + ['serve', '--http', f'127.0.0.1:{port}'] + options,
                                   stdout=log, stderr=log)
        try:
            with patch.dict('os.environ', {'ANTIGRAVITY_GITHUB_TOKEN': '', 'GEMINI_MODEL': '', 'GEMINI_THINKING_LEVEL': ''}):
                results = asyncio.run(measure(f'http://127.0.0.1:{port}', args.samples, args.pocketbase_delay_ms / 1000))
        except Exception:
            log.seek(0)
            print(log.read()[-3000:])
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    print(json.dumps({
        'sample_count': len(results),
        'added_delay_per_pocketbase_request_ms': args.pocketbase_delay_ms,
        'submit_to_model_post_seconds': [round(value, 3) for value in results],
        'median_seconds': round(statistics.median(results), 3),
        'max_seconds': round(max(results), 3),
    }))


if __name__ == '__main__':
    main()
