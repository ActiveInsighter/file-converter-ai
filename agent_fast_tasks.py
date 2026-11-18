"""Fast-model auxiliary tasks (title generation, periodic summaries, timing milestones).

All tasks in this module are strictly non-blocking and best-effort: failures
are logged as safe telemetry and never interrupt or fail the main Antigravity
Agent execution flow.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import time
from urllib.parse import quote

import httpx

from providers import GeminiProvider

DEFAULT_FAST_MODEL = os.environ.get('GEMINI_FAST_MODEL', 'gemini-3.5-flash-lite').strip() or 'gemini-3.5-flash-lite'
DEFAULT_SUMMARY_INTERVAL_SECONDS = 120.0
MAX_TITLE_CHARS = 24
MAX_SUMMARY_INPUT_CHARS = 24000
MAX_TIMING_STEPS = 100


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def resolve_fast_model(job: dict) -> str:
    options = job.get('options') or {}
    agent_opts = options.get('agent') or {}
    candidate = str(agent_opts.get('fastModel') or options.get('fastModel') or '').strip()
    return candidate or DEFAULT_FAST_MODEL


def resolve_summary_interval(job: dict) -> float:
    options = job.get('options') or {}
    agent_opts = options.get('agent') or {}
    raw = agent_opts.get('summaryIntervalSeconds') or os.environ.get('AGENT_SUMMARY_INTERVAL_SECONDS')
    try:
        value = float(raw)
        if 15.0 <= value <= 3600.0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_SUMMARY_INTERVAL_SECONDS


def is_continuation_job(job: dict) -> bool:
    if job.get('history'):
        return True
    if str(job.get('parentMessage') or '').strip():
        return True
    thread_root = str(job.get('threadRoot') or '').strip()
    job_id = str(job.get('id') or '').strip()
    if thread_root and job_id and thread_root != job_id:
        return True
    return False


def _extract_reply_text(data: dict) -> str:
    candidate = (data.get('candidates') or [{}])[0]
    parts = (candidate.get('content') or {}).get('parts') or []
    return '\n'.join(
        part['text'] for part in parts
        if isinstance(part, dict) and isinstance(part.get('text'), str) and not part.get('thought')
    ).strip()


async def call_fast_model(
    client: httpx.AsyncClient,
    pool,
    model: str,
    prompt: str,
    *,
    system_instruction: str = '',
    max_output_tokens: int = 512,
    timeout: float = 30.0,
) -> str | None:
    if pool is None or not getattr(pool, 'keys', None):
        return None
    provider = GeminiProvider()
    payload: dict = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': max_output_tokens,
        },
    }
    if system_instruction:
        payload['systemInstruction'] = {'parts': [{'text': system_instruction}]}

    index = key = lease = None
    try:
        index, key, lease = await asyncio.wait_for(pool.acquire(), timeout=15.0)
        endpoint = provider.endpoint(provider.default_base_url, model)
        async with asyncio.timeout(timeout):
            async with client.stream(
                'POST',
                endpoint,
                headers=provider.build_headers(key),
                json=payload,
                timeout=timeout,
            ) as response:
                status = response.status_code
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 512 * 1024:
                        break
        if status == 200:
            await pool.mark_success(index, lease)
            data = json.loads(raw)
            return _extract_reply_text(data)
        await pool.mark_error(index, lease, http_status=status)
        print(f'[agent.fast] http={status} model={model}', flush=True)
        return None
    except asyncio.CancelledError:
        if index is not None and lease is not None:
            await pool.mark_cancelled(index, lease)
        raise
    except Exception as error:
        if index is not None and lease is not None:
            await pool.mark_error(index, lease)
        print(f'[agent.fast] failed model={model} error_type={type(error).__name__}', flush=True)
        return None


async def _pb_get_record(pb, collection: str, record_id: str, fields: str) -> dict | None:
    if pb is None or not getattr(pb, 'root', None) or not getattr(pb, 'token', None) or not record_id:
        return None
    try:
        resp = await pb.client.get(
            f"{pb.root}/api/collections/{collection}/records/{quote(str(record_id), safe='')}",
            headers={'Authorization': pb.token},
            params={'fields': fields},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    return None


async def _pb_patch_record(pb, collection: str, record_id: str, body: dict) -> bool:
    if pb is None or not getattr(pb, 'root', None) or not getattr(pb, 'token', None) or not record_id:
        return False
    try:
        resp = await pb.client.patch(
            f"{pb.root}/api/collections/{collection}/records/{quote(str(record_id), safe='')}",
            headers={'Authorization': pb.token},
            json=body,
            timeout=15,
        )
        return bool(resp.is_success)
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def clean_generated_title(raw: str) -> str:
    text = (raw or '').strip()
    if not text:
        return ''
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), '')
    cleaned = re.sub(r'^[#*\-\s"\'“”‘’《》【】「」]+|[#*\-\s"\'“”‘’《》【】「」。，,.!?！？:：]+$', '', first_line)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if len(cleaned) > MAX_TITLE_CHARS:
        cleaned = cleaned[:MAX_TITLE_CHARS].rstrip()
    return cleaned


async def maybe_generate_title(
    client: httpx.AsyncClient,
    pool,
    pb,
    job: dict,
    message: dict,
) -> str | None:
    """Generate and overwrite the conversation title for a brand-new conversation."""
    job_id = str(job.get('id') or '').strip()
    if not job_id or is_continuation_job(job):
        return None

    record = await _pb_get_record(pb, 'aw_gemini_jobs', job_id, 'id,title,titleSource')
    if record is None or str(record.get('titleSource') or '').strip().lower() == 'ai':
        return None

    user_text = '\n'.join(
        part for part in (str(job.get('prompt') or '').strip(), str(message.get('inputText') or '').strip()) if part
    ).strip()
    if not user_text:
        return None

    model = resolve_fast_model(job)
    prompt = (
        "请根据下面用户的首条消息，生成一个简洁、准确的对话标题。\n"
        "要求：\n"
        "1. 使用与用户消息相同的语言（中文消息用中文，英文消息用英文）。\n"
        "2. 长度严格控制在 6 到 16 个汉字（或 3 到 7 个英文单词）以内。\n"
        "3. 只输出标题文本本身，不要包含引号、书名号、Markdown、前缀或句末标点。\n\n"
        f"用户消息：\n{user_text[:2000]}"
    )
    reply = await call_fast_model(client, pool, model, prompt, max_output_tokens=64, timeout=25.0)
    title = clean_generated_title(reply or '')
    if not title:
        return None

    ok = await _pb_patch_record(pb, 'aw_gemini_jobs', job_id, {'title': title, 'titleSource': 'ai'})
    if not ok:
        # Fallback if titleSource field has not been migrated in PocketBase yet.
        ok = await _pb_patch_record(pb, 'aw_gemini_jobs', job_id, {'title': title})
    if ok:
        print(f'[agent.fast] title_updated job={job_id} model={model} chars={len(title)}', flush=True)
    return title if ok else None


class AgentTimingTracker:
    """Tracks key execution timing milestones and step arrival offsets."""

    def __init__(self, *, clock=time.monotonic, t0: float | None = None, submitted_at: str | None = None):
        self.clock = clock
        self.t0 = t0 if t0 is not None else clock()
        self.submitted_at = submitted_at or _utc_iso()
        self.dispatched_at: str | None = None
        self.dispatch_seconds: float | None = None
        self.first_reply_at: str | None = None
        self.first_reply_seconds: float | None = None
        self.completed_at: str | None = None
        self.total_seconds: float | None = None
        self.status: str = 'running'
        self._steps: dict[int, dict] = {}
        self._last_step_elapsed: float = 0.0
        self._dirty = False

    def _elapsed(self) -> float:
        return round(max(0.0, self.clock() - self.t0), 2)

    def mark_dispatched(self) -> None:
        if self.dispatched_at is None:
            self.dispatched_at = _utc_iso()
            self.dispatch_seconds = self._elapsed()
            self._dirty = True

    def mark_first_reply(self) -> None:
        if self.first_reply_at is None:
            self.first_reply_at = _utc_iso()
            self.first_reply_seconds = self._elapsed()
            self._dirty = True

    def record_step(self, index: int, step_type: str, *, label: str = '', status: str = 'completed') -> None:
        self.mark_first_reply()
        existing = self._steps.get(index)
        if existing is None:
            if len(self._steps) >= MAX_TIMING_STEPS:
                return
            elapsed = self._elapsed()
            baseline = self.first_reply_seconds if self._steps else (self.dispatch_seconds or 0.0)
            prev = self._last_step_elapsed if self._steps else (baseline or 0.0)
            delta = round(max(0.0, elapsed - prev), 2)
            self._last_step_elapsed = elapsed
            self._steps[index] = {
                'index': index,
                'type': str(step_type or 'step')[:48],
                'label': str(label or '')[:80],
                'elapsedSeconds': elapsed,
                'deltaSeconds': delta,
                'status': status,
            }
            self._dirty = True
        else:
            if status != existing.get('status') or (label and not existing.get('label')):
                existing['status'] = status
                if label and not existing.get('label'):
                    existing['label'] = str(label)[:80]
                self._dirty = True

    def mark_completed(self, status: str) -> None:
        self.completed_at = _utc_iso()
        self.total_seconds = self._elapsed()
        self.status = str(status or 'completed')
        self._dirty = True

    def to_dict(self) -> dict:
        return {
            'submittedAt': self.submitted_at,
            'dispatchedAt': self.dispatched_at,
            'dispatchSeconds': self.dispatch_seconds,
            'firstReplyAt': self.first_reply_at,
            'firstReplySeconds': self.first_reply_seconds,
            'completedAt': self.completed_at,
            'totalSeconds': self.total_seconds,
            'status': self.status,
            'steps': [self._steps[idx] for idx in sorted(self._steps)],
        }

    async def sync_if_dirty(self, pb, message_id: str, *, force: bool = False) -> bool:
        if not message_id or (not self._dirty and not force):
            return False
        self._dirty = False
        return await _pb_patch_record(pb, 'aw_gemini_messages', message_id, {'agentTimings': self.to_dict()})


def _parse_summary_json(raw: str) -> dict | None:
    text = (raw or '').strip()
    if not text:
        return None
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)
    candidate = fenced.group(1) if fenced else text
    if not candidate.startswith('{'):
        brace = re.search(r'\{.*\}', candidate, re.S)
        if brace:
            candidate = brace.group(0)
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return {
                'sentWhat': str(obj.get('sentWhat') or '').strip()[:600],
                'actionsDone': [str(x).strip()[:240] for x in (obj.get('actionsDone') or []) if str(x).strip()][:12],
                'completed': [str(x).strip()[:240] for x in (obj.get('completed') or []) if str(x).strip()][:12],
                'issues': [str(x).strip()[:240] for x in (obj.get('issues') or []) if str(x).strip()][:8],
                'summary': str(obj.get('summary') or '').strip()[:800],
            }
    except ValueError:
        pass
    return {
        'sentWhat': '',
        'actionsDone': [],
        'completed': [],
        'issues': [],
        'summary': text[:800],
    }


def build_summary_digest(job: dict, message: dict, progress) -> tuple[str, int]:
    parts_list = []
    step_count = 0
    if progress is not None:
        step_count = int(progress.usage.get('agentStepCount') or 0)
        ordered = sorted(progress.parts.values(), key=lambda p: p.get('sequence', 0))
        for p in ordered:
            ptype = p.get('type', '')
            if ptype == 'status':
                continue
            content = str(p.get('content') or '').strip()
            tool_name = str(p.get('toolName') or '').strip()
            status = str(p.get('status') or '').strip()
            if ptype in ('tool_call', 'tool_result'):
                parts_list.append(f"[{ptype} {tool_name} ({status})]: {content[:400]}")
            elif ptype == 'reasoning_summary':
                parts_list.append(f"[思考摘要]: {content[:400]}")
            elif ptype == 'text':
                parts_list.append(f"[AI回复]: {content[:1200]}")

    history_lines = []
    for turn in (job.get('history') or [])[-6:]:
        role = turn.get('role', 'user')
        text = str(turn.get('text') or '').strip()[:500]
        if text:
            history_lines.append(f"{role}: {text}")

    current_user = '\n'.join(
        p for p in (str(job.get('prompt') or '').strip(), str(message.get('inputText') or '').strip()) if p
    ).strip()[:1500]

    sections = []
    if history_lines:
        sections.append("【历史对话】\n" + "\n".join(history_lines))
    if current_user:
        sections.append("【本轮用户发送】\n" + current_user)
    if parts_list:
        sections.append("【当前执行步骤与输出】\n" + "\n".join(parts_list[-40:]))

    digest = '\n\n'.join(sections)
    if len(digest) > MAX_SUMMARY_INPUT_CHARS:
        digest = digest[-MAX_SUMMARY_INPUT_CHARS:]
    return digest, step_count


class AgentSummaryWorker:
    """Periodically summarizes Agent execution progress using the fast model."""

    def __init__(self, client: httpx.AsyncClient, pool, pb, job: dict, message: dict, progress, *, sleep=asyncio.sleep):
        self.client = client
        self.pool = pool
        self.pb = pb
        self.job = job
        self.message = message
        self.progress = progress
        self.sleep = sleep
        self.interval = resolve_summary_interval(job)
        self.model = resolve_fast_model(job)
        self.last_step_count = 0
        self.has_summarized = False

    async def summarize_once(self, *, force: bool = False) -> dict | None:
        message_id = str(self.message.get('id') or '').strip()
        if not message_id:
            return None
        digest, step_count = build_summary_digest(self.job, self.message, self.progress)
        if not digest.strip():
            return None
        if not force and step_count <= self.last_step_count:
            return None

        prompt = (
            "你是一个执行过程总结助手。请阅读以下 Agent 对话与执行步骤，用简体中文输出严格 JSON 对象，不要输出多余说明。\n"
            "JSON 字段定义：\n"
            '- "sentWhat": 字符串，简述用户本轮发送了什么需求/指令（1句话）\n'
            '- "actionsDone": 字符串数组，列举 Agent 目前做了哪些操作（调用了什么工具、查了什么文件等，最多5条）\n'
            '- "completed": 字符串数组，列举已经完成了什么（最多5条）\n'
            '- "issues": 字符串数组，列举遇到了什么问题、报错或阻塞（没有则传 []）\n'
            '- "summary": 字符串，1-2句话概括当前整体进展\n\n'
            f"{digest}"
        )
        reply = await call_fast_model(self.client, self.pool, self.model, prompt, max_output_tokens=700, timeout=35.0)
        parsed = _parse_summary_json(reply or '')
        if not parsed:
            return None

        parsed['updatedAt'] = _utc_iso()
        parsed['stepCount'] = step_count
        parsed['model'] = self.model
        ok = await _pb_patch_record(self.pb, 'aw_gemini_messages', message_id, {'agentSummary': parsed})
        if ok:
            self.last_step_count = step_count
            self.has_summarized = True
            print(f'[agent.fast] summary_updated msg={message_id} steps={step_count} model={self.model}', flush=True)
        return parsed if ok else None

    async def run(self) -> None:
        while True:
            await self.sleep(self.interval)
            try:
                await self.summarize_once(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f'[agent.fast] summary_loop_error type={type(error).__name__}', flush=True)

    async def finalize(self) -> None:
        digest, step_count = build_summary_digest(self.job, self.message, self.progress)
        if not digest.strip() or step_count <= 0:
            return
        if not self.has_summarized or step_count > self.last_step_count:
            try:
                async with asyncio.timeout(25.0):
                    await self.summarize_once(force=True)
            except Exception as error:
                print(f'[agent.fast] final_summary_skipped type={type(error).__name__}', flush=True)
