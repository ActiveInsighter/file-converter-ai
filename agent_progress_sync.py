"""Repair progress for an existing execution. Google calls are GET only.

The original runner retains ownership of generation and final completion. This
observer writes only indexed public parts, verifying database contents after ACK.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

import httpx

from agent_progress import MessageProgress, observe_progress

ACTIVE = {'dispatching', 'pending', 'running'}


async def records_get(client, pb, collection, record='', **params):
    suffix = '/' + quote(record, safe='') if record else ''
    response = await client.get(f'{pb.root}/api/collections/{collection}/records{suffix}',
        headers={'Authorization': pb.token}, params=params, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'Progress sync lookup failed (HTTP {response.status_code})')
    return response.json()


class VerifiedProgressWriter:
    def __init__(self, client, pb, job_id, message_id, execution_id, *, sleep=asyncio.sleep):
        self.client, self.pb = client, pb
        self.job_id, self.message_id, self.execution_id = job_id, message_id, execution_id
        self.sleep = sleep
        self.progress = None

    async def __call__(self, body):
        for attempt in range(12):
            job = await records_get(self.client, self.pb, 'aw_gemini_jobs', self.job_id,
                                    fields='id,status,executionId,cancelRequested')
            if job['status'] not in ACTIVE or job.get('cancelRequested') or job.get('executionId') != self.execution_id:
                return {'closed': True}
            message = await records_get(self.client, self.pb, 'aw_gemini_messages', self.message_id,
                                        fields='id,status,uploadSequence')
            if message['status'] != 'running':
                return {'closed': True}
            body['sequence'] = message.get('uploadSequence', 0) + 1
            # The original runner owns usage/final result. Repair only public
            # parts, without overwriting its cumulative provider counters.
            wire = {**body, 'usage': {}}
            if self.progress:
                self.progress.save_spool()
            try:
                ack = await self.pb.post(f'jobs/{self.job_id}/messages/{self.message_id}/progress', wire, retries=3)
            except RuntimeError as error:
                if 'HTTP 409' not in str(error):
                    raise
                await self.sleep(min(2 ** attempt, 16))
                continue
            if ack.get('closed'):
                return ack
            if body['parts']:
                keys = ' || '.join(f'key="{part["key"]}"' for part in body['parts'])
                rows = await records_get(self.client, self.pb, 'aw_agent_parts',
                    filter=f'message="{self.message_id}" && ({keys})', perPage=32)
                saved = {part['key']: part for part in rows.get('items', [])}
                if not all(part['key'] in saved and all(saved[part['key']].get(key) == value
                    for key, value in part.items()) for part in body['parts']):
                    # A concurrent old heartbeat may have won this sequence.
                    # Never treat its duplicate-sequence ACK as our data saved.
                    await self.sleep(min(2 ** attempt, 16))
                    continue
            elif not ack.get('accepted'):
                # No content to verify; let the next pump refresh the heartbeat.
                await self.sleep(min(2 ** attempt, 16))
                continue
            return ack
        raise RuntimeError('Progress sync did not obtain a verified database ACK')


async def sync_existing(client, pb, job_id, keys, target):
    from gemini_agent import ROOT
    job = await records_get(client, pb, 'aw_gemini_jobs', job_id, fields='id,status,executionId')
    if job['status'] not in ACTIVE or not job.get('executionId'):
        raise RuntimeError('Progress sync requires an existing active execution')
    messages = await records_get(client, pb, 'aw_gemini_messages',
        filter=f'job="{job_id}" && status="running"', perPage=100)
    if len(messages.get('items', [])) != 1:
        raise RuntimeError('Progress sync requires exactly one running message')
    message = messages['items'][0]
    await records_get(client, pb, 'aw_agent_parts', filter=f'message="{message["id"]}"', perPage=1)
    state = message.get('providerState') or {}
    if isinstance(state, str):
        state = json.loads(state)
    key = next((key for key in keys if hashlib.sha256(key.encode()).hexdigest() == state.get('credentialHash')), None)
    if not key or not state.get('id'):
        raise RuntimeError('Saved interaction or matching credential unavailable')
    writer = VerifiedProgressWriter(client, pb, job_id, message['id'], job['executionId'])
    progress = MessageProgress(secrets=keys, post=writer, identity={'executionId': job['executionId']},
        target=target, job_id=job_id, message_id=message['id'])
    writer.progress = progress
    observer = asyncio.create_task(observe_progress(client, f'{ROOT}/{quote(state["id"], safe="")}', key, progress))
    pump = asyncio.create_task(progress.pump())
    try:
        while not observer.done():
            await asyncio.wait({observer}, timeout=3)
            current = await records_get(client, pb, 'aw_gemini_jobs', job_id, fields='status,executionId,cancelRequested')
            if current['status'] not in ACTIVE or current.get('cancelRequested') or current.get('executionId') != job['executionId']:
                break
        if observer.done():
            await observer
        await progress.flush()
        return 0
    finally:
        observer.cancel(); pump.cancel()
        await asyncio.gather(observer, pump, return_exceptions=True)


async def main(job_id):
    from gemini_text import PocketBase
    root = os.environ.get('GEMINI_PB_URL', '').rstrip('/')
    if not re.fullmatch(r'https://[^/?#@]+', root) or not re.fullmatch(r'[a-z0-9]{15}', job_id):
        raise RuntimeError('Valid PocketBase origin and job ID required')
    keys = [key.strip() for key in os.environ.get('GEMINI_API_KEYS', '').splitlines() if key.strip()]
    async with httpx.AsyncClient(follow_redirects=False) as client:
        pb = PocketBase(client, root)
        await pb.authenticate(token=os.environ.get('POCKETBASE_SERVER_TOKEN', ''),
            email=os.environ.get('GEMINI_PB_EMAIL', ''), password=os.environ.get('GEMINI_PB_PASSWORD', ''))
        return await sync_existing(client, pb, job_id, keys, Path(f'work/gemini/recovery/{job_id}.sync.progress.json'))
