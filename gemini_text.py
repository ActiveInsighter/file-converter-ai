"""Bounded text-only Gemini fan-out. PocketBase owns inputs and every saved reply.

No Files API, OCR, image requests or PDF transcription rules are used here.
A saved result retries only its PB write, never the paid generation. A second
Actions attempt cannot re-claim a partially executed job and silently resend it.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from key_pool import KeyPool, QuotaPoolExhaustedError, parse_key_groups
from providers import GeminiProvider
from response_capture import extract_structured_payload, generate_content_snapshot
from gemini_agent import AgentCanceled, generate_agent, is_antigravity
from agent_fast_tasks import AgentSummaryWorker, AgentTimingTracker, maybe_generate_title
from agent_trace import AgentTrace
from agent_progress import MessageProgress
from image_fetch import collect_job_image_urls, extract_image_markers, fetch_images_as_base64

MAX_REPLY_BYTES = 2 * 1024 * 1024
TOKEN_FIELDS = {
    'input_tokens': 'promptTokenCount', 'output_tokens': 'candidatesTokenCount',
    'thought_tokens': 'thoughtsTokenCount', 'cached_tokens': 'cachedContentTokenCount',
    'tool_tokens': 'toolUseTokenCount', 'total_tokens': 'totalTokenCount',
}


def log_result(log, result):
    """Log the answer once, before persistence; never dump prompts or tool output."""
    usage = result.get('usage') or {}
    # Redact the whole reply before chunking, including credentials spanning chunks.
    reply = log.redact(result.get('reply') or '')
    parts = [reply[i:i + 4000] for i in range(0, len(reply), 4000)]
    log.emit('segment_result', status='failed' if result.get('error') else 'succeeded',
             protocol=result.get('protocol', ''),
             finish_reason=result.get('finishReason', ''), error=result.get('error', ''),
             reply_chars=len(result.get('reply') or ''), reply_parts=len(parts),
             ui_parts=len(result.get('parts') or []),
             tool_calls=len(result.get('toolCalls') or []),
             code_executions=len(result.get('codeExecutions') or []),
             annotations=len(result.get('annotations') or []),
             has_thinking=bool(result.get('thinkingSummary')),
             has_structured_output=result.get('structuredOutput') is not None,
             **{name: usage.get(key) for name, key in TOKEN_FIELDS.items()},
             steps=usage.get('agentStepCount'))
    for tc in (result.get('toolCalls') or [])[:30]:
        log.emit('tool_summary', tool_name=tc.get('toolName', ''), kind=tc.get('kind', ''),
                 status=tc.get('status', ''), call_id=tc.get('callId', ''))
    for index, text in enumerate(parts, 1):
        # JSON keeps arbitrary model text from becoming GitHub workflow commands.
        log.emit('reply', part=index, parts=len(parts), text=text)


class GeminiRequestError(RuntimeError):
    def __init__(self, message, data, *, secrets=()):
        super().__init__(message)
        self.result = {'reply': '', 'error': message, 'finishReason': 'HTTP_ERROR', 'usage': {},
                       'response': generate_content_snapshot(data, secrets=secrets)}

# Keep HTTP/error-category diagnostics; ordinary chat intentionally uses no tools.
def safe_error_code(data: dict) -> str:
    """Only expose a machine-readable Google status, never message text."""
    error = data.get('error') if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return 'UNKNOWN'
    value = error.get('status') or error.get('code')
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
        return value.upper()
    return 'UNKNOWN'


async def create_key_pool(keys: list[str], options: dict, raw_groups: str = "") -> KeyPool:
    return await KeyPool.create(keys, parse_key_groups(len(keys), raw_groups),
        rpm_per_key=options["rpmPerKey"], rpd_per_key=options["rpdPerKey"])


_THINKING_LEVEL_ALIASES = {
    "none": "off",
    "xhigh": "high",
    "max": "high",
}

_GEMINI_25_BUDGETS = {
    "off": 0,
    "minimal": 0,
    "low": 4096,
    "medium": 8192,
    "high": 24576,
}


def normalize_thinking_level(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    return _THINKING_LEVEL_ALIASES.get(value, value)


def _parts_with_images(texts: list[str], images_map: dict[str, tuple[str, str]]) -> list[dict]:
    parts: list[dict] = []
    urls: list[str] = []
    for raw in texts:
        clean_text, found = extract_image_markers(raw)
        if clean_text:
            parts.append({"text": clean_text})
        urls.extend(found)
    for url in urls:
        entry = images_map.get(url)
        if entry is not None:
            mime_type, b64_data = entry
            parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})
    return parts


def build_payload(job: dict, message: dict, *, resolved_images: dict[str, tuple[str, str]] | None = None) -> dict:
    images_map = resolved_images or {}
    parts = _parts_with_images([job.get("prompt", ""), message.get("inputText", "")], images_map)
    options = job.get("options") or {}
    model = str(job.get("model") or "").strip()
    model_lower = model.lower()
    thinking_level = normalize_thinking_level(options.get("thinkingLevel"))
    max_output_tokens = int(options.get("maxOutputTokens") or 8192)
    payload = GeminiProvider().build_payload(
        model, parts, thinking_level=thinking_level,
        max_output_tokens=max_output_tokens,
    )
    if model_lower.startswith("gemini-2.5"):
        payload["generationConfig"].pop("thinkingConfig", None)
        if thinking_level in _GEMINI_25_BUDGETS:
            budget = _GEMINI_25_BUDGETS[thinking_level]
            if budget == 0 and "pro" in model_lower:
                budget = 128
            cfg = {"thinkingBudget": budget}
            if budget != 0:
                cfg["includeThoughts"] = True
            payload["generationConfig"]["thinkingConfig"] = cfg
        elif thinking_level in ("", "auto"):
            payload["generationConfig"]["thinkingConfig"] = {"includeThoughts": True}
    elif model_lower.startswith(("gemini-1.5", "gemini-2.0")) and "thinking" not in model_lower:
        payload["generationConfig"].pop("thinkingConfig", None)
    elif thinking_level == "off":
        payload["generationConfig"].pop("thinkingConfig", None)
    elif thinking_level in ("minimal", "low", "medium", "high"):
        payload["generationConfig"]["thinkingConfig"] = {
            "thinkingLevel": thinking_level,
            "includeThoughts": True,
        }
    else:
        payload["generationConfig"]["thinkingConfig"].pop("thinkingLevel", None)
        if model_lower.startswith(("gemini-3", "gemini-4")):
            payload["generationConfig"].setdefault("thinkingConfig", {})["includeThoughts"] = True
        else:
            payload["generationConfig"].pop("thinkingConfig", None)
    if options.get('responseMimeType'):
        payload['generationConfig']['responseMimeType'] = str(options['responseMimeType'])
    if isinstance(options.get('responseSchema'), dict):
        payload['generationConfig']['responseSchema'] = options['responseSchema']
    elif isinstance(options.get('responseJsonSchema'), dict):
        payload['generationConfig']['responseJsonSchema'] = options['responseJsonSchema']
    if isinstance(options.get('tools'), list) and options['tools']:
        payload['tools'] = options['tools']
    if job.get('history'):
        payload['contents'] = [
            {'role': turn['role'], 'parts': [{'text': turn['text']}]}
            for turn in job['history']
        ] + payload['contents']
    if job.get("systemPrompt"):
        payload["systemInstruction"] = {"parts": [{"text": job["systemPrompt"]}]}
    return payload


def parse_answer(payload: dict, *, secrets=()) -> dict:
    candidate = (payload.get("candidates") or [{}])[0]
    reason = str(candidate.get("finishReason") or "")[:64]
    reply = "\n".join(part["text"] for part in (candidate.get("content") or {}).get("parts", [])
                      if isinstance(part.get("text"), str) and not part.get("thought"))
    error = ""
    if len(reply.encode("utf-8")) > MAX_REPLY_BYTES:
        raise RuntimeError("Gemini reply exceeds 2 MiB; reduce the output limit")
    if reason == "MAX_TOKENS":
        error = "回复达到输出上限，已保留不完整正文；可提高输出上限后重新提交。"
    elif (payload.get("promptFeedback") or {}).get("blockReason") or reason not in ("", "STOP"):
        error = f"Gemini 未正常完成回复（{reason or 'PROMPT_BLOCKED'}）"
    elif not reply.strip():
        error = "Gemini 返回空回复"
    usage = {key: value for key, value in (payload.get("usageMetadata") or {}).items()
             if key in {"promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount", "cachedContentTokenCount", "toolUsePromptTokenCount", "toolUseTokenCount"}
             and isinstance(value, int) and value >= 0}
    if 'toolUsePromptTokenCount' in usage:
        usage['toolUseTokenCount'] = usage['toolUsePromptTokenCount']
    response = generate_content_snapshot(payload, secrets=secrets)
    structured = extract_structured_payload('generateContent', payload, secrets=secrets, reply_text=reply)
    return {
        "reply": reply,
        "error": error,
        "finishReason": reason,
        "usage": usage,
        "response": response,
        **structured,
    }


class PocketBase:
    def __init__(self, client: httpx.AsyncClient, root: str, *, api_prefix="gemini"):
        self.client, self.root, self.token = client, root.rstrip("/"), ""
        self.api_prefix = api_prefix

    async def authenticate(self, *, token="", email="", password=""):
        # A repository server token is server-side only. Dedicated executor
        # credentials remain available when full database access is unnecessary.
        if token.strip():
            self.token = token.strip()
        elif email and password:
            await self.login(email, password)
        else:
            raise RuntimeError("Set POCKETBASE_SERVER_TOKEN or GEMINI_PB_EMAIL and GEMINI_PB_PASSWORD in repository secrets")

    async def login(self, email: str, password: str):
        response = await self.client.post(f"{self.root}/api/collections/aw_gemini_services/auth-with-password",
                                          json={"identity": email, "password": password}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"PB executor authentication failed (HTTP {response.status_code})")
        self.token = response.json()["token"]

    async def post(self, path: str, data: dict, *, retries=0):
        for attempt in range(retries + 1):
            try:
                response = await self.client.post(f"{self.root}/api/anyworkflow/{self.api_prefix}/{path}",
                    headers={"Authorization": self.token}, json=data, timeout=60)
                if response.is_success:
                    return response.json()
                if response.status_code < 500 or attempt == retries:
                    raise RuntimeError(f"PB request failed (HTTP {response.status_code})")
            except httpx.TransportError:
                if attempt == retries:
                    raise RuntimeError("PB connection failed; no model request was re-sent") from None
            await asyncio.sleep(min(16, 2 ** attempt))

    async def checkpoint(self, path: str, identity: dict, state=None):
        # Checkpoints are idempotent for the recorded interaction. A service
        # restart can leave the public proxy returning 502 longer than 1+2+4s;
        # keep the same execution identity through that recovery window.
        return await self.post(path, {**identity, **({'state': state} if state is not None else {})}, retries=12)


async def generate(client, pool, job, message, *, sleep=asyncio.sleep, checkpoint=None, progress=None, pb=None, timing=None) -> dict:
    if is_antigravity(job['model']):
        if checkpoint is None:
            raise RuntimeError('Agent requires durable execution state')
        return await generate_agent(client, pool, job, message, checkpoint=checkpoint, sleep=sleep, progress=progress, pb=pb, timing=timing)
    provider = GeminiProvider()
    image_urls = collect_job_image_urls(job, message, include_history=True)
    resolved_images = await fetch_images_as_base64(client, image_urls) if image_urls else {}
    payload = build_payload(job, message, resolved_images=resolved_images)
    last_http_error = ''
    last_data = {}
    for attempt in range(4):
        try:
            index, key, lease = await pool.acquire()
        except QuotaPoolExhaustedError:
            # If every key was rejected, keep the actual HTTP code instead of
            # replacing it with the generic exhausted-pool exception.
            if last_http_error:
                raise GeminiRequestError(last_http_error, last_data, secrets=pool.keys) from None
            raise
        try:
            async with asyncio.timeout(180):
                # Gemini responses are bounded too, even for a chunked HTTP response.
                endpoint = provider.endpoint(provider.default_base_url, job["model"])
                if progress is not None:
                    endpoint = endpoint.replace(':generateContent', ':streamGenerateContent') + '?alt=sse'
                async with client.stream("POST", endpoint,
                        headers=provider.build_headers(key), json=payload, timeout=180) as response:
                    status, headers = response.status_code, response.headers
                    if status == 200 and 'text/event-stream' in headers.get('content-type', ''):
                        from agent_trace import sse_events
                        answer, summary, data = '', '', {}
                        accumulated_parts = []
                        merged_candidate = {}
                        step_seq = 0
                        async for chunk, _, _ in sse_events(response):
                            candidate = (chunk.get('candidates') or [{}])[0]
                            for mkey in ('index', 'finishReason', 'finishMessage', 'safetyRatings',
                                         'citationMetadata', 'groundingMetadata', 'urlContextMetadata',
                                         'avgLogprobs', 'logprobsResult'):
                                if mkey in candidate and candidate[mkey] is not None:
                                    if mkey == 'groundingMetadata' and isinstance(merged_candidate.get(mkey), dict) and isinstance(candidate[mkey], dict):
                                        prev_gm = merged_candidate[mkey]
                                        cur_gm = candidate[mkey]
                                        merged_gm = {**prev_gm, **cur_gm}
                                        if prev_gm.get('groundingChunks') and cur_gm.get('groundingChunks'):
                                            merged_gm['groundingChunks'] = prev_gm['groundingChunks'] + cur_gm['groundingChunks']
                                        merged_candidate[mkey] = merged_gm
                                    else:
                                        merged_candidate[mkey] = candidate[mkey]
                            for part in (candidate.get('content') or {}).get('parts', []):
                                if not isinstance(part, dict):
                                    continue
                                text = part.get('text')
                                if isinstance(text, str):
                                    if part.get('thought'):
                                        summary += text
                                        if accumulated_parts and isinstance(accumulated_parts[-1].get('text'), str) and accumulated_parts[-1].get('thought'):
                                            accumulated_parts[-1]['text'] += text
                                        else:
                                            accumulated_parts.append({**part})
                                    else:
                                        answer += text
                                        if accumulated_parts and isinstance(accumulated_parts[-1].get('text'), str) and not accumulated_parts[-1].get('thought'):
                                            accumulated_parts[-1]['text'] += text
                                        else:
                                            accumulated_parts.append({**part})
                                else:
                                    accumulated_parts.append(part)
                                    if progress is not None:
                                        if isinstance(part.get('executableCode'), dict):
                                            ec = part['executableCode']
                                            progress.put(f'step:{step_seq}', 'tool_call',
                                                         json.dumps({'language': ec.get('language', 'PYTHON'), 'code': ec.get('code', '')}, ensure_ascii=False),
                                                         status='running', tool_name='code_execution',
                                                         call_id=str(ec.get('id') or ''), provider_type='executableCode',
                                                         tool_kind='server')
                                            step_seq += 1
                                        elif isinstance(part.get('codeExecutionResult'), dict):
                                            cer = part['codeExecutionResult']
                                            err = str(cer.get('outcome') or '') not in ('', 'OUTCOME_OK')
                                            progress.put(f'step:{step_seq}', 'tool_result',
                                                         json.dumps({'outcome': cer.get('outcome', ''), 'output': cer.get('output', '')}, ensure_ascii=False),
                                                         status='failed' if err else 'completed', tool_name='code_execution',
                                                         call_id=str(cer.get('id') or ''), provider_type='codeExecutionResult',
                                                         tool_kind='server')
                                            step_seq += 1
                                        elif isinstance(part.get('functionCall'), dict):
                                            fc = part['functionCall']
                                            progress.put(f'step:{step_seq}', 'tool_call',
                                                         json.dumps({'arguments': fc.get('args') or {}}, ensure_ascii=False),
                                                         status='running', tool_name=str(fc.get('name') or 'function'),
                                                         call_id=str(fc.get('id') or ''), provider_type='functionCall',
                                                         tool_kind='client')
                                            step_seq += 1
                                        elif isinstance(part.get('functionResponse'), dict):
                                            fr = part['functionResponse']
                                            progress.put(f'step:{step_seq}', 'tool_result',
                                                         json.dumps({'response': fr.get('response') or {}}, ensure_ascii=False),
                                                         status='completed', tool_name=str(fr.get('name') or 'function'),
                                                         call_id=str(fr.get('id') or ''), provider_type='functionResponse',
                                                         tool_kind='client')
                                            step_seq += 1
                                        elif isinstance(part.get('toolCall'), dict):
                                            tc = part['toolCall']
                                            progress.put(f'step:{step_seq}', 'tool_call',
                                                         json.dumps({'arguments': tc.get('args') or {}}, ensure_ascii=False),
                                                         status='running', tool_name=str(tc.get('toolType') or tc.get('name') or 'server_tool'),
                                                         call_id=str(tc.get('id') or ''), provider_type='toolCall',
                                                         tool_kind='server')
                                            step_seq += 1
                                        elif isinstance(part.get('toolResponse'), dict):
                                            tr = part['toolResponse']
                                            progress.put(f'step:{step_seq}', 'tool_result',
                                                         json.dumps({'response': tr.get('response') or {}}, ensure_ascii=False),
                                                         status='completed', tool_name=str(tr.get('toolType') or tr.get('name') or 'server_tool'),
                                                         call_id=str(tr.get('id') or ''), provider_type='toolResponse',
                                                         tool_kind='server')
                                            step_seq += 1
                            if len(answer.encode()) > MAX_REPLY_BYTES or len(summary.encode()) > MAX_REPLY_BYTES:
                                raise RuntimeError('Gemini streamed reply exceeds 2 MiB')
                            data = {**data, **chunk}
                            if progress is not None:
                                if answer: progress.put('answer', 'text', answer, status='running')
                                if summary: progress.put('reasoning', 'reasoning_summary', summary, status='running')
                                um = chunk.get('usageMetadata') or {}
                                if isinstance(um, dict):
                                    for k, v in um.items():
                                        mapped = 'toolUseTokenCount' if k == 'toolUsePromptTokenCount' else k
                                        if mapped in TOKEN_FIELDS.values() and isinstance(v, int) and v >= 0:
                                            progress.usage[mapped] = v
                        data['candidates'] = [{**merged_candidate, 'content': {'parts': accumulated_parts}}]
                        if progress is not None:
                            if answer: progress.put('answer', 'text', answer)
                            if summary: progress.put('reasoning', 'reasoning_summary', summary)
                    else:
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            raw.extend(block)
                            if len(raw) > 3 * MAX_REPLY_BYTES:
                                raise RuntimeError("Gemini response exceeds 6 MiB")
                        try:
                            data = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            raise RuntimeError("Gemini returned invalid JSON") from None
        except asyncio.CancelledError:
            await pool.mark_cancelled(index, lease)
            raise
        except (TimeoutError, httpx.TransportError):
            # The server may already have generated an answer. Do not blindly
            # repeat an ambiguous paid request after a transport timeout.
            await pool.mark_error(index, lease)
            raise RuntimeError("Gemini 连接中断，无法确认回复；为避免重复计费，未自动补发。") from None
        except Exception:
            await pool.mark_error(index, lease)
            raise
        if status == 200:
            await pool.mark_success(index, lease)
            return parse_answer(data, secrets=pool.keys)
        last_data = data
        error_code = safe_error_code(data)
        last_http_error = (f"Gemini 拒绝请求（HTTP {status}，错误类型 {error_code}），"
                           "请检查模型、工具权限、密钥权限或项目额度")
        print(f'[gemini] upstream_http={status} error_code={error_code} '
              f'model={job["model"]}', flush=True)
        await pool.mark_error(index, lease, http_status=status)
        scope = provider.credential_error_scope(status, data)
        if scope == "key":
            pool.retire_key(index, "invalid credential")
        elif scope == "bucket":
            pool.retire(index, "unavailable quota")
        elif status == 429:
            quota = provider.quota_kind(data)
            await pool.rate_limited(index, lease, provider.retry_after(headers, data),
                                    daily_exhausted=quota in {"rpd", "spend"}, quota_type=quota)
        elif status not in (500, 502, 503, 504):
            raise GeminiRequestError(last_http_error, data, secrets=pool.keys)
        if attempt < 3 and not scope and status != 429:
            await sleep(min(30, max(2 ** (attempt + 1), provider.retry_after(headers, data) or 0)))
    raise GeminiRequestError("Gemini 暂时不可用或请求额度不足，请稍后重新提交", last_data, secrets=pool.keys)


async def run_job(pb, client, job_id: str, execution_id: str, pool_factory, work_dir: Path, *, fast_pool_factory=None):
    path = f"jobs/{job_id}"
    identity = {"executionId": execution_id}
    model_override = os.environ.get("GEMINI_MODEL", "").strip().removeprefix('models/')
    if model_override and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,109}', model_override) or
                           any(part in ('.', '..') for part in model_override.split('/'))):
        raise RuntimeError('Invalid Gemini model override')
    thinking_override = normalize_thinking_level(os.environ.get("GEMINI_THINKING_LEVEL"))
    if thinking_override and thinking_override not in {'auto', *_GEMINI_25_BUDGETS}:
        raise RuntimeError('Invalid Gemini thinking level override')
    job = await pb.post(f"{path}/claim", identity, retries=3)
    options = job.setdefault("options", {})
    options.setdefault("concurrency", 5)
    options.setdefault("maxOutputTokens", 8192)
    options.setdefault("rpmPerKey", 15)
    options.setdefault("rpdPerKey", 500)
    options.setdefault("thinkingLevel", "")
    if model_override and model_override.casefold() != job['model'].casefold():
        if is_antigravity(model_override) or is_antigravity(job['model']):
            raise RuntimeError('Antigravity execution settings must come from the PocketBase job')
        job['model'] = model_override
    if thinking_override:
        options['thinkingLevel'] = thinking_override
    pool = await pool_factory(options)
    fast_pool = None
    auxiliary_tasks = set()
    if fast_pool_factory is not None and is_antigravity(job['model']):
        try:
            fast_pool = await fast_pool_factory(options)
        except Exception as error:
            print(f'[agent.fast] pool_setup_failed error_type={type(error).__name__}', flush=True)

    def run_auxiliary(coro):
        task = asyncio.create_task(coro)
        auxiliary_tasks.add(task)
        def done(task):
            auxiliary_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                print(f'[agent.fast] task_failed error_type={type(task.exception()).__name__}', flush=True)
        task.add_done_callback(done)
    job_log = AgentTrace(job=job_id, secrets=pool.keys, prefix='[gemini]')
    antigravity = is_antigravity(job['model'])
    protocol = 'interactions' if antigravity else 'generateContent'
    job_log.emit('job_started', model=job['model'], protocol=protocol,
                 segments=len(job['messages']), concurrency=options['concurrency'])
    queue = asyncio.Queue()
    for message in job["messages"]:
        if message["status"] == "queued":
            queue.put_nowait(message)
    work_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    usages = []
    counts = {'saved': 0, 'skipped': 0, 'canceled': 0}

    async def worker():
        while not queue.empty():
            message = queue.get_nowait()
            endpoint = f"{path}/messages/{message['id']}"
            log = AgentTrace(job=job_id, segment=message['index'], secrets=pool.keys, prefix='[gemini]')
            stage = 'permit'
            ended = asyncio.Event()
            timing = None
            final_status = 'failed'
            try:
                begun = time.monotonic()
                started_at = datetime.now(timezone.utc).isoformat()
                # Not retried: a lost permit is not permission to send twice.
                permit = await pb.post(f"{endpoint}/start", identity)
                if not permit.get("send"):
                    counts['skipped'] += 1
                    log.emit('segment_skipped', reason='no send permit')
                    continue
                log.emit('segment_started', model=job['model'], protocol=protocol, message_id=message['id'])
                async def upload_progress(body):
                    return await pb.post(f"{endpoint}/progress", body, retries=2)
                if antigravity:
                    timing = AgentTimingTracker(t0=begun, submitted_at=job.get('created'))
                progress = MessageProgress(secrets=pool.keys, post=upload_progress, identity=identity,
                    target=work_dir / f"{message['id']}.progress.json", job_id=job_id, message_id=message['id'], timing=timing)
                if timing is not None:
                    run_auxiliary(timing.pump(pb, endpoint, identity, ended))
                    if fast_pool is not None:
                        run_auxiliary(maybe_generate_title(client, fast_pool, pb, job, message, endpoint=endpoint, identity=identity))
                        summary = AgentSummaryWorker(client, fast_pool, pb, job, message, progress, endpoint=endpoint, identity=identity)
                        run_auxiliary(summary.run(ended))
                progress_task = asyncio.create_task(progress.pump())
                stage = 'generation'
                try:
                    if antigravity:
                        async def checkpoint(state=None):
                            return await pb.checkpoint(f"{endpoint}/checkpoint", identity, state)
                        result = await generate(client, pool, job, message, checkpoint=checkpoint, progress=progress, pb=pb, timing=timing)
                    else:
                        result = await generate(client, pool, job, message, progress=progress)
                except AgentCanceled:
                    final_status = 'canceled'
                    counts['canceled'] += 1
                    log.emit('segment_canceled')
                    continue
                except Exception as error:
                    if antigravity:
                        try:
                            stopped = (await checkpoint()).get('canceled', False)
                        except Exception as check_error:
                            stopped = False
                            log.emit('cancel_check_failed', error_type=type(check_error).__name__)
                        if stopped:
                            final_status = 'canceled'
                            counts['canceled'] += 1
                            log.emit('segment_canceled')
                            continue
                    # Never include provider bodies, credentials, or source text in logs.
                    detail = str(error)
                    secrets = [*pool.keys, os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', ''), os.environ.get('ANTIGRAVITY_CLOUDFLARE_TOKEN', '')]
                    for secret in secrets:
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    result = getattr(error, "result", None) or {"reply": '\n\n'.join(p['content'] for p in sorted(progress.parts.values(), key=lambda part: part['sequence']) if p['type'] == 'text').encode('utf-8')[:MAX_REPLY_BYTES].decode('utf-8', errors='ignore'), "error": detail[:1800], "finishReason": "", "usage": progress.usage.copy()}
                finally:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                final_status = 'failed' if result.get('error') else 'completed'
                if timing is not None:
                    timing.mark_completed(final_status)
                usages.append(result.get('usage') or {})
                log_result(log, result)
                stage = 'recovery_write'
                execution_meta = {
                    'startedAt': started_at, 'finishedAt': datetime.now(timezone.utc).isoformat(),
                    'durationSeconds': round(time.monotonic() - begun, 3), 'timingBasis': 'Actions runner wall clock',
                }
                if isinstance(result.get('response'), dict):
                    result['response']['metadata']['execution'] = execution_meta
                saved = {**identity, 'execution': execution_meta, **result}
                target = work_dir / f"{message['id']}.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps({"jobId": job_id, "messageId": message["id"], "result": saved}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
                try:
                    progress.finalize(result)
                    await progress.flush()
                except Exception as error:
                    # The final reply has its own durable payload and endpoint.
                    log.emit('progress_persistence_deferred', error_type=type(error).__name__,
                             error=log.redact(str(error))[:240])
                stage = 'persistence'
                # Retrying persistence cannot trigger another Gemini request.
                await pb.post(f"{endpoint}/complete", saved, retries=5)
                target.unlink()
                if progress.target and progress.target.exists(): progress.target.unlink()
                counts['saved'] += 1
                log.emit('segment_saved', status='failed' if result['error'] else 'succeeded',
                         duration_seconds=execution_meta['durationSeconds'])
                if result["error"]:
                    failures.append(message["id"])
            except Exception as error:
                failures.append(message["id"])
                log.emit('segment_error', stage=stage, error_type=type(error).__name__,
                         error=log.redact(str(error))[:1800], recovery_available=(work_dir / f"{message['id']}.json").exists())
            finally:
                if timing is not None and timing.completed_at is None:
                    timing.mark_completed(final_status)
                ended.set()
                queue.task_done()

    try:
        await asyncio.gather(*(worker() for _ in range(job["options"]["concurrency"])))
    finally:
        if auxiliary_tasks:
            _, pending = await asyncio.wait(tuple(auxiliary_tasks), timeout=50)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if fast_pool is not None:
            await fast_pool.close()
        await pool.close()
    totals = {}
    for name, key in TOKEN_FIELDS.items():
        values = [usage.get(key) for usage in usages]
        reported = [value for value in values if type(value) is int]
        totals[name] = sum(reported) if reported else None
    job_log.emit('job_summary', status='failed' if failures else 'canceled' if counts['canceled'] else 'completed',
                 generated=len(usages), failed=len(failures), **counts, **totals,
                 usage_reported_segments=sum(type(usage.get('totalTokenCount')) is int for usage in usages))
    return 2 if failures else 0


async def recover(pb, directory: Path):
    """Flush recovery artifacts using their original identity; never generate text."""
    for path in sorted(directory.glob('*.progress.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        if not all(re.fullmatch(r'[a-z0-9]{15}', data.get(k, '')) for k in ('jobId', 'messageId')):
            raise RuntimeError('Invalid progress recovery identity')
        async def upload(body):
            return await pb.post(f"jobs/{data['jobId']}/messages/{data['messageId']}/progress", body, retries=5)
        progress = MessageProgress(post=upload, identity=data['identity'], target=path,
                                   job_id=data['jobId'], message_id=data['messageId'])
        progress.sequence, progress.parts, progress.usage, progress.pending = data['sequence'], data['parts'], data['usage'], data.get('pending')
        progress.capture_bytes = sum(len(p['content'].encode()) for p in progress.parts.values())
        progress.dirty = set(progress.parts)
        try:
            await progress.flush()
        except Exception as error:
            if not (directory / f"{data['messageId']}.json").is_file():
                raise
            print(f"[gemini] progress recovery deferred ({type(error).__name__}); saving final reply", flush=True)
    for path in sorted(directory.glob('*.json')):
        if path.name.endswith('.progress.json'): continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if not all(re.fullmatch(r'[a-z0-9]{15}', data.get(key, '')) for key in ('jobId', 'messageId')):
            raise RuntimeError('Invalid recovery artifact identity')
        await pb.post(f"jobs/{data['jobId']}/messages/{data['messageId']}/complete", data['result'], retries=5)
        print(f"[gemini] recovered message={data['messageId']}", flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-dir', type=Path, help='Write saved recovery JSONs to PB without calling Gemini')
    args = parser.parse_args()
    job_id = os.environ.get("REQUEST_ID", "")
    maintenance = re.fullmatch(r'(check|rescue|probe)-([a-z0-9]{15})', job_id)
    if not args.recover_dir and maintenance:
        from agent_maintenance import main as maintenance_main
        return await maintenance_main(*maintenance.groups())
    # Manual diagnostics use the existing workflow, with an isolated concurrency
    # identity. No real PB job ID has this prefix; never claim or mutate that job.
    if not args.recover_dir and re.fullmatch(r'sync-[a-z0-9]{15}', job_id):
        from agent_progress_sync import main as sync_main
        return await sync_main(job_id.removeprefix('sync-'))
    if not args.recover_dir and re.fullmatch(r'cancel-requested-[a-z0-9]{15}', job_id):
        from agent_diagnostics import main as reconcile_cancel
        return await reconcile_cancel(job_id.removeprefix('cancel-requested-'), retry_cancel=True)
    if not args.recover_dir and re.fullmatch(r'trace-[a-z0-9]{15}', job_id):
        from agent_diagnostics import main as diagnose_main
        return await diagnose_main(job_id.removeprefix('trace-'))
    execution_id = f"{os.environ.get('GITHUB_RUN_ID', '')}:{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"
    if not args.recover_dir and (not re.fullmatch(r"[a-z0-9]{15}", job_id) or not re.fullmatch(r"\d+:\d+", execution_id)):
        raise RuntimeError("Valid REQUEST_ID and GitHub run identity are required")
    root = os.environ.get("GEMINI_PB_URL", "")
    if not re.fullmatch(r"https://[^/?#@]+", root):
        raise RuntimeError("GEMINI_PB_URL must be an HTTPS origin")
    keys = [key.strip() for key in os.environ.get("GEMINI_API_KEYS", "").splitlines() if key.strip()]
    if not keys and not args.recover_dir:
        raise RuntimeError("GEMINI_API_KEYS is required")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        pb = PocketBase(client, root)
        await pb.authenticate(token=os.environ.get("POCKETBASE_SERVER_TOKEN", ""),
            email=os.environ.get("GEMINI_PB_EMAIL", ""), password=os.environ.get("GEMINI_PB_PASSWORD", ""))
        if args.recover_dir:
            if not args.recover_dir.is_dir():
                raise RuntimeError('Recovery directory does not exist')
            await recover(pb, args.recover_dir)
            return 0

        async def pool_factory(options):
            return await create_key_pool(keys, options, os.environ.get("GEMINI_KEY_GROUPS", ""))

        async def fast_pool_factory(options):
            return await pool_factory({"rpmPerKey": 15, "rpdPerKey": 500})

        return await run_job(pb, client, job_id, execution_id, pool_factory, Path("work/gemini/recovery"),
                             fast_pool_factory=fast_pool_factory)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (RuntimeError, KeyError) as error:
        print(f"[gemini] setup failed: {error}", flush=True)
        raise SystemExit(1) from None
