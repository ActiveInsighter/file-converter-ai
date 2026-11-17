"""Explicit read-only capability checks and workspace recovery for existing jobs."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

import httpx

from agent_trace import AgentTrace
from github_context import prepare_github_context


async def main(mode, job_id):
    from gemini_text import PocketBase
    if mode not in ('check', 'rescue', 'probe') or not re.fullmatch(r'[a-z0-9]{15}', job_id):
        raise RuntimeError('Invalid maintenance mode or job ID')
    root = os.environ.get('GEMINI_PB_URL', '').rstrip('/')
    if not re.fullmatch(r'https://[^/?#@]+', root):
        raise RuntimeError('Valid GEMINI_PB_URL is required')
    keys = [v.strip() for v in os.environ.get('GEMINI_API_KEYS', '').splitlines() if v.strip()]
    trace = AgentTrace(job=job_id, secrets=keys)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        pb = PocketBase(client, root)
        await pb.authenticate(token=os.environ.get('POCKETBASE_SERVER_TOKEN', ''),
            email=os.environ.get('GEMINI_PB_EMAIL', ''), password=os.environ.get('GEMINI_PB_PASSWORD', ''))
        job_response = await client.get(f'{root}/api/collections/aw_gemini_jobs/records/{job_id}',
            headers={'Authorization': pb.token}, params={'fields': 'id,prompt'}, timeout=30)
        if job_response.status_code != 200:
            raise RuntimeError('Read-only task lookup failed')
        job = job_response.json()
        if mode == 'check':
            try:
                context = await prepare_github_context(client, os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', ''),
                                                       job['prompt'], trace=trace)
                trace.emit('capability_check_complete', context=context)
                return 0
            except RuntimeError as error:
                trace.emit('capability_check_failed', reason=str(error))
                return 2
        response = await client.get(f'{root}/api/collections/aw_gemini_messages/records',
            headers={'Authorization': pb.token}, params={'filter': f'job="{job_id}"', 'perPage': 1,
                'fields': 'providerState'}, timeout=30)
        if response.status_code != 200 or not response.json().get('items'):
            raise RuntimeError('Read-only interaction lookup failed')
        state = response.json()['items'][0].get('providerState') or {}
        if isinstance(state, str):
            state = json.loads(state)
        key = next((k for k in keys if hashlib.sha256(k.encode()).hexdigest() == state.get('credentialHash')), None)
        if not key or not state.get('environmentId'):
            raise RuntimeError('Saved environment or matching credential missing')
        context = await prepare_github_context(client, os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', ''),
                                               job['prompt'], require_write=False, trace=trace)
        if not context or not context['selected']:
            raise RuntimeError('Maintenance requires an unambiguous repository')
        if mode == 'probe':
            from gemini_agent import generate_agent
            from key_pool import KeyPool
            pool = KeyPool([key], rpm_per_key=60, rpd_per_key=10000)
            probe_state = {}
            async def checkpoint(update=None):
                if update:
                    probe_state.update(update)
                return {'state': probe_state, 'canceled': False}
            try:
                async with asyncio.timeout(180):
                    result = await generate_agent(client, pool, {
                        'id': 'probe-' + job_id, 'model': 'Antigravity',
                        'prompt': 'Read-only runtime check for ' + context['selected']['full_name'] + '. '
                            'Read /workspace/.anyworkflow/github.py and /.agents/hooks.json. '
                            'Run python3 /workspace/.anyworkflow/github.py identity. '
                            'Only inspect those files and identity; no repository clone or changes. '
                            'If all succeed reply AW_RUNTIME_OK and the authenticated login.',
                        'options': {}}, {'index': 1}, checkpoint=checkpoint)
                trace.emit('runtime_probe_result', error=result['error'], reply=result['reply'][:800], usage=result['usage'])
                return 0 if not result['error'] and 'AW_RUNTIME_OK' in result['reply'] else 2
            finally:
                await pool.close()
        # Recover Git objects only, not node_modules or arbitrary workspace data.
        path = 'workspace/' + context['selected']['full_name'].split('/')[1] + '/.git'
        url = ('https://generativelanguage.googleapis.com/v1beta/environments/' +
               quote(state['environmentId'], safe='') + '/files/' + path)
        archive = bytearray()
        async with client.stream('GET', url, params={'alt': 'media', 'recursive': 'true'},
                                 headers={'x-goog-api-key': key}, timeout=90) as download:
            trace.emit('workspace_download', http=download.status_code, path=path)
            if download.status_code != 200:
                raise RuntimeError('Workspace download unavailable; original environment unchanged')
            async for block in download.aiter_bytes():
                archive.extend(block)
                if len(archive) > 64 * 1024 * 1024:
                    raise RuntimeError('Git recovery archive exceeds 64 MiB')
        target = Path('work/gemini/recovery') / f'workspace-{job_id}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'format': 'anyworkflow-git-recovery-v1', 'job': job_id,
            'path': path, 'sha256': hashlib.sha256(archive).hexdigest(),
            'tar_base64': base64.b64encode(archive).decode()}))
        trace.emit('workspace_recovered', bytes=len(archive), artifact_file=target.name)
        return 0
