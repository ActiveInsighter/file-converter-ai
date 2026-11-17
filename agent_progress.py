"""Provider-neutral, bounded message parts with retryable batched persistence.

Only public reasoning summaries are retained. Provider thoughts/signatures and
arbitrary interaction objects never enter the database. Poll snapshots reconcile
SSE so losing the optional event connection does not lose completed steps.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import re

from agent_trace import AgentTrace, TERMINAL
from response_capture import sanitize

PART_BYTES = 60 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
USAGE = {'total_input_tokens': 'promptTokenCount', 'total_output_tokens': 'candidatesTokenCount',
         'total_thought_tokens': 'thoughtsTokenCount', 'total_tool_use_tokens': 'toolUseTokenCount',
         'total_cached_tokens': 'cachedContentTokenCount', 'total_tokens': 'totalTokenCount'}


class MessageProgress:
    def __init__(self, *, secrets=(), post=None, identity=None, target=None, job_id='', message_id=''):
        self.redactor = AgentTrace(secrets=secrets, verbose=False)
        self.parts, self.dirty, self.usage, self.steps = {}, set(), {}, {}
        self._last_flushed_usage = {}
        self.sequence = 0
        self.capture_bytes = 0
        self.post, self.identity, self.target = post, identity or {}, target
        self.job_id, self.message_id = job_id, message_id
        self.last_event_id = None
        self.seen_events = set()
        self.event_ids = deque()
        self.pending = None
        self.closed = False
        self.lock = asyncio.Lock()

    def put(self, key, kind, content='', *, status='completed', tool_name='', call_id='', provider_type='', truncated=False, payload=None, tool_kind=''):
        previous = self.parts.get(key)
        if previous is None and len(self.parts) >= 10000:
            return
        content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', self.redactor.redact(content))
        raw = content.encode('utf-8')
        old_bytes = len(previous['content'].encode()) if previous else 0
        budget = max(0, MAX_CAPTURE_BYTES - self.capture_bytes + old_bytes)
        limit = min(PART_BYTES, budget)
        # Keep identities stable and avoid collisions with reasoning and annotations.
        if previous is not None:
            sequence = previous['sequence']
        elif key.startswith('step:'):
            sequence = int(key.split(':')[1]) + 1
            if sequence > 9998: return
        elif key == 'agent-status': sequence = 10000
        elif key == 'answer': sequence = 9999
        elif key == 'reasoning': sequence = 1
        else: sequence = previous['sequence'] if previous else len(self.parts) + 1
        if previous is None and key not in ('answer', 'agent-status'):
            occupied = {p['sequence'] for p in self.parts.values()}
            while sequence in occupied and sequence <= 9998:
                sequence += 1
            if sequence > 9998:
                return
        part = {'key': key, 'sequence': sequence,
                'type': kind, 'content': raw[:limit].decode('utf-8', errors='ignore'), 'status': status,
                'toolName': self.redactor.redact(tool_name)[:120], 'callId': self.redactor.redact(call_id)[:256],
                'providerType': provider_type[:120], 'truncated': truncated or len(raw) > limit}
        if tool_kind:
            part['toolKind'] = tool_kind
        if payload is not None:
            # A single oversized payload must not prevent the dirty queue draining.
            clean_payload = sanitize(payload, secrets=self.redactor.secrets)
            encoded_payload = json.dumps(clean_payload, ensure_ascii=False)
            if len(encoded_payload.encode()) <= PART_BYTES:
                part['payload'] = clean_payload
            else:
                part['truncated'] = True
        if part != previous:
            self.parts[key] = part
            self.capture_bytes += len(part['content'].encode()) - old_bytes
            self.dirty.add(key)

    def snapshot(self, data):
        raw_usage = data.get('usage') or {}
        self.usage.update({dst: max(self.usage.get(dst, 0), raw_usage[src]) for src, dst in USAGE.items()
                           if type(raw_usage.get(src)) is int and raw_usage[src] >= 0})
        steps = data.get('steps') or data.get('outputs') or []
        self.usage['agentStepCount'] = max(self.usage.get('agentStepCount', 0), len(steps))
        for index, step in enumerate(steps):
            if isinstance(step, dict):
                self.step(index, step)
        self.put('agent-status', 'status', str(data.get('status', 'in_progress')),
                 status='completed' if data.get('status') in TERMINAL else 'running')
        if data.get('output_text') and not any(p['type'] == 'text' and key != 'answer' for key, p in self.parts.items()):
            self.put('answer', 'text', data['output_text'], status='completed' if data.get('status') in TERMINAL else 'running')

    def step(self, index, step, status='completed'):
        kind = step.get('type', '')
        key = f'step:{index}'
        if kind == 'thought':
            # Explicit summary only. Never persist content/text or signatures of thought steps.
            summary = step.get('summary') or []
            if isinstance(summary, dict): summary = [summary]
            if isinstance(summary, str):
                text = summary
            else:
                text = '\n'.join(p.get('text', '') for p in summary if isinstance(p, dict) and p.get('type') == 'text')
            if text:
                self.put(key, 'reasoning_summary', text, status=status, provider_type=kind, truncated=bool(step.get('_truncated')))
        elif kind in ('model_output', 'text'):
            blocks = step.get('content', []) if kind == 'model_output' else [step]
            text = step.get('text', '') or '\n'.join(p.get('text', '') for p in blocks
                                                    if isinstance(p, dict) and p.get('type') == 'text')
            if text:
                self.put(key, 'text', text, status=status, provider_type=kind, truncated=bool(step.get('_truncated')))
            annotations = []
            for p in blocks:
                if isinstance(p, dict):
                    for ann in p.get('annotations') or []:
                        if isinstance(ann, dict) and ann.get('url'):
                            annotations.append({'title': ann.get('title', ''), 'url': ann.get('url', '')})
                    if p.get('type') == 'image':
                        self.put(f'{key}:img', 'image', json.dumps({'mimeType': p.get('mime_type') or p.get('mimeType') or 'image/png',
                                                                    'uri': p.get('uri') or '', 'inline': bool(p.get('data'))}, ensure_ascii=False),
                                 status=status, provider_type='image')
            if annotations:
                self.put(f'{key}:annotations', 'annotations', json.dumps(annotations, ensure_ascii=False),
                         status=status, provider_type='annotations')
        elif kind.endswith(('_call', '_result')):
            # Serialize only known tool fields; discard signatures and hidden provider state.
            allowed = ('language', 'arguments', 'code', 'command', 'input', 'output', 'result', 'content',
                       'error', 'stdout', 'stderr', 'results', 'response', 'outcome', 'exit_code',
                       'server_name', 'queries', 'urls')
            payload = {k: step[k] for k in allowed if k in step}
            sensitive_key = re.compile(r'(?i)^(?:.*signature|secret|authorization|password|api[_-]?key|access[_-]?token|token|headers|environment)$')
            def scrub(value):
                if isinstance(value, dict):
                    return {k: scrub(v) for k, v in value.items() if not sensitive_key.search(str(k))}
                if isinstance(value, list): return [scrub(v) for v in value]
                if isinstance(value, str): return self.redactor.redact(value)
                return value
            clean_payload = scrub(payload) if payload else None
            text = json.dumps(clean_payload, ensure_ascii=False) if clean_payload else ''
            is_err = bool(step.get('is_error')) or (isinstance(step.get('exit_code'), int) and step['exit_code'] != 0)
            tool_kind = 'client' if kind in ('function_call', 'function_result') else 'server'
            self.put(key, 'tool_call' if kind.endswith('_call') else 'tool_result', text,
                     status='failed' if is_err else status,
                     tool_name=str(step.get('server_name') or step.get('name') or step.get('tool_name') or kind),
                     call_id=str(step.get('call_id') or step.get('id') or ''), provider_type=kind,
                     payload=clean_payload, tool_kind=tool_kind)

    def consume(self, data, sse_type='', event_id=None):
        if not isinstance(data, dict): return
        event_id = event_id or data.get('event_id')
        if not isinstance(event_id, str) or len(event_id) > 256:
            event_id = None
        if event_id:
            if event_id in self.seen_events: return
            self.seen_events.add(event_id)
            self.event_ids.append(event_id)
            if len(self.event_ids) > 20000:
                self.seen_events.discard(self.event_ids.popleft())
        kind = data.get('event_type') or sse_type
        # SSE is an optional low-latency cache. Poll snapshots remain authoritative.
        index = data.get('index')
        if kind in ('step.start', 'step.delta', 'step.stop') and type(index) is int and 0 <= index < 10000:
            self.usage['agentStepCount'] = max(self.usage.get('agentStepCount', 0), index + 1)
        if kind == 'step.start' and type(index) is int and 0 <= index < 10000:
            step = data.get('step') or {}
            self.step(index, step, 'running')
            if len(self.steps) < 128:
                self.steps[index] = {**step, 'type': step.get('type', ''), 'summary': '', 'text': ''}
        elif kind == 'step.delta' and type(index) is int and 0 <= index < 10000:
            delta = data.get('delta') or {}
            delta_type = delta.get('type', '')
            if index not in self.steps:
                if len(self.steps) >= 128:
                    return
                inferred = ('thought' if delta_type in ('thought_summary', 'thought_signature')
                            else 'function_call' if delta_type == 'arguments_delta'
                            else 'model_output' if delta_type in ('text', 'image', 'text_annotation_delta')
                            else delta_type)
                self.steps[index] = {'type': inferred, 'summary': '', 'text': ''}
            step = self.steps[index]
            if delta_type == 'thought_summary':
                content = delta.get('content') or {}
                if isinstance(content, dict) and content.get('type') == 'text':
                    previous = step.get('summary') or ''
                    if not isinstance(previous, str): previous = ''
                    combined = previous + content.get('text', '')
                    step['_truncated'] = bool(step.get('_truncated')) or len(combined.encode()) > PART_BYTES
                    step['summary'] = combined.encode()[:PART_BYTES].decode('utf-8', errors='ignore')
            elif step.get('type') == 'model_output' and delta_type == 'text':
                combined = step.get('text', '') + delta.get('text', '')
                step['_truncated'] = bool(step.get('_truncated')) or len(combined.encode()) > PART_BYTES
                step['text'] = combined.encode()[:PART_BYTES].decode('utf-8', errors='ignore')
            elif delta_type == 'arguments_delta':
                combined = (step.get('arguments') if isinstance(step.get('arguments'), str) else '') + delta.get('arguments', '')
                step['arguments'] = combined.encode()[:PART_BYTES].decode('utf-8', errors='ignore')
            elif delta_type == 'text_annotation_delta' and isinstance(delta.get('annotations'), list):
                anns = [{'title': a.get('title', ''), 'url': a.get('url', '')} for a in delta['annotations'] if isinstance(a, dict) and a.get('url')]
                if anns:
                    self.put(f'step:{index}:annotations', 'annotations', json.dumps(anns, ensure_ascii=False), status='running', provider_type='annotations')
            elif step.get('type', '').endswith(('_call', '_result')):
                for k, v in delta.items():
                    if k != 'type':
                        step[k] = v
            self.step(index, step, 'running')
        elif kind == 'step.stop' and index in self.steps:
            self.step(index, data.get('step') or self.steps.pop(index))
            self.steps.pop(index, None)
        elif kind and kind.startswith('interaction.') and isinstance(data.get('interaction'), dict):
            self.snapshot(data['interaction'])
        if event_id:
            self.last_event_id = event_id

    def save_spool(self):
        if not self.target: return
        self.target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target.with_suffix('.tmp')
        temporary.write_text(json.dumps({'jobId': self.job_id, 'messageId': self.message_id,
            'sequence': self.sequence, 'parts': self.parts, 'usage': self.usage, 'pending': self.pending,
            'identity': self.identity}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.target)

    async def flush(self, *, force=False):
        if self.closed or not self.post: return
        async with self.lock:
            # At most four 60 KiB parts per request, below the server's 256 KiB limit.
            keys = sorted(self.dirty, key=lambda key: self.parts[key]['sequence'])
            usage_changed = self.usage != self._last_flushed_usage
            if not keys and self.pending is None and not usage_changed and not force:
                return
            while True:
                if self.pending is None:
                    batch = []
                    body = {**self.identity, 'sequence': self.sequence + 1, 'parts': batch, 'usage': self.usage.copy()}
                    for key in keys[:4]:
                        batch.append(self.parts[key].copy())
                        if len(json.dumps(body, ensure_ascii=False).encode()) > 250 * 1024:
                            batch.pop()
                            break
                    self.pending = body
                self.save_spool()
                response = await self.post(self.pending)
                self.sequence = self.pending['sequence']
                self._last_flushed_usage = self.pending['usage'].copy()
                for saved in self.pending['parts']:
                    if self.parts.get(saved['key']) == saved:
                        self.dirty.discard(saved['key'])
                self.pending = None
                self.closed = bool((response or {}).get('closed'))
                self.save_spool()
                if self.closed: return
                keys = sorted(self.dirty, key=lambda key: self.parts[key]['sequence'])
                if not keys: break

    async def pump(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.flush()
            except Exception as error:
                detail = self.redactor.redact(str(error))[:240]
                print(f'[agent.progress] upload deferred job={self.job_id} msg={self.message_id} '
                      f'seq={self.sequence} dirty={len(self.dirty)} error_type={type(error).__name__} detail={detail}', flush=True)

    def finalize(self, result):
        usage = result.get('usage') or {}
        self.usage.update({key: value for key, value in usage.items() if key in {*USAGE.values(), 'agentStepCount'} and type(value) is int and value >= 0})
        if result.get('reply') and not any(p['type'] == 'text' for p in self.parts.values()):
            self.put('answer', 'text', result['reply'], truncated=len(result['reply'].encode()) > PART_BYTES)
        if result.get('annotations'):
            self.put('annotations', 'annotations', json.dumps(result['annotations'], ensure_ascii=False), provider_type='annotations')
        self.put('agent-status', 'status', result.get('error') or result.get('finishReason') or 'completed',
                 status='failed' if result.get('error') else 'completed')


async def observe_progress(client, url, key, progress, *, sleep=asyncio.sleep):
    from agent_trace import sse_events
    import httpx
    # The owner cancels this observer when polling reaches a terminal state. A
    # lifetime reconnect budget silently drops all later steps of long agents:
    # nonterminal GET snapshots may contain only status, without steps or usage.
    failures = 0
    while not progress.closed:
        params = {'stream': 'true'}
        if progress.last_event_id: params['last_event_id'] = progress.last_event_id
        received = False
        try:
            async with client.stream('GET', url, params=params, headers={'x-goog-api-key': key, 'Accept': 'text/event-stream'},
                                     timeout=httpx.Timeout(45, connect=15)) as response:
                if response.status_code != 200:
                    if response.status_code not in (408, 429, 500, 502, 503, 504):
                        print(f'[agent.progress] stream unavailable job={progress.job_id} '
                              f'msg={progress.message_id} http={response.status_code}', flush=True)
                        return
                elif 'text/event-stream' not in response.headers.get('content-type', ''):
                    print(f'[agent.progress] stream unavailable job={progress.job_id} '
                          f'msg={progress.message_id} reason=non_sse_response', flush=True)
                    return
                else:
                    async for data, kind, event_id in sse_events(response):
                        received = True
                        progress.consume(data, kind, event_id)
                        failures = 0
                        event_type = (data.get('event_type') or kind) if isinstance(data, dict) else kind
                        if event_type in ('interaction.completed', 'interaction.failed', 'interaction.cancelled',
                                          'interaction.canceled', 'interaction.incomplete', 'interaction.requires_action'):
                            return
        except (httpx.HTTPError, ValueError, UnicodeError) as error:
            # Exception text/URLs can contain credentials; log the type only.
            print(f'[agent.progress] stream interrupted job={progress.job_id} '
                  f'msg={progress.message_id} error_type={type(error).__name__}', flush=True)
        if received and not progress.last_event_id:
            # Replaying cursorless deltas could duplicate content. An initial
            # connection failure with no events, however, is safe to retry.
            print(f'[agent.progress] stream unavailable job={progress.job_id} '
                  f'msg={progress.message_id} reason=missing_event_cursor', flush=True)
            return
        failures = min(failures + 1, 5)
        delay = min(2 ** failures, 30)
        print(f'[agent.progress] stream reconnecting job={progress.job_id} '
              f'msg={progress.message_id} delay_seconds={delay} resumed={bool(progress.last_event_id)}', flush=True)
        await sleep(delay)
