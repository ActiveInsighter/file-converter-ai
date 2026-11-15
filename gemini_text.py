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

from key_pool import KeyPool, parse_key_groups
from providers import GeminiProvider

MAX_REPLY_BYTES = 2 * 1024 * 1024


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
    def __init__(self, client: httpx.AsyncClient, root: str):
        self.client, self.root, self.token = client, root.rstrip("/"), ""

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
                response = await self.client.post(f"{self.root}/api/anyworkflow/gemini/{path}",
                    headers={"Authorization": self.token}, json=data, timeout=60)
                if response.is_success:
                    return response.json()
                if response.status_code < 500 or attempt == retries:
                    raise RuntimeError(f"PB request failed (HTTP {response.status_code})")
            except httpx.TransportError:
                if attempt == retries:
                    raise RuntimeError("PB connection failed; no model request was re-sent") from None
            await asyncio.sleep(min(16, 2 ** attempt))


async def generate(client, pool, job, message, *, sleep=asyncio.sleep) -> dict:
    provider = GeminiProvider()
    payload = build_payload(job, message)
    for attempt in range(4):
        index, key, lease = await pool.acquire()
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
            return parse_answer(data)
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
            raise RuntimeError(f"Gemini 拒绝请求（HTTP {status}），请检查模型及参数")
        if attempt < 3 and not scope and status != 429:
            await sleep(min(30, max(2 ** (attempt + 1), provider.retry_after(headers, data) or 0)))
    raise RuntimeError("Gemini 暂时不可用或请求额度不足，请稍后重新提交")


async def run_job(pb, client, job_id: str, execution_id: str, pool_factory, work_dir: Path):
    path = f"jobs/{job_id}"
    identity = {"executionId": execution_id}
    job = await pb.post(f"{path}/claim", identity, retries=3)
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
                # Not retried: a lost permit is not permission to send twice.
                permit = await pb.post(f"{endpoint}/start", identity)
                if not permit.get("send"):
                    continue
                try:
                    result = await generate(client, pool, job, message)
                except Exception as error:
                    # Never include provider bodies, credentials, or source text in logs.
                    detail = str(error)
                    for secret in pool.keys:
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
                print(f"[gemini] segment={message['index']} saved status={'failed' if result['error'] else 'succeeded'}", flush=True)
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
