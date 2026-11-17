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
from pathlib import Path

import httpx

from key_pool import KeyPool, QuotaPoolExhaustedError, parse_key_groups
from providers import GeminiProvider
from gemini_agent import AgentCanceled, generate_agent, is_antigravity
from agent_trace import AgentTrace
from agent_progress import MessageProgress

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
             finish_reason=result.get('finishReason', ''), error=result.get('error', ''),
             reply_chars=len(result.get('reply') or ''), reply_parts=len(parts),
             **{name: usage.get(key) for name, key in TOKEN_FIELDS.items()},
             steps=usage.get('agentStepCount'))
    for index, text in enumerate(parts, 1):
        # JSON keeps arbitrary model text from becoming GitHub workflow commands.
        log.emit('reply', part=index, parts=len(parts), text=text)

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


def build_payload(job: dict, message: dict) -> dict:
    parts = [{"text": value} for value in (job.get("prompt", ""), message.get("inputText", "")) if value]
    options = job["options"]
    payload = GeminiProvider().build_payload(
        job["model"], parts, thinking_level=options.get("thinkingLevel", ""),
        max_output_tokens=options["maxOutputTokens"],
    )
    if not options.get("thinkingLevel"):
        payload["generationConfig"].pop("thinkingConfig", None)
    if job.get("systemPrompt"):
        payload["systemInstruction"] = {"parts": [{"text": job["systemPrompt"]}]}
    return payload


def parse_answer(payload: dict) -> dict:
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
             if key in {"promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount", "cachedContentTokenCount"}
             and isinstance(value, int) and value >= 0}
    return {"reply": reply, "error": error, "finishReason": reason, "usage": usage}


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


async def generate(client, pool, job, message, *, sleep=asyncio.sleep, checkpoint=None, progress=None) -> dict:
    if is_antigravity(job['model']):
        if checkpoint is None:
            raise RuntimeError('Agent requires durable execution state')
        return await generate_agent(client, pool, job, message, checkpoint=checkpoint, sleep=sleep, progress=progress)
    provider = GeminiProvider()
    payload = build_payload(job, message)
    last_http_error = ''
    for attempt in range(4):
        try:
            index, key, lease = await pool.acquire()
        except QuotaPoolExhaustedError:
            # If every key was rejected, keep the actual HTTP code instead of
            # replacing it with the generic exhausted-pool exception.
            if last_http_error:
                raise RuntimeError(last_http_error) from None
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
                        async for chunk, _, _ in sse_events(response):
                            candidate = (chunk.get('candidates') or [{}])[0]
                            for part in (candidate.get('content') or {}).get('parts', []):
                                text = part.get('text')
                                if isinstance(text, str):
                                    if part.get('thought'):
                                        summary += text
                                    else:
                                        answer += text
                            if len(answer.encode()) > MAX_REPLY_BYTES or len(summary.encode()) > MAX_REPLY_BYTES:
                                raise RuntimeError('Gemini streamed reply exceeds 2 MiB')
                            data = {**data, **chunk}
                            if progress is not None:
                                if answer: progress.put('answer', 'text', answer, status='running')
                                if summary: progress.put('reasoning', 'reasoning_summary', summary, status='running')
                                progress.usage.update(parse_answer({**data, 'candidates': [{'content': {'parts': [{'text': answer}]}}]})['usage'])
                        candidate = (data.get('candidates') or [{}])[0]
                        data['candidates'] = [{**candidate, 'content': {'parts': [{'text': answer}]}}]
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
            return parse_answer(data)
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
            raise RuntimeError(last_http_error)
        if attempt < 3 and not scope and status != 429:
            await sleep(min(30, max(2 ** (attempt + 1), provider.retry_after(headers, data) or 0)))
    raise RuntimeError("Gemini 暂时不可用或请求额度不足，请稍后重新提交")


async def run_job(pb, client, job_id: str, execution_id: str, pool_factory, work_dir: Path):
    path = f"jobs/{job_id}"
    identity = {"executionId": execution_id}
    job = await pb.post(f"{path}/claim", identity, retries=3)
    pool = await pool_factory(job["options"])
    job_log = AgentTrace(job=job_id, secrets=pool.keys, prefix='[gemini]')
    job_log.emit('job_started', model=job['model'], segments=len(job['messages']),
                 concurrency=job['options']['concurrency'])
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
            try:
                # Not retried: a lost permit is not permission to send twice.
                permit = await pb.post(f"{endpoint}/start", identity)
                if not permit.get("send"):
                    counts['skipped'] += 1
                    log.emit('segment_skipped', reason='no send permit')
                    continue
                log.emit('segment_started', model=job['model'])
                async def upload_progress(body):
                    return await pb.post(f"{endpoint}/progress", body, retries=2)
                progress = MessageProgress(secrets=pool.keys, post=upload_progress, identity=identity,
                    target=work_dir / f"{message['id']}.progress.json", job_id=job_id, message_id=message['id'])
                progress_task = asyncio.create_task(progress.pump())
                stage = 'generation'
                try:
                    if is_antigravity(job['model']):
                        async def checkpoint(state=None):
                            return await pb.post(f"{endpoint}/checkpoint", {**identity, **({'state': state} if state is not None else {})}, retries=3)
                        result = await generate(client, pool, job, message, checkpoint=checkpoint, progress=progress)
                    else:
                        result = await generate(client, pool, job, message, progress=progress)
                except AgentCanceled:
                    counts['canceled'] += 1
                    log.emit('segment_canceled')
                    continue
                except Exception as error:
                    # Never include provider bodies, credentials, or source text in logs.
                    detail = str(error)
                    secrets = [*pool.keys, os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '')]
                    for secret in secrets:
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    result = {"reply": '\n\n'.join(p['content'] for p in progress.parts.values() if p['type'] == 'text').encode('utf-8')[:MAX_REPLY_BYTES].decode('utf-8', errors='ignore'), "error": detail[:1800], "finishReason": "", "usage": progress.usage.copy()}
                finally:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                usages.append(result.get('usage') or {})
                log_result(log, result)
                stage = 'recovery_write'
                saved = {**identity, **result}
                target = work_dir / f"{message['id']}.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps({"jobId": job_id, "messageId": message["id"], "result": saved}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
                stage = 'progress_persistence'
                progress.finalize(result)
                await progress.flush()
                stage = 'persistence'
                # Retrying persistence cannot trigger another Gemini request.
                await pb.post(f"{endpoint}/complete", saved, retries=5)
                target.unlink()
                if progress.target and progress.target.exists(): progress.target.unlink()
                counts['saved'] += 1
                log.emit('segment_saved', status='failed' if result['error'] else 'succeeded')
                if result["error"]:
                    failures.append(message["id"])
            except Exception as error:
                failures.append(message["id"])
                log.emit('segment_error', stage=stage, error_type=type(error).__name__,
                         error=log.redact(str(error))[:1800], recovery_available=(work_dir / f"{message['id']}.json").exists())
            finally:
                queue.task_done()

    try:
        await asyncio.gather(*(worker() for _ in range(job["options"]["concurrency"])))
    finally:
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
        await progress.flush()
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

        return await run_job(pb, client, job_id, execution_id, pool_factory, Path("work/gemini/recovery"))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (RuntimeError, KeyError) as error:
        print(f"[gemini] setup failed: {error}", flush=True)
        raise SystemExit(1) from None
