"""Fast-model helper (default: gemini-3.5-flash-lite) for lightweight Agent side-tasks.

All helpers are strictly best-effort and bounded by short timeouts so they
never block, delay, or fail the primary Antigravity Agent execution.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re

import httpx

DEFAULT_FAST_MODEL = os.environ.get('GEMINI_FAST_MODEL', '').strip() or 'gemini-3.5-flash-lite'
FAST_MODEL_FALLBACK = 'gemini-flash-lite-latest'
GENERATE_CONTENT_ROOT = 'https://generativelanguage.googleapis.com/v1beta/models'
SUMMARY_INTERVAL_SECONDS = max(30, int(os.environ.get('GEMINI_SUMMARY_INTERVAL_SECONDS', '120') or '120'))


def resolve_fast_model(job: dict | None = None) -> str:
    if isinstance(job, dict):
        options = job.get('options')
        if isinstance(options, dict):
            configured = str(options.get('fastModel') or '').strip()
            if configured:
                return configured
    return DEFAULT_FAST_MODEL


def should_generate_title(job: dict) -> bool:
    """Only generate a title for the first turn when no AI title exists yet."""
    if not isinstance(job, dict):
        return False
    if job.get('titleGenerated') is True or job.get('aiTitleGenerated') is True:
        return False
    if str(job.get('parentMessage') or '').strip():
        return False
    if job.get('history'):
        return False
    messages = job.get('messages') or []
    if len(messages) > 1:
        return False
    thread_root = str(job.get('threadRoot') or '').strip()
    job_id = str(job.get('id') or '').strip()
    if thread_root and job_id and thread_root != job_id:
        return False
    prompt = str(job.get('prompt') or '').strip()
    if not prompt and messages and isinstance(messages[0], dict):
        prompt = str(messages[0].get('inputText') or '').strip()
    return bool(prompt)


def sanitize_title(raw: str, max_chars: int = 36) -> str:
    text = str(raw or '').strip()
    if not text:
        return ''
    text = text.splitlines()[0].strip()
    text = re.sub(r'^[#*\-\s"\'「『《【]+', '', text)
    text = re.sub(r'["\'」』》】。.!！?？:：;；\s]+$', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _extract_candidate_text(data: dict) -> str:
    candidates = data.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        return ''
    content = candidates[0].get('content') if isinstance(candidates[0], dict) else None
    parts = content.get('parts') if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ''
    chunks = [str(part.get('text') or '') for part in parts if isinstance(part, dict) and not part.get('thought')]
    return ''.join(chunks).strip()


async def call_fast_model(
    client: httpx.AsyncClient,
    keys: list[str] | tuple[str, ...],
    prompt: str,
    *,
    system_instruction: str = '',
    model: str | None = None,
    response_mime_type: str | None = None,
    timeout: float = 20.0,
    max_output_tokens: int = 512,
    temperature: float = 0.2,
) -> str | None:
    if not keys or not prompt.strip():
        return None
    primary_model = (model or DEFAULT_FAST_MODEL).strip() or DEFAULT_FAST_MODEL
    models_to_try = [primary_model]
    if FAST_MODEL_FALLBACK and FAST_MODEL_FALLBACK != primary_model:
        models_to_try.append(FAST_MODEL_FALLBACK)

    generation_config: dict = {
        'temperature': temperature,
        'maxOutputTokens': max_output_tokens,
    }
    if response_mime_type:
        generation_config['responseMimeType'] = response_mime_type

    payload: dict = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': generation_config,
    }
    if system_instruction.strip():
        payload['systemInstruction'] = {'parts': [{'text': system_instruction.strip()}]}

    for model_name in models_to_try:
        url = f'{GENERATE_CONTENT_ROOT}/{model_name}:generateContent'
        for key in list(keys)[:3]:
            try:
                async with asyncio.timeout(timeout):
                    response = await client.post(
                        url,
                        headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                        json=payload,
                        timeout=timeout,
                    )
                if response.status_code == 200:
                    text = _extract_candidate_text(response.json())
                    if text:
                        return text
                    break
                if response.status_code in (404, 503):
                    # Try fallback model on 404/503
                    break
                if response.status_code in (401, 403, 429):
                    # Try next key
                    continue
                break
            except (httpx.HTTPError, TimeoutError, ValueError):
                continue
    return None


async def generate_title(
    client: httpx.AsyncClient,
    keys: list[str] | tuple[str, ...],
    user_text: str,
    *,
    model: str | None = None,
) -> str | None:
    snippet = user_text.strip()[:2000]
    if not snippet:
        return None
    system = (
        '你是一个对话标题生成器。根据用户的首条消息，生成一个简短、准确、概括主题的对话标题。'
        '要求：1. 使用与用户消息相同的语言；2. 中文控制在4到16个字以内（英文不超过6个单词，总长度不超过30个字符）；'
        '3. 只输出标题纯文本，不要加引号、书名号、Markdown标记或句尾标点。'
    )
    raw = await call_fast_model(
        client,
        keys,
        f'请为以下用户消息生成对话标题：\n\n{snippet}',
        system_instruction=system,
        model=model,
        timeout=15.0,
        max_output_tokens=64,
        temperature=0.2,
    )
    title = sanitize_title(raw or '')
    return title or None


def _parse_summary_json(raw: str) -> dict | None:
    text = str(raw or '').strip()
    if not text:
        return None
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        brace_match = re.search(r'\{.*\}', text, flags=re.S)
        if not brace_match:
            return {'overview': text[:600], 'sentSummary': '', 'actions': [], 'completed': [], 'issues': []}
        try:
            data = json.loads(brace_match.group(0))
        except ValueError:
            return {'overview': text[:600], 'sentSummary': '', 'actions': [], 'completed': [], 'issues': []}
    if not isinstance(data, dict):
        return None

    def _str_list(val) -> list[str]:
        if isinstance(val, list):
            return [str(item).strip()[:300] for item in val if str(item).strip()][:12]
        if isinstance(val, str) and val.strip():
            return [val.strip()[:300]]
        return []

    return {
        'overview': str(data.get('overview') or '').strip()[:600],
        'sentSummary': str(data.get('sentSummary') or '').strip()[:400],
        'actions': _str_list(data.get('actions')),
        'completed': _str_list(data.get('completed')),
        'issues': _str_list(data.get('issues')),
    }


def _build_transcript(user_prompt: str, history: list | None, parts: dict | list) -> str:
    sections = []
    if history:
        hist_lines = []
        for item in history[-6:]:
            if isinstance(item, dict):
                role = str(item.get('role') or 'user')
                text = str(item.get('text') or '').strip()[:600]
                if text:
                    hist_lines.append(f'[{role}] {text}')
        if hist_lines:
            sections.append('## 历史对话\n' + '\n'.join(hist_lines))
    if user_prompt.strip():
        sections.append('## 当前用户发送内容\n' + user_prompt.strip()[:3000])

    part_list = list(parts.values()) if isinstance(parts, dict) else list(parts or [])
    part_list = sorted(
        (p for p in part_list if isinstance(p, dict) and p.get('type') != 'status'),
        key=lambda p: p.get('sequence', 0),
    )
    step_lines = []
    for p in part_list[-40:]:
        kind = p.get('type', '')
        tool_name = p.get('toolName') or p.get('providerType') or ''
        status = p.get('status', '')
        content = str(p.get('content') or '').strip().replace('\n', ' ')[:500]
        prefix = f'[{kind}' + (f':{tool_name}' if tool_name else '') + (f'/{status}' if status else '') + ']'
        if content:
            step_lines.append(f'{prefix} {content}')
    if step_lines:
        sections.append('## 当前 Agent 执行步骤与输出\n' + '\n'.join(step_lines))
    transcript = '\n\n'.join(sections)
    if len(transcript) > 20000:
        transcript = transcript[-20000:]
    return transcript


async def summarize_agent_progress(
    client: httpx.AsyncClient,
    keys: list[str] | tuple[str, ...],
    *,
    user_prompt: str,
    history: list | None,
    parts: dict | list,
    step_count: int,
    status: str = 'running',
    model: str | None = None,
) -> dict | None:
    transcript = _build_transcript(user_prompt, history, parts)
    if not transcript.strip():
        return None
    used_model = (model or DEFAULT_FAST_MODEL).strip() or DEFAULT_FAST_MODEL
    system = (
        '你是 Agent 运行进度总结助手。请用简洁清晰的中文，总结当前 Agent 的执行情况。'
        '必须返回严格合法的 JSON 对象，包含以下字段：\n'
        '- "overview": 一句话概括当前整体进展（不超过80字）\n'
        '- "sentSummary": 简述用户当前发送了什么需求/任务（不超过80字）\n'
        '- "actions": 字符串数组，列出已经执行的关键操作/工具调用（每条不超过60字，最多6条）\n'
        '- "completed": 字符串数组，列出已经完成的内容或阶段性成果（每条不超过60字，最多6条）\n'
        '- "issues": 字符串数组，列出遇到的报错、阻塞或待确认问题；如果没有问题则返回空数组 []'
    )
    raw = await call_fast_model(
        client,
        keys,
        f'当前运行状态：{status}，已记录步骤数：{step_count}\n\n{transcript}',
        system_instruction=system,
        model=used_model,
        response_mime_type='application/json',
        timeout=20.0,
        max_output_tokens=768,
        temperature=0.2,
    )
    parsed = _parse_summary_json(raw or '')
    if not parsed:
        return None
    return {
        'updatedAt': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'stepCount': step_count,
        'model': used_model,
        'status': status,
        **parsed,
    }
