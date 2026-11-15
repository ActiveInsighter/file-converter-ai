"""Antigravity managed-agent transport. Never send an agent to generateContent.

Background POSTs are never retried after an ambiguous transport failure. Once an
interaction ID exists, retries only GET that ID, using the same API credential.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from urllib.parse import quote

import httpx

from agent_trace import AgentTrace, watch_interaction

ROOT = 'https://generativelanguage.googleapis.com/v1beta/interactions'
DEFAULT_AGENT = 'antigravity-preview-09-2026'
DEFAULT_MODEL = 'gemini-4-hydrogen'
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024
GITHUB_TASK = re.compile(r'github|(?<![a-z])gh(?![a-z])|git(?:hub)?\b|repo(?:sitory)?\b|pull.request|'
                         r'仓库|代码库|项目源码|项目代码|拉取代码|提交代码|推送代码|克隆|分支', re.I)


def agent_telemetry(event, **fields):
    """Metadata only: no prompts, interaction IDs, credentials, or tool payloads."""
    print('[agent.telemetry] ' + json.dumps({'event': event, **fields}, ensure_ascii=False, separators=(',', ':')), flush=True)


def agent_usage_diagnostics(data):
    """Google's cumulative usage; never sum multiple polling responses."""
    raw = data.get('usage') or {}
    keys = ('total_tokens', 'total_input_tokens', 'total_output_tokens', 'total_thought_tokens',
            'total_tool_use_tokens', 'total_cached_tokens')
    usage = {k: raw[k] for k in keys if type(raw.get(k)) is int and raw[k] >= 0}
    kinds = {}
    for step in data.get('steps', []):
        if not isinstance(step, dict):
            continue
        kind = step.get('type', '')
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z_]{1,48}', kind):
            kind = 'other'
        kinds[kind] = kinds.get(kind, 0) + 1
    return {'usage': usage, 'step_count': sum(kinds.values()), 'step_types': kinds}


class AgentCanceled(RuntimeError):
    pass


def is_antigravity(model):
    value = str(model).casefold()
    return value == 'antigravity' or value.startswith('antigravity-')


def build_agent_payload(job, message):
    options = job.get('options', {}).get('agent', {})
    name = str(job['model']).lower()
    payload = {
        'agent': options.get('id') or (DEFAULT_AGENT if name == 'antigravity' else name),
        'input': '\n\n'.join(value for value in [job.get('prompt', ''), message.get('inputText', '')] if value),
        'background': True, 'store': True,
        'environment': {'type': 'remote'},
        'tools': [{'type': name} for name in ['code_execution', 'google_search', 'url_context']],
        'agent_config': {'type': 'antigravity', 'model': options.get('model', DEFAULT_MODEL)},
    }
    if job.get('systemPrompt'):
        payload['system_instruction'] = job['systemPrompt']
    # A configured GitHub PAT grants access to every repository permitted by
    # that PAT, not an application-defined list. Clone repositories on demand.
    # User content cannot override the authentication rules or MCP endpoint.
    github_token = os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '').strip()
    task_text = '\n'.join(str(x or '') for x in (job.get('prompt'), message.get('inputText')))
    if github_token and GITHUB_TASK.search(task_text):
        basic = base64.b64encode(f'x-oauth-basic:{github_token}'.encode()).decode()
        payload['environment'] = {
            'type': 'remote',
            'network': {
                'allowlist': [
                    {'domain': 'github.com', 'transform': {'Authorization': f'Basic {basic}'}},
                    {'domain': 'api.github.com', 'transform': {'Authorization': f'Bearer {github_token}'}},
                    {'domain': '*'},
                ],
            },
        }
        payload['tools'].append({
            'type': 'mcp_server', 'name': 'github',
            'url': 'https://api.githubcopilot.com/mcp',
            'headers': {'Authorization': f'Bearer {github_token}'},
        })
        github_guide = (
            'GitHub is available through the github MCP and authenticated HTTPS Git '
            'and REST APIs. Use the MCP to discover repositories accessible to the '
            'configured token, then clone only the repository needed for the '
            'requested task into /workspace. You may read or write any repository '
            'that GitHub grants this token access to; do not assume a fixed two-repo '
            'list. Before editing, read the repository AGENTS.md when present; '
            'prefer a feature branch and pull request to pushing main. For read-only '
            'permission checks, make only a few lightweight MCP requests. Do not '
            'clone or scan all repositories or enumerate issues unless explicitly '
            'asked. Stop as soon as you can answer the question.'
        )
        payload['system_instruction'] = '\n\n'.join(
            part for part in [payload.get('system_instruction', ''), github_guide] if part
        )
    return payload


def parse_interaction(data):
    reply = data.get('output_text')
    if not isinstance(reply, str):
        reply = '\n\n'.join(
            part['text'] for step in data.get('steps', []) if step.get('type') == 'model_output'
            for part in step.get('content', []) if part.get('type') == 'text' and isinstance(part.get('text'), str)
        )
    # Compatibility with the older Interactions response shape.
    if not reply and isinstance(data.get('outputs'), list):
        reply = '\n'.join(part['text'] for part in data['outputs'] if part.get('type') == 'text' and isinstance(part.get('text'), str))
    if len(reply.encode()) > MAX_REPLY_BYTES:
        raise RuntimeError('Agent 回复超过 2 MiB，执行结果已保留在原 interaction 中。')
    status = data.get('status', '')
    error = ''
    if status == 'incomplete':
        error = 'Agent 执行未完成，已保留现有回复；请检查上游状态或服务端限制。'
    elif status in ['failed', 'requires_action']:
        detail = data.get('error') or {}
        message = detail.get('message', '') if isinstance(detail, dict) else str(detail)
        error = 'Agent 执行失败：' + (message[:1000] or ('需要外部操作' if status == 'requires_action' else '上游未提供详情'))
    elif status in ['cancelled', 'canceled']:
        error = 'Agent 已取消'
    elif not reply.strip():
        error = 'Agent 返回空回复'
    usage = data.get('usage') or {}
    mapping = {'total_input_tokens': 'promptTokenCount', 'total_output_tokens': 'candidatesTokenCount',
               'total_thought_tokens': 'thoughtsTokenCount', 'total_cached_tokens': 'cachedContentTokenCount', 'total_tokens': 'totalTokenCount'}
    normalized = {target: usage[source] for source, target in mapping.items() if isinstance(usage.get(source), int) and usage[source] >= 0}
    calls = {}
    for step in data.get('steps', []):
        kind = step.get('type', '')
        if isinstance(kind, str) and kind.endswith('_call') and len(kind) <= 80:
            calls[kind] = calls.get(kind, 0) + 1
    if calls:
        normalized['agentToolCalls'] = calls
    normalized['agentStepCount'] = len(data.get('steps', []))
    if type(usage.get('total_tool_use_tokens')) is int and usage['total_tool_use_tokens'] >= 0:
        normalized['toolUseTokenCount'] = usage['total_tool_use_tokens']
    return {'reply': reply, 'error': error, 'finishReason': status.upper()[:64], 'usage': normalized}


async def request_json(client, method, url, key, payload=None):
    async with client.stream(method, url, headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                             json=payload, timeout=90) as response:
        raw = bytearray()
        async for block in response.aiter_bytes():
            raw.extend(block)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError('Agent 响应超过 6 MiB，原 interaction 保留在上游。')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError(f'Agent 返回非 JSON 响应（HTTP {response.status_code}）') from None
        return response.status_code, response.headers, data


def upstream_error(status, data, key):
    error = data.get('error') or {}
    detail = error.get('message', '') if isinstance(error, dict) else str(error)
    detail = str(detail).replace(key, '[REDACTED]')
    github_token = os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '').strip()
    if github_token:
        basic = base64.b64encode(f'x-oauth-basic:{github_token}'.encode()).decode()
        detail = detail.replace(github_token, '[REDACTED]').replace(basic, '[REDACTED]')
    detail = detail[:1200]
    if status == 429 and 'free tier' in detail.casefold() and 'limit: 0' in detail.casefold():
        detail = ('当前 API 项目的所选模型免费额度为 0。请使用有该模型 Agent 额度的 API Key，'
                  '或在 Google 项目中配置结算并确认可用额度。上游详情：' + detail)
    return RuntimeError(f'Antigravity 请求失败（HTTP {status}）：{detail or "请检查 Agent、参数及 API Key 权限"}')


async def generate_agent(client, pool, job, message, *, checkpoint, sleep=asyncio.sleep, clock=time.monotonic):
    from providers import GeminiProvider
    provider = GeminiProvider()
    payload = build_agent_payload(job, message)
    started = clock()
    trace = AgentTrace(job=job.get('id', os.environ.get('REQUEST_ID', '')),
                       segment=message.get('index'), secrets=pool.keys, clock=clock)
    original_checkpoint = checkpoint
    checkpoint_report = started
    async def checkpoint(update=None):
        nonlocal checkpoint_report
        began = clock()
        try:
            return await original_checkpoint(update)
        finally:
            if clock() - checkpoint_report >= 30 or clock() - began >= 1:
                trace.emit('checkpoint_timing', seconds=round(clock() - began, 3), writing=update is not None)
                checkpoint_report = clock()
    state = (await checkpoint()).get('state') or {}
    agent_telemetry('prepared',
        model=payload['agent_config']['model'],
        tools=[tool.get('type') for tool in payload['tools']],
        github_enabled=any(tool.get('type') == 'mcp_server' for tool in payload['tools']),
        input_chars=len(payload['input']),
        system_chars=len(payload.get('system_instruction', '')),
        resumed=bool(state.get('id')))
    index = key = lease = None
    current = None
    for attempt in range(max(1, len(pool.keys))):
        if not state.get('id') and (await checkpoint()).get('canceled'):
            raise AgentCanceled('Agent 已取消，未创建远程执行。')
        acquire_started = clock()
        index, key, lease = await pool.acquire()
        trace.emit('credential_acquired', wait_seconds=round(clock() - acquire_started, 3), key_index=index)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if state.get('id'):
            # Resume the original remote execution, never create a second interaction.
            if state.get('credentialHash') != fingerprint:
                await pool.mark_cancelled(index, lease)
                pool.retire_key(index, 'different interaction credential')
                continue
            current = {'id': state['id'], 'status': 'in_progress'}
            break
        try:
            create_started = clock()
            status, headers, data = await request_json(client, 'POST', ROOT, key, payload)
            trace.emit('create_http', http=status, seconds=round(clock() - create_started, 3),
                       attempt=attempt + 1,
                       **({'error': trace.preview(data['error'])} if data.get('error') else {}))
        except (httpx.TransportError, TimeoutError) as error:
            trace.emit('create_transport_error', error_type=type(error).__name__,
                       seconds=round(clock() - create_started, 3), resent=False)
            await pool.mark_error(index, lease)
            raise RuntimeError('Agent 提交连接中断，无法确认是否已创建；为避免重复执行，未自动重发。') from None
        if status in [200, 201, 202]:
            current = data
            if not isinstance(data.get('id'), str) or not data['id']:
                raise RuntimeError('Agent 未返回 interaction ID；未自动重发。')
            agent_telemetry('created', elapsed_seconds=round(clock() - started, 2),
                            status=data.get('status', 'unknown'),
                            environment_created=bool(data.get('environment_id')))
            state = {'id': data['id'], 'credentialHash': fingerprint, 'status': data.get('status', 'in_progress')}
            if isinstance(data.get('environment_id'), str):
                state['environmentId'] = data['environment_id']
            try:
                await checkpoint(state)
            except Exception:
                # Stop a remote run whose identity cannot be durably recorded.
                await request_json(client, 'POST', f'{ROOT}/{quote(data["id"], safe="")}:cancel', key)
                raise
            break
        await pool.mark_error(index, lease, http_status=status)
        print(f'[agent] create rejected attempt={attempt + 1}/{len(pool.keys)} HTTP={status}', flush=True)
        scope = provider.credential_error_scope(status, data)
        if scope:
            pool.retire_key(index, 'agent access unavailable')
        elif status == 429:
            await pool.rate_limited(index, lease, provider.retry_after(headers, data),
                                    daily_exhausted=provider.quota_kind(data) in ['rpd', 'spend'])
        else:
            # A definite rejected POST is safe to report, without guessing other IDs.
            raise upstream_error(status, data, key)
        if attempt + 1 >= max(1, len(pool.keys)):
            raise upstream_error(status, data, key)
    if current is None:
        raise RuntimeError('无法找到原 Agent interaction 使用的 API Key；未创建重复执行。')

    url = f'{ROOT}/{quote(current["id"], safe="")}'
    # Independent, read-only event stream; polling remains authoritative for control/results.
    stream_task = asyncio.create_task(watch_interaction(client, url, key, trace))
    deadline = clock() + job.get('options', {}).get('agent', {}).get('timeoutMinutes', 175) * 60
    failures = 0
    poll_count = 0
    last_report = started
    last_status = None
    try:
        while True:
            if current.get('status') in ['completed', 'failed', 'incomplete', 'requires_action', 'cancelled', 'canceled']:
                await checkpoint({'status': current['status'], **({'environmentId': current['environment_id']} if current.get('environment_id') else {})})
                await pool.mark_success(index, lease)
                result = parse_interaction(current)
                trace.snapshot(current)
                agent_telemetry('completed',
                    status=current['status'],
                    elapsed_seconds=round(clock() - started, 2),
                    polls=poll_count,
                    reply_chars=len(result['reply']),
                    **agent_usage_diagnostics(current))
                return result
            control = await checkpoint()
            if control.get('canceled'):
                await request_json(client, 'POST', url + ':cancel', key)
                await checkpoint({'status': 'cancelled'})
                await pool.mark_cancelled(index, lease)
                raise AgentCanceled('Agent 已取消')
            if clock() >= deadline:
                await request_json(client, 'POST', url + ':cancel', key)
                await checkpoint({'status': 'cancelled'})
                raise RuntimeError('Agent 达到执行时限，已取消远程执行。')
            await sleep(3)
            try:
                poll_started = clock()
                status, _, data = await request_json(client, 'GET', url, key)
                if status != 200:
                    trace.emit('poll_http_error', http=status, consecutive_failures=failures + 1,
                               seconds=round(clock() - poll_started, 3),
                               **({'error': trace.preview(data['error'])} if data.get('error') else {}))
                    if status in [429, 500, 502, 503, 504]:
                        failures += 1
                        if failures <= 20:
                            await sleep(min(failures * 2, 30))
                            continue
                    raise upstream_error(status, data, key)
                current, failures = data, 0
                poll_count += 1
                # Heartbeat and cumulative counters, never the response body or thoughts.
                if current.get('status') != last_status or clock() - last_report >= 30:
                    summary = agent_usage_diagnostics(current)
                    agent_telemetry('progress', elapsed_seconds=round(clock() - started, 2),
                                    polls=poll_count, status=current.get('status', 'unknown'),
                                    **summary)
                    trace.emit('poll_timing', http=status, seconds=round(clock() - poll_started, 3),
                               polls=poll_count, status=current.get('status', 'unknown'), **summary)
                    last_status = current.get('status')
                    last_report = clock()
            except (httpx.TransportError, TimeoutError) as error:
                failures += 1
                trace.emit('poll_transport_error', error_type=type(error).__name__, consecutive_failures=failures)
                if failures > 20:
                    raise RuntimeError('Agent 状态连接持续失败，interaction 已保留，未重复创建。') from None
                await sleep(min(failures * 2, 30))
    except asyncio.CancelledError:
        try:
            await asyncio.shield(request_json(client, 'POST', url + ':cancel', key))
        finally:
            await pool.mark_cancelled(index, lease)
        raise
    except AgentCanceled:
        raise
    except Exception:
        # Do not leave an unmonitored agent spending tokens after a local failure.
        try:
            await request_json(client, 'POST', url + ':cancel', key)
        except Exception:
            pass
        raise
    finally:
        # A diagnostic failure must never alter execution or hide the actual result.
        stream_task.cancel()
        await asyncio.gather(stream_task, return_exceptions=True)
        trace.summary('executor finished monitoring')
