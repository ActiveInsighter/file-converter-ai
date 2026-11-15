"""Bounded text-only Gemini fan-out. PocketBase owns inputs and every saved reply.

No Files API, OCR, image requests or PDF transcription rules are used here.
A saved result retries only its PB write, never the paid generation. A second
Actions attempt cannot re-claim a partially executed job and silently resend it.
"""
from __future__ import annotations

import asyncio
import argparse
import base64
import json
from html.parser import HTMLParser
import os
import re
import time
from urllib.parse import urlsplit
from pathlib import Path

import httpx

from key_pool import KeyPool, QuotaPoolExhaustedError, parse_key_groups
from providers import GeminiProvider
from gemini_agent import AgentCanceled, generate_agent, is_antigravity

MAX_REPLY_BYTES = 2 * 1024 * 1024

# Current Gemini text models support Google Search grounding. Do not add the
# tool to arbitrary user-saved model IDs (image/audio/legacy models may reject it).
# Google Search is available for these Gemini API families, including
# Gemini 4 Hydrogen (confirmed in Google's Firebase AI Logic documentation).
# Availability on a given API key/project/tier is a separate runtime check.
SEARCHABLE_CHAT_MODEL = re.compile(
    r'^gemini-(?:2\.(?:0-flash|5-(?:pro|flash(?:-lite)?))|'
    r'3\.(?:1-(?:pro|flash-lite)|5-flash(?:-lite)?|6-flash|7-flash|8-flash)|'
    r'3-(?:pro|flash)|4-(?:hydrogen|argon|carbon))(?=$|-)', re.I)
NON_CHAT_MODEL = re.compile(r'image|audio|tts|embedding|live|speech|video', re.I)


def supports_google_search(model: str) -> bool:
    value = str(model).lower().strip()
    return bool(SEARCHABLE_CHAT_MODEL.match(value)) and not NON_CHAT_MODEL.search(value)


def safe_error_code(data: dict) -> str:
    """Only expose machine-readable Google status, never provider message text."""
    error = data.get('error') if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return 'UNKNOWN'
    value = error.get('status') or error.get('code')
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
        return value.upper()
    return 'UNKNOWN'


def search_fallback_allowed(status: int, data: dict, seconds: float) -> bool:
    """Retry only a definite, fast rejection: never duplicate a possibly billed tool run."""
    error = data.get('error') if isinstance(data, dict) else None
    message = str(error.get('message', '')).casefold() if isinstance(error, dict) else ''
    if 'too many tool calls' in message or 'quota' in message or 'rate limit' in message:
        return False
    if status in (400, 403) and any(word in message for word in
                                     ('google search', 'google_search', 'googlesearch',
                                      'grounding', 'search tool')):
        return True
    return (status == 400 and seconds <= 10 and
            not data.get('usageMetadata') and safe_error_code(data) in
            ('INVALID_ARGUMENT', 'FAILED_PRECONDITION', '400', 'UNKNOWN'))



def safe_search_link(url: object) -> str | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return None
    if any(char in url for char in '\r\n<>'):
        return None
    return url.replace(')', '%29').replace('(', '%28')


def link_label(text: object) -> str:
    return re.sub(r'[\[\]\r\n]', ' ', str(text or '来源'))[:100].strip() or '来源'


class _SearchSuggestionLinks(HTMLParser):
    """Read clickable suggestions from Google's supplied renderedContent without rendering raw HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current_url = None
        self.current_label = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current_url = safe_search_link(dict(attrs).get('href'))
            self.current_label = []

    def handle_data(self, data):
        if self.current_url:
            self.current_label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current_url:
            self.links.append((link_label(' '.join(self.current_label)), self.current_url))
            self.current_url = None
            self.current_label = []



def grounding_appendix(candidate: dict) -> str:
    """Preserve Google grounding sources and Search Suggestions as clickable links.

    Use Google's returned URLs (not a model-generated URL). The standard
    searchEntryPoint.sdkBlob contains [query, url] pairs; render them as
    Google Search suggestions in Markdown, which the existing chat UI renders.
    """
    grounding = candidate.get('groundingMetadata') or {}
    if not isinstance(grounding, dict):
        return ''
    lines = []
    sources = []
    for chunk in grounding.get('groundingChunks') or []:
        if not isinstance(chunk, dict) or not isinstance(chunk.get('web'), dict):
            continue
        web = chunk['web']
        url = safe_search_link(web.get('uri'))
        if url and url not in {item[1] for item in sources}:
            sources.append((link_label(web.get('title')), url))
        if len(sources) >= 12:
            break
    if sources:
        lines.extend(['**Google 搜索参考来源**', *[f'- [{title}]({url})' for title, url in sources]])
    entry = grounding.get('searchEntryPoint') or {}
    suggestions = []
    if isinstance(entry, dict) and isinstance(entry.get('sdkBlob'), str):
        try:
            blob = base64.b64decode(entry['sdkBlob'], validate=True)
            if len(blob) <= 32768:
                rows = json.loads(blob)
                if isinstance(rows, list):
                    for item in rows[:8]:
                        if isinstance(item, list) and len(item) >= 2:
                            url = safe_search_link(item[1])
                            if url:
                                suggestions.append((link_label(item[0]), url))
        except (ValueError, UnicodeDecodeError, TypeError):
            pass
    if not suggestions and isinstance(entry, dict):
        rendered = entry.get('renderedContent')
        if isinstance(rendered, str) and len(rendered) <= 65536:
            extractor = _SearchSuggestionLinks()
            extractor.feed(rendered)
            suggestions = extractor.links[:8]
    if suggestions:
        if lines:
            lines.append('')
        lines.extend(['**Google 搜索建议**', *[f'- [{title}]({url})' for title, url in suggestions]])
    return '\n'.join(lines)


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
    if supports_google_search(job["model"]):
        # Model decides whether a query needs web search; this does not force
        # a search on every turn. Keep OCR/PDF requests completely untouched.
        payload["tools"] = [{"googleSearch": {}}]
    return payload


def parse_answer(payload: dict) -> dict:
    candidate = (payload.get("candidates") or [{}])[0]
    reason = str(candidate.get("finishReason") or "")[:64]
    reply = "\n".join(part["text"] for part in (candidate.get("content") or {}).get("parts", [])
                      if isinstance(part.get("text"), str) and not part.get("thought"))
    error = ""
    appendix = grounding_appendix(candidate)
    if appendix:
        reply += "\n\n" + appendix
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
    grounding = candidate.get('groundingMetadata') or {}
    if isinstance(grounding, dict) and isinstance(grounding.get('webSearchQueries'), list):
        usage['googleSearchCount'] = len(grounding['webSearchQueries'])
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


async def generate(client, pool, job, message, *, sleep=asyncio.sleep, checkpoint=None) -> dict:
    if is_antigravity(job['model']):
        if checkpoint is None:
            raise RuntimeError('Agent requires durable execution state')
        return await generate_agent(client, pool, job, message, checkpoint=checkpoint, sleep=sleep)
    provider = GeminiProvider()
    payload = build_payload(job, message)
    search_fallback_used = False
    search_error_code = ''
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
        attempt_started = time.monotonic()
        try:
            async with asyncio.timeout(180):
                # Gemini responses are bounded too, even for a chunked HTTP response.
                async with client.stream("POST", provider.endpoint(provider.default_base_url, job["model"]),
                        headers=provider.build_headers(key), json=payload, timeout=180) as response:
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 3 * MAX_REPLY_BYTES:
                            raise RuntimeError("Gemini response exceeds 6 MiB")
                    status, headers = response.status_code, response.headers
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
            result = parse_answer(data)
            if search_fallback_used:
                result['reply'] = ('*Google 搜索暂时不可用，本次已自动切换为不联网回答。*'
                                   '\n\n' + result['reply'])
                result['usage']['googleSearchFallback'] = True
                result['usage']['googleSearchErrorCode'] = search_error_code
            return result
        error_code = safe_error_code(data)
        last_http_error = (f"Gemini 拒绝请求（HTTP {status}，错误类型 {error_code}），"
                           "请检查模型、工具权限、密钥权限或项目额度")
        search_used = bool(payload.get('tools'))
        print(f'[gemini] upstream_http={status} error_code={error_code} '
              f'model={job["model"]} search_enabled={search_used}', flush=True)
        await pool.mark_error(index, lease, http_status=status)
        # A search tool may be unavailable for a specific Google project even
        # if the model is on the public support list. A definite rejected 400
        # contains no generated output, so retry ONCE without tools; do not
        # replay 429s, network timeouts or errors after a long tool loop.
        if (search_used and not search_fallback_used and
                search_fallback_allowed(status, data, time.monotonic() - attempt_started)):
            payload.pop('tools', None)
            search_fallback_used = True
            search_error_code = error_code
            print(f'[gemini] search_fallback=enabled rejected_http={status} '
                  f'error_code={error_code} retry_without_tool=1', flush=True)
            continue
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
    print(f"[gemini] job claimed model={job['model']} segments={len(job['messages'])} "
          f"concurrency={job['options']['concurrency']}", flush=True)
    pool = await pool_factory(job["options"])
    queue = asyncio.Queue()
    for message in job["messages"]:
        if message["status"] == "queued":
            queue.put_nowait(message)
    work_dir.mkdir(parents=True, exist_ok=True)
    failures = []

    async def worker():
        while not queue.empty():
            message = queue.get_nowait()
            endpoint = f"{path}/messages/{message['id']}"
            try:
                begun = time.monotonic()
                # Not retried: a lost permit is not permission to send twice.
                permit = await pb.post(f"{endpoint}/start", identity)
                if not permit.get("send"):
                    print(f"[gemini] segment={message['index']} skipped: no send permit", flush=True)
                    continue
                print(f"[gemini] segment={message['index']} started agent={is_antigravity(job['model'])}", flush=True)
                try:
                    if is_antigravity(job['model']):
                        async def checkpoint(state=None):
                            return await pb.post(f"{endpoint}/checkpoint", {**identity, **({'state': state} if state is not None else {})}, retries=3)
                        result = await generate(client, pool, job, message, checkpoint=checkpoint)
                    else:
                        result = await generate(client, pool, job, message)
                except AgentCanceled:
                    continue
                except Exception as error:
                    # Never include provider bodies, credentials, or source text in logs.
                    detail = str(error)
                    secrets = [*pool.keys, os.environ.get('ANTIGRAVITY_GITHUB_TOKEN', '')]
                    for secret in secrets:
                        if secret:
                            detail = detail.replace(secret, "[REDACTED]")
                    result = {"reply": "", "error": detail[:1800], "finishReason": "", "usage": {}}
                saved = {**identity, **result}
                target = work_dir / f"{message['id']}.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps({"jobId": job_id, "messageId": message["id"], "result": saved}, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
                # Retrying persistence cannot trigger another Gemini request.
                await pb.post(f"{endpoint}/complete", saved, retries=5)
                target.unlink()
                usage = result.get('usage') or {}
                print(f"[gemini] segment={message['index']} saved "
                      f"status={'failed' if result['error'] else 'succeeded'} "
                      f"elapsed_seconds={time.monotonic() - begun:.2f} "
                      f"total_tokens={usage.get('totalTokenCount', 'unknown')} "
                      f"input_tokens={usage.get('promptTokenCount', 'unknown')} "
                      f"output_tokens={usage.get('candidatesTokenCount', 'unknown')} "
                      f"thought_tokens={usage.get('thoughtsTokenCount', 'unknown')} "
                      f"tool_tokens={usage.get('toolUseTokenCount', 'unknown')} "
                      f"steps={usage.get('agentStepCount', 'unknown')}", flush=True)
                if result["error"]:
                    failures.append(message["id"])
            except Exception:
                failures.append(message["id"])
                print(f"[gemini] segment={message['index']} persistence/permit failed; check PB and recovery artifact", flush=True)
            finally:
                queue.task_done()

    try:
        await asyncio.gather(*(worker() for _ in range(job["options"]["concurrency"])))
    finally:
        await pool.close()
    return 2 if failures else 0


async def recover(pb, directory: Path):
    """Flush recovery artifacts using their original identity; never generate text."""
    for path in sorted(directory.glob('*.json')):
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
