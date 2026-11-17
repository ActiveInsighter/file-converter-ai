"""Bounded, redacted diagnostics for an existing interaction; all provider calls are GET.

Step timings measure event arrival, not historical execution time during replay.
Only provider-labelled thought summaries are emitted, never signatures/raw thoughts.
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, deque
from datetime import datetime, timezone
import json
import os
import re
import time

import httpx

MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW = 1800
MAX_CAPTURE = 16000
TERMINAL = {'completed', 'failed', 'incomplete', 'requires_action', 'cancelled', 'canceled'}


class AgentTrace:
    def __init__(self, *, job='', segment=None, secrets=(), clock=time.monotonic, sink=print,
                 verbose=True, prefix='[agent.trace]'):
        self.clock, self.sink = clock, sink
        self.verbose, self.prefix = verbose, prefix
        self.started = self.last_event = clock()
        self.context = {'job': job, 'segment': segment}
        values = [*secrets]
        for name, value in os.environ.items():
            if any(word in name.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
                values.extend(value.splitlines())
        self.secrets = sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)
        self.secrets += [base64.b64encode(f'x-oauth-basic:{v}'.encode()).decode() for v in self.secrets]
        self.steps = {}
        self.pending_tools = []
        self.events = Counter()
        self.event_ids, self.seen_ids = deque(), set()
        self.last_event_id = None
        self.status = 'unknown'
        self.content_budget = 1024 * 1024

    def redact(self, text):
        text = str(text)
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_-]{20,})', '[REDACTED]', text)
        text = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)((?:password|authorization|api[_-]?key|access[_-]?token|token|secret)["\x27]?\s*[:=]\s*["\x27]?)[^\s,"\x27;&}]+', r'\1[REDACTED]', text)
        text = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', text)
        # GitHub Actions interprets leading :: as workflow commands; JSON-escape all text.
        return text

    def preview(self, value):
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        def scrub(item, depth=0):
            if depth > 8:
                return '[nested value omitted]'
            if isinstance(item, dict):
                return {k: ('[REDACTED]' if re.search(r'(?i)authorization|password|secret|token|api[_-]?key', str(k))
                            else scrub(v, depth + 1)) for k, v in item.items()}
            if isinstance(item, list):
                return [scrub(v, depth + 1) for v in item]
            if isinstance(item, str):
                if item.lstrip().startswith(('{', '[')):
                    try:
                        return scrub(json.loads(item), depth + 1)
                    except ValueError:
                        pass
                return self.redact(item)
            return item
        safe = scrub(value)
        clean = self.redact(safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, default=str))
        preview = clean[:MAX_PREVIEW].encode('utf-8')[:self.content_budget].decode('utf-8', errors='ignore')
        self.content_budget -= len(preview.encode('utf-8'))
        return {'chars': len(raw), 'preview': preview, 'truncated': len(clean) > len(preview)}

    def emit(self, event, **fields):
        if not self.verbose and not (event.endswith(('_error', '_failed'))
                                     or event == 'create_http' and fields.get('http', 0) >= 400):
            return
        row = {'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
               'event': event, **self.context, 'elapsed_seconds': round(self.clock() - self.started, 3), **fields}
        def clean(value):
            if isinstance(value, str):
                return self.redact(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value
        self.sink(self.prefix + ' ' + json.dumps(clean(row), ensure_ascii=False, separators=(',', ':')), flush=True)

    def summary(self, reason):
        longest = sorted(({'index': i, 'type': s['type'], 'name': s.get('name'),
                           'observed_seconds': round(s.get('stopped', self.clock()) - s['started'], 3)}
                          for i, s in self.steps.items()), key=lambda s: s['observed_seconds'], reverse=True)[:10]
        self.emit('trace_summary', reason=reason, status=self.status, counts=dict(self.events),
                  idle_seconds=round(self.clock() - self.last_event, 3), longest_observed_steps=longest,
                  timing_basis='local event arrival; replay is not historical execution duration')

    def pending(self):
        return [{'index': s['index'], 'type': s['type'], 'name': s['name'],
                 'observed_seconds': round(self.clock() - s['started'], 3)} for s in self.pending_tools][-10:]

    def _details(self, step):
        # Only explicit tool fields. Do not dump a thought step or arbitrary interaction.
        result = {}
        for key in ('name', 'server_name', 'tool_name', 'language', 'call_id', 'id', 'status', 'is_error', 'exit_code'):
            if key in step:
                result[key] = str(step[key])[:200]
        if step.get('type', '').endswith(('_call', '_result')):
            for key in ('arguments', 'code', 'command', 'input', 'output', 'result', 'content', 'error', 'stdout', 'stderr', 'results', 'response'):
                if key in step:
                    result[key] = self.preview(step[key])
        return result

    def consume(self, data, sse_type='', event_id=None):
        if not isinstance(data, dict):
            return
        event_id = data.get('event_id') or event_id
        if isinstance(event_id, str) and event_id:
            if event_id in self.seen_ids:
                return
            self.last_event_id = event_id
            self.seen_ids.add(event_id)
            self.event_ids.append(event_id)
            if len(self.event_ids) > 10000:
                self.seen_ids.discard(self.event_ids.popleft())
        kind = data.get('event_type') or sse_type or 'unknown'
        if not isinstance(kind, str):
            return
        self.events[kind] += 1
        self.last_event = self.clock()
        index = data.get('index')
        if not isinstance(index, int) or not 0 <= index < 100000:
            index = None
        interaction = data.get('interaction') or {}
        if not isinstance(interaction, dict):
            interaction = {}
        if kind.startswith('interaction.') or kind == 'error':
            self.status = data.get('status') or interaction.get('status') or self.status
            fields = {'type': kind, 'status': self.status}
            if isinstance(interaction.get('usage'), dict):
                fields['usage'] = {k: v for k, v in interaction['usage'].items()
                                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
            for key in ('error', 'incomplete_details'):
                if data.get(key) or interaction.get(key):
                    fields[key] = self.preview(data.get(key) or interaction.get(key))
            self.emit('interaction_event', **fields)
        elif kind == 'step.start' and index is not None:
            step = data.get('step') or {}
            if not isinstance(step, dict):
                return
            step_type = str(step.get('type', 'unknown'))[:80]
            self.steps[index] = {'type': step_type, 'started': self.clock(), 'capture': '', 'summary': '', 'delta_chars': 0,
                                 'name': step.get('name') or step.get('tool_name'),
                                 'call_id': step.get('call_id') or step.get('id')}
            if step_type.endswith('_call'):
                self.pending_tools.append({'index': index, **self.steps[index]})
            self.emit('step_start', index=index, type=step_type, **self._details(step))
        elif kind == 'step.delta' and index is not None:
            delta = data.get('delta') or {}
            if not isinstance(delta, dict):
                return
            step = self.steps.get(index)
            if not step:
                return
            if delta.get('type') == 'thought_summary':
                content = delta.get('content') or {}
                if isinstance(content, dict) and content.get('type') == 'text':
                    text = content.get('text', '')
                    if isinstance(text, str):
                        step['summary'] += text[:max(0, MAX_CAPTURE - len(step['summary']))]
            elif step['type'].endswith(('_call', '_result')):
                # Keep split secrets together and redact before logging at step.stop.
                for key in ('arguments', 'text', 'code', 'output', 'result', 'content'):
                    value = delta.get(key)
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    if isinstance(value, str):
                        step['delta_chars'] += len(value)
                        if len(step['capture']) < MAX_CAPTURE:
                            step['capture'] += value[:MAX_CAPTURE - len(step['capture'])]
            # Never emit thought_signature, raw thought text, or model-output deltas.
        elif kind == 'step.stop' and index in self.steps:
            step = self.steps[index]
            step['stopped'] = self.clock()
            if step['summary']:
                self.emit('thought_summary', index=index, **self.preview(step['summary']))
            details = {'tool_payload': self.preview(step['capture']), 'delta_chars': step['delta_chars']} if step['capture'] else {}
            final = data.get('step')
            if isinstance(final, dict):
                details.update(self._details({**final, 'type': step['type']}))
            self.emit('step_stop', index=index, type=step['type'],
                      observed_seconds=round(step['stopped'] - step['started'], 3), **details)
            if step['type'].endswith('_result'):
                call_id = step['call_id'] or (final.get('call_id') if isinstance(final, dict) else None)
                candidates = [s for s in self.pending_tools if s['type'] == step['type'][:-7] + '_call']
                matched = next((s for s in candidates if call_id and s['call_id'] == call_id), None)
                association = 'call_id' if matched else 'type_fifo_estimate'
                matched = matched or next(iter(candidates), None)
                if matched:
                    self.pending_tools.remove(matched)
                    self.emit('tool_finished', call_index=matched['index'], result_index=index,
                              type=matched['type'], name=matched['name'], association=association,
                              observed_seconds=round(self.clock() - matched['started'], 3))

    def snapshot(self, data):
        self.status = str(data.get('status', 'unknown'))
        steps = data.get('steps') or []
        self.emit('snapshot', status=self.status, step_count=len(steps),
                  provider_created=data.get('created'), provider_updated=data.get('updated'),
                  **{key: self.preview(data[key]) for key in ('error', 'incomplete_details') if data.get(key)},
                  usage={k: v for k, v in (data.get('usage') or {}).items() if type(v) is int})
        for index, step in enumerate(steps):
            if isinstance(step, dict):
                self.emit('snapshot_step', index=index, type=str(step.get('type', 'unknown'))[:80],
                          timing_basis='snapshot; no execution duration available', **self._details(step))


async def sse_events(response):
    """Parse bounded SSE frames across arbitrary UTF-8/CRLF chunk boundaries."""
    buffer = bytearray()
    frame = []
    frame_bytes = 0
    async for block in response.aiter_bytes():
        buffer.extend(block)
        while b'\n' in buffer:
            line, _, tail = buffer.partition(b'\n')
            buffer = bytearray(tail)
            line = line.rstrip(b'\r')
            frame_bytes += len(line)
            if frame_bytes > MAX_EVENT_BYTES:
                raise ValueError('SSE event exceeds 2 MiB')
            if line:
                frame.append(line.decode('utf-8'))
            else:
                event = decode_frame(frame)
                if event:
                    yield event
                frame, frame_bytes = [], 0
        if len(buffer) + frame_bytes > MAX_EVENT_BYTES:
            raise ValueError('SSE event exceeds 2 MiB')
    if buffer:
        frame.append(bytes(buffer).rstrip(b'\r').decode('utf-8'))
    event = decode_frame(frame)
    if event:
        yield event


def decode_frame(lines):
    fields, payload = {}, []
    for line in lines:
        if line.startswith(':'):
            continue
        name, _, value = line.partition(':')
        value = value[1:] if value.startswith(' ') else value
        if name == 'data':
            payload.append(value)
        elif name in ('event', 'id'):
            fields[name] = value
    text = '\n'.join(payload)
    if not text or text.strip() == '[DONE]':
        return None
    return json.loads(text), fields.get('event', ''), fields.get('id')


async def observe_stream(client, url, key, trace, *, sleep=asyncio.sleep):
    """Best effort side channel. It never creates/cancels/interferes with the agent."""
    for attempt in range(1, 7):
        params = {'stream': 'true'}
        if trace.last_event_id:
            params['last_event_id'] = trace.last_event_id
        began = trace.clock()
        try:
            async with client.stream('GET', url, params=params,
                                     headers={'x-goog-api-key': key, 'Accept': 'text/event-stream'},
                                     timeout=httpx.Timeout(45, connect=15)) as response:
                content_type = response.headers.get('content-type', '')
                trace.emit('stream_connected', attempt=attempt, http=response.status_code,
                           connect_seconds=round(trace.clock() - began, 3),
                           resumed=bool(trace.last_event_id), content_type=content_type)
                if response.status_code != 200:
                    if response.status_code not in (429, 500, 502, 503, 504):
                        return
                elif 'text/event-stream' not in content_type:
                    trace.emit('stream_unavailable', reason='provider did not return SSE')
                    return
                else:
                    async for data, event_type, event_id in sse_events(response):
                        trace.consume(data, event_type, event_id)
                        if (data.get('event_type', event_type).startswith('interaction.')
                                and trace.status in TERMINAL):
                            return
                    trace.emit('stream_disconnected', attempt=attempt, reason='EOF')
        except (httpx.HTTPError, ValueError, UnicodeError) as error:
            # HTTP exception strings may contain a credential-bearing URL.
            trace.emit('stream_error', attempt=attempt, error_type=type(error).__name__)
        await sleep(min(2 ** attempt, 20))
    trace.emit('stream_unavailable', reason='reconnect attempts exhausted; polling continues')


async def watch_interaction(client, url, key, trace):
    task = asyncio.create_task(observe_stream(client, url, key, trace))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=20)
            if not done:
                trace.emit('stream_heartbeat', status=trace.status,
                           idle_seconds=round(trace.clock() - trace.last_event, 3),
                           pending_tools=trace.pending(),
                           active_steps=[{'index': i, 'type': s['type'], 'name': s['name'],
                                          'observed_seconds': round(trace.clock() - s['started'], 3)}
                                         for i, s in trace.steps.items() if 'stopped' not in s][-10:])
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
