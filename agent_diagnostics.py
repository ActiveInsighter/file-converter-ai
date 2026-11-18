"""Inspect stored interactions and reconcile explicitly canceled PocketBase jobs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from urllib.parse import quote

import httpx

from agent_trace import AgentTrace, TERMINAL, watch_interaction
from gemini_agent import ROOT, reconcile_requested_cancel, request_json
from gemini_text import PocketBase


async def diagnose(client, root, token, job_id, keys, seconds):
    response = await client.get(f'{root}/api/collections/aw_gemini_messages/records',
                                headers={'Authorization': token},
                                params={'filter': f'job="{job_id}"', 'perPage': 100, 'sort': 'index',
                                        'fields': 'id,index,status,providerState'}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'Read-only message lookup failed: HTTP {response.status_code}')
    messages = response.json().get('items') or []
    if not messages:
        raise RuntimeError('No saved messages found for request_id')

    async def inspect(message):
        trace = AgentTrace(job=job_id, segment=message['index'], secrets=keys)
        state = message.get('providerState') or {}
        if isinstance(state, str):
            state = json.loads(state)
        key = next((key for key in keys if hashlib.sha256(key.encode()).hexdigest() == state.get('credentialHash')), None)
        if not key or not state.get('id'):
            trace.emit('diagnostic_unavailable', reason='saved interaction or matching credential missing')
            return False
        url = f'{ROOT}/{quote(state["id"], safe="")}'
        trace.emit('diagnostic_start', read_only=True, duration_seconds=seconds, pb_status=message['status'])
        status, _, data = await request_json(client, 'GET', url, key)
        trace.emit('snapshot_http', http=status)
        if status != 200:
            trace.emit('diagnostic_unavailable', reason='upstream snapshot rejected')
            return False
        trace.snapshot(data)
        try:
            # Stream historical and live events, even if the saved interaction completed.
            async with asyncio.timeout(seconds):
                try:
                    async with asyncio.timeout(min(10, seconds / 2)):
                        await watch_interaction(client, url, key, trace)
                except TimeoutError:
                    trace.emit('diagnostic_reconnect', cursor_chars=len(trace.last_event_id or ''), remote_canceled=False)
                    await watch_interaction(client, url, key, trace)
        except TimeoutError:
            trace.emit('observation_window_ended', remote_canceled=False)
        finally:
            trace.summary('read-only observation ended')
        return True

    # Diagnostic fanout is bounded independently of the number of saved messages.
    semaphore = asyncio.Semaphore(4)
    async def bounded(message):
        async with semaphore:
            return await inspect(message)
    return 0 if all(await asyncio.gather(*(bounded(m) for m in messages))) else 2


async def reconcile_canceled_job(client, root, token, job_id, keys):
    headers = {'Authorization': token}
    saved = await client.get(f'{root}/api/collections/aw_gemini_jobs/records/{job_id}',
                             params={'fields': 'id,status,cancelRequested'}, headers=headers, timeout=30)
    if (saved.status_code != 200 or saved.json().get('status') != 'canceled'
            or saved.json().get('cancelRequested') is not True):
        raise RuntimeError('PocketBase does not show an explicit cancellation for this job')
    rows = await client.get(f'{root}/api/collections/aw_gemini_messages/records',
                            params={'filter': f'job="{job_id}" && attempted=true', 'perPage': 100,
                                    'fields': 'index,status,providerState'},
                            headers=headers, timeout=30)
    if rows.status_code != 200:
        raise RuntimeError('PocketBase rejected the canceled message lookup')

    candidates = [message for message in rows.json().get('items', []) if message['status'] in ('canceled', 'failed')]
    if not candidates:
        raise RuntimeError('No attempted canceled messages found')
    unconfirmed = False
    for message in candidates:
        trace = AgentTrace(job=job_id, segment=message['index'], secrets=keys, verbose=False)
        state = message.get('providerState') or {}
        if isinstance(state, str):
            state = json.loads(state)
        key = next((key for key in keys if hashlib.sha256(key.encode()).hexdigest() == state.get('credentialHash')), None)
        if not key or not state.get('id'):
            trace.emit('cancel_unavailable', reason='saved interaction or matching credential missing')
            unconfirmed = True
            continue
        url = f'{ROOT}/{quote(state["id"], safe="")}'
        status, _, data = await request_json(client, 'GET', url, key)
        state = data.get('status') if status == 200 and isinstance(data, dict) else None
        if not isinstance(state, str) or state not in TERMINAL:
            state = await reconcile_requested_cancel(client, url, key)
        trace.emit('cancel_reconciled', status=state)
        unconfirmed = unconfirmed or state == 'cancel_unconfirmed'
    return 2 if unconfirmed else 0


async def main(request_id=None, *, retry_cancel=False):
    root = os.environ.get('GEMINI_PB_URL', '').rstrip('/')
    job_id = request_id or os.environ.get('REQUEST_ID', '')
    if not re.fullmatch(r'https://[^/?#@]+', root) or not re.fullmatch(r'[a-z0-9]{15}', job_id):
        raise RuntimeError('Valid GEMINI_PB_URL and REQUEST_ID are required')
    seconds = int(os.environ.get('AGENT_TRACE_SECONDS', '180'))
    if not 10 <= seconds <= 900:
        raise RuntimeError('AGENT_TRACE_SECONDS must be 10..900')
    keys = [key.strip() for key in os.environ.get('GEMINI_API_KEYS', '').splitlines() if key.strip()]
    if not keys:
        raise RuntimeError('GEMINI_API_KEYS is required')
    async with httpx.AsyncClient(follow_redirects=False) as client:
        pb = PocketBase(client, root)
        await pb.authenticate(token=os.environ.get('POCKETBASE_SERVER_TOKEN', ''),
                              email=os.environ.get('GEMINI_PB_EMAIL', ''),
                              password=os.environ.get('GEMINI_PB_PASSWORD', ''))
        if retry_cancel:
            return await reconcile_canceled_job(client, root, pb.token, job_id, keys)
        return await diagnose(client, root, pb.token, job_id, keys, seconds)


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as error:
        # Never print response bodies, request URLs or credential-bearing exception text.
        print(f'[agent.trace] diagnostic setup failed: {type(error).__name__}', flush=True)
        raise SystemExit(1) from None
