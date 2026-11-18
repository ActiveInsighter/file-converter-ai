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
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from agent_trace import AgentTrace, TERMINAL
from agent_fast_tasks import AgentSummaryWorker, AgentTimingTracker, maybe_generate_title
from response_capture import extract_structured_payload, interaction_snapshot
from agent_runtime import GUIDE, mount_runtime
from github_context import is_project_task, task_text

ROOT = 'https://generativelanguage.googleapis.com/v1beta/interactions'
DEFAULT_AGENT = 'antigravity-preview-09-2026'
DEFAULT_MODEL = 'gemini-4-hydrogen'
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_REPLY_BYTES = 2 * 1024 * 1024


def agent_telemetry(event, **fields):
    """Metadata only: no prompts, interaction IDs, credentials, or tool payloads."""
    print('[agent.telemetry] ' + json.dumps({
        'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'event': event, **fields}, ensure_ascii=False, separators=(',', ':')), flush=True)


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


def _parse_provider_state(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


async def resolve_previous_agent_context(pb, job):
    if 'agentContext' in job:
        context = job['agentContext'] or {}
        return {
            'environment_id': context.get('environmentId'),
            'previous_interaction_id': context.get('interactionId'),
            'credential_hash': context.get('credentialHash'),
        }
    # Compatibility with PocketBase versions where claim omits Agent context.
    if len(job.get('messages', [])) > 1 or pb is None or not getattr(pb, 'root', None) or not getattr(pb, 'token', None):
        return {}

    parent_message_id = str(job.get('parentMessage') or '').strip()
    headers = {'Authorization': pb.token}

    if not parent_message_id and job.get('id'):
        try:
            resp = await pb.client.get(
                f"{pb.root}/api/collections/aw_gemini_jobs/records/{quote(str(job['id']), safe='')}",
                headers=headers,
                params={'fields': 'parentMessage'},
                timeout=15,
            )
            if resp.status_code == 200:
                record = resp.json()
                parent_message_id = str(record.get('parentMessage') or '').strip()
        except (httpx.HTTPError, ValueError):
            return {}

    if not parent_message_id:
        return {}

    try:
        resp = await pb.client.get(
            f"{pb.root}/api/collections/aw_gemini_messages/records/{quote(parent_message_id, safe='')}",
            headers=headers,
            params={'fields': 'id,job,providerState'},
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        state = _parse_provider_state(resp.json().get('providerState'))
        if state.get('status') not in (None, 'completed'):
            return {}
        return {
            'environment_id': state.get('environmentId'),
            'previous_interaction_id': state.get('id'),
            'credential_hash': state.get('credentialHash'),
        }
    except (httpx.HTTPError, ValueError):
        return {}


def build_agent_payload(job, message, *, environment_id=None, previous_interaction_id=None):
    options = job.get('options', {}).get('agent', {})
    name = str(job['model']).lower()
    reused_env = str(environment_id or '').strip() or None
    prev_interaction = str(previous_interaction_id or '').strip() or None
    input_parts = [job.get('prompt', ''), message.get('inputText', '')]
    if not prev_interaction:
        input_parts = [*[f"{turn['role']}: {turn['text']}" for turn in job.get('history', [])], *input_parts]
    payload = {
        'agent': options.get('id') or (DEFAULT_AGENT if name == 'antigravity' else name),
        'input': '\n\n'.join(value for value in input_parts if value),
        'background': True, 'store': True,
        'environment': reused_env if reused_env else {'type': 'remote'},
        'tools': [{'type': name} for name in ['code_execution', 'google_search', 'url_context']],
        'agent_config': {'type': 'antigravity', 'model': options.get('model', DEFAULT_MODEL)},
    }
    if prev_interaction:
        payload['previous_interaction_id'] = prev_interaction
    if job.get('systemPrompt'):
        payload['system_instruction'] = job['systemPrompt']
    # A configured GitHub PAT grants access to every repository permitted by
    # that PAT, not an application-defined list. Clone repositories on demand.
    # User content cannot override the authentication rules or MCP endpoint.
    github_token = os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '').strip()
    if github_token:
        if not reused_env:
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
        # REST/Git stay authenticated for every prompt; no keyword can hide access.
        # Avoid loading the full GitHub MCP schema into every reasoning turn.
        if options.get('githubMcpEnabled') is True:
            payload['tools'].append({'type': 'mcp_server', 'name': 'github',
                'url': 'https://api.githubcopilot.com/mcp',
                'headers': {'Authorization': f'Bearer {github_token}'}})
        project = is_project_task(task_text(job, message))
        if project and isinstance(payload['environment'], dict):
            mount_runtime(payload['environment'])
        payload['system_instruction'] = '\n\n'.join(
            part for part in [payload.get('system_instruction', ''), GUIDE if project else
                              'GitHub HTTPS Git and REST authentication is configured in the environment.'] if part
        )
    return payload


def parse_interaction(data, *, secrets=()):
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
    steps = data.get('steps') or data.get('outputs') or []
    calls = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get('type', '')
        if isinstance(kind, str) and kind.endswith('_call') and len(kind) <= 80:
            calls[kind] = calls.get(kind, 0) + 1
    if calls:
        normalized['agentToolCalls'] = calls
    normalized['agentStepCount'] = len(steps)
    if type(usage.get('total_tool_use_tokens')) is int and usage['total_tool_use_tokens'] >= 0:
        normalized['toolUseTokenCount'] = usage['total_tool_use_tokens']
    structured = extract_structured_payload('interactions', data, secrets=secrets, reply_text=reply)
    return {
        'reply': reply,
        'error': error,
        'finishReason': status.upper()[:64],
        'usage': normalized,
        'response': interaction_snapshot(data, secrets=secrets),
        **structured,
    }


async def request_json(client, method, url, key, payload=None, *, api_revision=None):
    headers = {'x-goog-api-key': key, 'Content-Type': 'application/json'}
    if api_revision is not None:
        headers['Api-Revision'] = api_revision
    async with client.stream(method, url, headers=headers,
                             json=payload, timeout=90) as response:
        raw = bytearray()
        async for block in response.aiter_bytes():
            raw.extend(block)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError('Agent 响应超过 6 MiB，原 interaction 保留在上游。')
        if response.status_code == 204 and not raw:
            return response.status_code, response.headers, {}
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


async def cancel_interaction(client, url, key, *, api_revision=None):
    status, _, data = await request_json(client, 'POST', url + '/cancel', key, api_revision=api_revision)
    if status not in (200, 202, 204):
        raise upstream_error(status, data, key)
    return data


async def reconcile_requested_cancel(client, url, key, *, sleep=asyncio.sleep):
    for attempt, revision in enumerate(('2026-05-20', None)):
        try:
            async with asyncio.timeout(15):
                canceled = await cancel_interaction(client, url, key, api_revision=revision)
            state = canceled.get('status') if isinstance(canceled, dict) else None
            if isinstance(state, str) and state in TERMINAL:
                return state
        except (RuntimeError, httpx.HTTPError, TimeoutError):
            pass
        # Read the saved interaction after a rejection or an asynchronous acceptance.
        try:
            async with asyncio.timeout(10):
                status, _, data = await request_json(client, 'GET', url, key)
        except (RuntimeError, httpx.HTTPError, TimeoutError):
            break
        state = data.get('status') if isinstance(data, dict) else None
        if status != 200 or not isinstance(state, str):
            break
        if state in TERMINAL:
            return state
        if state not in ('in_progress', 'queued', 'pending') or attempt == 1:
            break
        await sleep(2)
    return 'cancel_unconfirmed'


async def generate_agent(client, pool, job, message, *, checkpoint, sleep=asyncio.sleep, clock=time.monotonic, progress=None, pb=None):
    from providers import GeminiProvider
    provider = GeminiProvider()
    started = clock()
    message_id = str(message.get('id') or '').strip()
    timing_tracker = AgentTimingTracker(clock=clock, t0=started)
    trace = AgentTrace(job=job.get('id', os.environ.get('REQUEST_ID', '')),
                       segment=message.get('index'), secrets=pool.keys, clock=clock, verbose=False)
    def telemetry(event, **fields):
        agent_telemetry(event, **trace.context, **fields)
    state = (await checkpoint()).get('state') or {}
    resumed = bool(state.get('id'))
    title_task = None
    if not resumed and pb is not None:
        title_task = asyncio.create_task(maybe_generate_title(client, pool, pb, job, message))
    context = {} if resumed else await resolve_previous_agent_context(pb, job)
    preferred_hash = state.get('credentialHash') if resumed else context.get('credential_hash')
    preferred_key = next((i for i, k in enumerate(pool.keys) if hashlib.sha256(k.encode()).hexdigest() == preferred_hash), None)
    if resumed and preferred_key is None:
        raise RuntimeError('无法找到原 Agent interaction 使用的 API Key；未创建重复执行。')
    reusing_remote = bool(context.get('environment_id') and context.get('previous_interaction_id'))
    if reusing_remote and preferred_key is None:
        telemetry('environment_fallback', reason='credential_missing')
        reusing_remote = False
    payload = build_agent_payload(job, message,
        environment_id=context['environment_id'] if reusing_remote else None,
        previous_interaction_id=context['previous_interaction_id'] if reusing_remote else None)
    telemetry('prepared',
        model=payload['agent_config']['model'],
        tools=[tool.get('type') for tool in payload['tools']],
        github_enabled=bool(os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '').strip()),
        github_mcp_enabled=any(tool.get('type') == 'mcp_server' for tool in payload['tools']),
        input_chars=len(payload['input']),
        system_chars=len(payload.get('system_instruction', '')),
        resumed=resumed,
        reused_environment=isinstance(payload.get('environment'), str),
        continued_interaction=bool(payload.get('previous_interaction_id')))
    index = key = lease = None
    current = None
    attempts = len(pool.keys) + int(reusing_remote)
    for attempt in range(attempts):
        if not resumed and (await checkpoint()).get('canceled'):
            raise AgentCanceled('Agent 已取消，未创建远程执行。')
        index, key, lease = await pool.acquire(key_index=preferred_key if resumed or reusing_remote else None)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if resumed:
            # Resume the original remote execution, never create a second interaction.
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
            timing_tracker.mark_dispatched()
            if not isinstance(data.get('id'), str) or not data['id']:
                raise RuntimeError('Agent 未返回 interaction ID；未自动重发。')
            telemetry('created', elapsed_seconds=round(clock() - started, 2),
                            status=data.get('status', 'unknown'),
                            environment_created=bool(data.get('environment_id')))
            state = {'id': data['id'], 'credentialHash': fingerprint, 'status': data.get('status', 'in_progress')}
            environment_id = data.get('environment_id') or (payload['environment'] if reusing_remote else None)
            if isinstance(environment_id, str):
                state['environmentId'] = environment_id
            try:
                await checkpoint(state)
            except Exception:
                # Stop a remote run whose identity cannot be durably recorded.
                await cancel_interaction(client, f'{ROOT}/{quote(data["id"], safe="")}', key)
                raise
            break
        if status in (400, 404) and reusing_remote:
            reusing_remote = False
            await pool.mark_cancelled(index, lease)
            telemetry('environment_fallback', reason='upstream_rejected', http=status)
            payload = build_agent_payload(job, message)
            continue
        await pool.mark_error(index, lease, http_status=status)
        scope = provider.credential_error_scope(status, data)
        if scope:
            pool.retire_key(index, 'agent access unavailable')
        elif status == 429:
            await pool.rate_limited(index, lease, provider.retry_after(headers, data),
                                    daily_exhausted=provider.quota_kind(data) in ['rpd', 'spend'])
        else:
            # A definite rejected POST is safe to report, without guessing other IDs.
            raise upstream_error(status, data, key)
        if reusing_remote or attempt + 1 >= attempts:
            raise upstream_error(status, data, key)
    if current is None:
        raise RuntimeError('无法找到原 Agent interaction 使用的 API Key；未创建重复执行。')

    url = f'{ROOT}/{quote(current["id"], safe="")}'
    # GET snapshots reconcile the optional event channel; neither can create a run.
    observer = None
    summary_worker = None
    summary_task = None
    if progress is not None:
        from agent_progress import observe_progress
        observer = asyncio.create_task(observe_progress(client, url, key, progress))
    if pb is not None and message_id:
        summary_worker = AgentSummaryWorker(client, pool, pb, job, message, progress, sleep=sleep)
        summary_task = asyncio.create_task(summary_worker.run())
        await timing_tracker.sync_if_dirty(pb, message_id)
    failures = 0
    poll_count = 0
    last_report = started
    last_status = None
    seen_steps = set()
    try:
        while True:
            for idx, step in enumerate(current.get('steps') or current.get('outputs') or []):
                if not isinstance(step, dict):
                    continue
                step_sig = (idx, str(step.get('type') or ''), bool(step.get('is_error')))
                step_type = str(step.get('type') or 'unknown')[:80]
                step_name = str(step.get('server_name') or step.get('name') or step.get('tool_name') or '')[:80]
                timing_tracker.record_step(
                    idx,
                    step_type,
                    label=step_name,
                    status='failed' if step.get('is_error') else 'completed',
                )
                if step_sig not in seen_steps:
                    seen_steps.add(step_sig)
                    telemetry('step', step_index=idx, step_type=step_type,
                              **({'tool_name': step_name} if step_name else {}),
                              **({'is_error': True} if step.get('is_error') else {}))
            if progress is not None:
                progress.snapshot(current)
                for pkey, pval in list(progress.parts.items()):
                    if pval.get('type') == 'status':
                        continue
                    timing_tracker.mark_first_reply()
                    if pkey.startswith('step:'):
                        tail = pkey.split(':', 1)[1]
                        if tail.isdigit():
                            timing_tracker.record_step(
                                int(tail),
                                str(pval.get('providerType') or pval.get('type') or 'step'),
                                label=str(pval.get('toolName') or ''),
                                status=str(pval.get('status') or 'completed'),
                            )
            if pb is not None and message_id:
                await timing_tracker.sync_if_dirty(pb, message_id)
            if current.get('status') in ['completed', 'failed', 'incomplete', 'requires_action', 'cancelled', 'canceled']:
                timing_tracker.mark_completed(current['status'])
                control = await checkpoint({'status': current['status'], **({'environmentId': current['environment_id']} if current.get('environment_id') else {})})
                if control.get('canceled') and current['status'] != 'completed':
                    await pool.mark_cancelled(index, lease)
                    raise AgentCanceled('Agent 已取消')
                await pool.mark_success(index, lease)
                result = parse_interaction(current, secrets=pool.keys)
                if summary_task is not None:
                    summary_task.cancel()
                    await asyncio.gather(summary_task, return_exceptions=True)
                    summary_task = None
                if summary_worker is not None:
                    await summary_worker.finalize()
                if pb is not None and message_id:
                    await timing_tracker.sync_if_dirty(pb, message_id, force=True)
                if title_task is not None:
                    if not title_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(title_task), timeout=5.0)
                        except (TimeoutError, Exception):
                            pass
                telemetry('completed',
                    status=current['status'],
                    elapsed_seconds=round(clock() - started, 2),
                    polls=poll_count,
                    reply_chars=len(result['reply']),
                    parts_count=len(result.get('parts') or []),
                    tool_calls_count=len(result.get('toolCalls') or []),
                    code_executions_count=len(result.get('codeExecutions') or []),
                    annotations_count=len(result.get('annotations') or []),
                    **agent_usage_diagnostics(current))
                return result
            control = await checkpoint()
            if control.get('canceled'):
                if observer is not None:
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
                    observer = None
                upstream_status = await reconcile_requested_cancel(client, url, key, sleep=sleep)
                telemetry('cancel_reconciled', status=upstream_status)
                try:
                    await checkpoint({'status': upstream_status})
                except Exception as error:
                    telemetry('cancel_checkpoint_failed', error_type=type(error).__name__)
                finally:
                    await pool.mark_cancelled(index, lease)
                raise AgentCanceled('Agent 已取消')
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
                # One useful progress line per minute, plus nonterminal status changes.
                # Terminal usage is reported once above; cumulative counters are never summed.
                if (current.get('status') not in TERMINAL
                        and (current.get('status') != last_status or clock() - last_report >= 60)):
                    summary = agent_usage_diagnostics(current)
                    telemetry('progress', elapsed_seconds=round(clock() - started, 2),
                                    polls=poll_count, status=current.get('status', 'unknown'),
                                    **({'event_observer_active': observer is not None and not observer.done(),
                                        'observed_step_count': progress.usage.get('agentStepCount', 0)}
                                       if progress is not None else {}),
                                    **summary)
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
            await asyncio.shield(cancel_interaction(client, url, key))
        except (RuntimeError, httpx.HTTPError, TimeoutError) as error:
            telemetry('cancel_failed', error_type=type(error).__name__)
        finally:
            await pool.mark_cancelled(index, lease)
        raise
    except AgentCanceled:
        raise
    except Exception:
        # Do not leave an unmonitored agent spending tokens after a local failure.
        try:
            await cancel_interaction(client, url, key)
        except Exception as cancel_error:
            telemetry('cancel_failed', error_type=type(cancel_error).__name__)
        raise
    finally:
        if summary_task is not None:
            summary_task.cancel()
            await asyncio.gather(summary_task, return_exceptions=True)
        if title_task is not None and not title_task.done():
            title_task.cancel()
            await asyncio.gather(title_task, return_exceptions=True)
        if observer is not None:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
