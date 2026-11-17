"""Versioned, bounded public response snapshots. No signatures or credentials.

Provider thought=true text and thought.summary are public summaries; arbitrary
thought.content is intentionally excluded. No event stream is required.
"""
from __future__ import annotations

import base64
import json
import os
import re

MAX_BYTES = 2 * 1024 * 1024
MAX_PARTS = 500
MAX_VALUE_CHARS = 64000
SENSITIVE = re.compile(r'(?i)signature|authorization|password|secret|api[_-]?key|access[_-]?token|credential|^token$|^headers$|^environment$')


def sanitize(value, *, secrets=(), depth=0):
    if depth > 12:
        return '[nested value omitted]'
    if isinstance(value, dict):
        safe = {str(k): sanitize(v, secrets=secrets, depth=depth + 1)
                for k, v in list(value.items())[:200] if not SENSITIVE.search(str(k))
                and k not in ('inlineData', 'inline_data')}
        if len(value) > 200:
            safe['_captureOmittedFields'] = len(value) - 200
        return safe
    if isinstance(value, list):
        return [sanitize(v, secrets=secrets, depth=depth + 1) for v in value[:500]] + ([{'_captureOmittedItems': len(value) - 500}] if len(value) > 500 else [])
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if len(secret) >= 4:
            value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AIza[A-Za-z0-9_-]{20,})', '[REDACTED]', value)
    value = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)((?:password|authorization|api[_-]?key|access[_-]?token|token|secret)["\x27]?\s*[:=]\s*["\x27]?)[^\s,"\x27;&}]+', r'\1[REDACTED]', value)
    value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    if value.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(sanitize(json.loads(value), secrets=secrets, depth=depth + 1), ensure_ascii=False)
        except ValueError:
            pass
    return value[:MAX_VALUE_CHARS] + ('\n[truncated]' if len(value) > MAX_VALUE_CHARS else '')


def snapshot(protocol, parts, metadata, *, secrets=()):
    values = list(secrets)
    for name, value in os.environ.items():
        if any(word in name.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
            values.extend(value.splitlines())
    values = sorted({s for s in values if len(s) >= 4}, key=len, reverse=True)
    values += [base64.b64encode(f'x-oauth-basic:{s}'.encode()).decode() for s in values]
    safe_metadata = sanitize(metadata, secrets=values)
    metadata_truncated = any(marker in json.dumps(safe_metadata, ensure_ascii=False) for marker in ('[truncated]', '[nested value omitted]', '_captureOmitted'))
    # A large logprobs/grounding field must not push the whole snapshot over its DB limit.
    for key in list(safe_metadata):
        if len(json.dumps(safe_metadata[key], ensure_ascii=False).encode()) > 128 * 1024:
            safe_metadata[key] = {'omitted': True, 'reason': 'metadata field exceeds 128 KiB'}
            metadata_truncated = True
    result = {'version': 1, 'protocol': protocol, 'parts': [], 'metadata': safe_metadata,
              'capture': {'totalParts': len(parts), 'omittedParts': 0, 'truncated': metadata_truncated}}
    size = len(json.dumps(result, ensure_ascii=False).encode())
    for i, part in enumerate(parts):
        safe = sanitize(part, secrets=values)
        clean = json.dumps(safe, ensure_ascii=False)
        # An explicit marker describes bounded fields, never silently implies a full trace.
        if any(marker in clean for marker in ('[truncated]', '[nested value omitted]', '_captureOmitted')):
            result['capture']['truncated'] = True
        added = len(clean.encode()) + 2
        if i >= MAX_PARTS or size + added > MAX_BYTES - 4096:
            result['capture']['omittedParts'] += 1
            result['capture']['truncated'] = True
            continue
        size += added
        result['parts'].append(safe)
    return result


def generate_content_snapshot(data, *, secrets=()):
    parts = []
    candidates = data.get('candidates') or []
    for ci, candidate in enumerate(candidates):
        for pi, part in enumerate((candidate.get('content') or {}).get('parts', [])):
            base = {'id': f'c{ci}p{pi}', 'candidate': ci}
            if isinstance(part.get('text'), str):
                parts.append({**base, 'type': 'reasoning' if part.get('thought') else 'text', 'text': part['text']})
            for key, kind in [('functionCall', 'tool-call'), ('functionResponse', 'tool-result'),
                              ('toolCall', 'tool-call'), ('toolResponse', 'tool-result'),
                              ('executableCode', 'tool-call'), ('codeExecutionResult', 'tool-result')]:
                if isinstance(part.get(key), dict):
                    value = part[key]
                    default_name = (
                        value.get('toolType')
                        or ('code_execution' if 'Code' in key or 'Execution' in key else key)
                    )
                    parts.append({**base, 'id': base['id'] + key, 'type': kind,
                                  'name': str(value.get('name') or default_name)[:240],
                                  'callId': str(value.get('id', ''))[:256], 'data': value})
            for key in ('fileData', 'inlineData'):
                if isinstance(part.get(key), dict):
                    value = part[key]
                    parts.append({**base, 'id': base['id'] + key, 'type': 'artifact',
                                  'data': {'mimeType': value.get('mimeType', ''), 'uri': value.get('fileUri', ''),
                                           'inline': key == 'inlineData'}})
    # 'data' is our normalized public payload, not provider base64 binary data.
    return snapshot('generateContent', parts, {
        **{key: data[key] for key in ('responseId', 'modelVersion', 'createTime', 'promptFeedback', 'usageMetadata', 'error') if key in data},
        'candidates': [{key: c[key] for key in ('index', 'finishReason', 'finishMessage', 'safetyRatings',
                        'citationMetadata', 'groundingMetadata', 'urlContextMetadata', 'avgLogprobs', 'logprobsResult') if key in c}
                       for c in candidates],
    }, secrets=secrets)


def interaction_snapshot(data, *, secrets=()):
    parts = []
    steps = data.get('steps') or data.get('outputs') or []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        kind = step.get('type', 'unknown')
        base = {'id': f's{index}', 'step': index, 'providerType': kind}
        if kind == 'thought':
            summary = step.get('summary') or []
            if isinstance(summary, str):
                summary = [{'type': 'text', 'text': summary}]
            text = '\n\n'.join(p['text'] for p in summary if isinstance(p, dict) and isinstance(p.get('text'), str))
            parts.append({**base, 'type': 'reasoning', 'text': text})
        elif kind == 'model_output' or kind == 'text':
            blocks = step.get('content', []) if kind == 'model_output' else [step]
            for pi, part in enumerate(blocks):
                if isinstance(part, dict) and part.get('type') == 'text':
                    parts.append({**base, 'id': f's{index}p{pi}', 'type': 'text', 'text': part.get('text', '')})
                elif isinstance(part, dict):
                    parts.append({**base, 'id': f's{index}p{pi}', 'type': 'artifact', 'data': part})
        elif kind.endswith(('_call', '_result')):
            parts.append({**base, 'type': 'tool-call' if kind.endswith('_call') else 'tool-result',
                          'name': str(step.get('name') or step.get('tool_name') or kind.rsplit('_', 1)[0])[:240],
                          'callId': str(step.get('call_id') or step.get('id') or '')[:256],
                          'data': {k: v for k, v in step.items() if k != 'type'}})
        else:
            parts.append({**base, 'type': 'artifact', 'data': step})
    return snapshot('interactions', parts, {k: data[k] for k in
                    ('id', 'model', 'agent', 'status', 'created', 'updated', 'usage', 'error', 'incomplete_details') if k in data}, secrets=secrets)


def _secret_values(secrets=()):
    values = list(secrets)
    for name, value in os.environ.items():
        if any(word in name.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
            values.extend(value.splitlines())
    values = sorted({s for s in values if len(s) >= 4}, key=len, reverse=True)
    values += [base64.b64encode(f'x-oauth-basic:{s}'.encode()).decode() for s in values]
    return values


def _dedupe_annotations(items):
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get('url') or '').strip()
        title = str(item.get('title') or '').strip()
        if not url:
            continue
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        result.append({'type': 'url_citation', 'title': title or url, 'url': url})
    return result


def _try_parse_structured_output(text: str, *, secrets=()):
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith('```'):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == '```':
            stripped = '\n'.join(lines[1:-1]).strip()
    if not stripped.startswith(('{', '[')):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    safe = sanitize(parsed, secrets=secrets)
    if len(json.dumps(safe, ensure_ascii=False).encode('utf-8')) > 256 * 1024:
        return None
    return safe


def extract_structured_payload(protocol, data, *, secrets=(), reply_text=''):
    """Extract RikkaHub-aligned UI parts, tool calls, code executions, annotations, and structured output."""
    secret_list = _secret_values(secrets)
    ui_parts = []
    tool_calls = []
    code_executions = []
    annotations = []
    thinking_chunks = []
    grounding = {}
    seq = 0

    def next_seq():
        nonlocal seq
        seq += 1
        return seq

    def add_code_execution(call_id, language, code, outcome, output, stdout, stderr, exit_code, status):
        code_executions.append(sanitize({
            'callId': call_id or '',
            'language': language or 'PYTHON',
            'code': code or '',
            'outcome': outcome or '',
            'output': output or '',
            'stdout': stdout or '',
            'stderr': stderr or '',
            'exitCode': exit_code,
            'status': status,
        }, secrets=secret_list))

    if protocol == 'generateContent':
        candidates = data.get('candidates') or []
        open_server_tools = {}
        open_client_tools = {}
        text_chunks = []

        for ci, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            gm = candidate.get('groundingMetadata')
            if isinstance(gm, dict):
                grounding['groundingMetadata'] = sanitize(gm, secrets=secret_list)
                for chunk in gm.get('groundingChunks') or []:
                    web = (chunk or {}).get('web') if isinstance(chunk, dict) else None
                    if isinstance(web, dict) and web.get('uri'):
                        annotations.append({'title': web.get('title', ''), 'url': web.get('uri', '')})
            ucm = candidate.get('urlContextMetadata')
            if isinstance(ucm, dict):
                grounding['urlContextMetadata'] = sanitize(ucm, secrets=secret_list)
                for entry in ucm.get('urlMetadata') or []:
                    if isinstance(entry, dict) and entry.get('retrievedUrl'):
                        annotations.append({'title': entry.get('retrievedUrl', ''), 'url': entry.get('retrievedUrl', '')})
            cm = candidate.get('citationMetadata')
            if isinstance(cm, dict):
                grounding['citationMetadata'] = sanitize(cm, secrets=secret_list)
                for src in cm.get('citationSources') or []:
                    if isinstance(src, dict) and src.get('uri'):
                        annotations.append({'title': src.get('title') or src.get('uri', ''), 'url': src.get('uri', '')})

            for pi, part in enumerate((candidate.get('content') or {}).get('parts') or []):
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get('text'), str):
                    if part.get('thought'):
                        thinking_chunks.append(part['text'])
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'reasoning',
                            'reasoning': part['text'],
                            'reasoningType': 'summary_text',
                            'providerType': 'thought',
                        }, secrets=secret_list))
                    else:
                        text_chunks.append(part['text'])
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'text',
                            'text': part['text'],
                        }, secrets=secret_list))
                elif isinstance(part.get('executableCode'), dict):
                    ec = part['executableCode']
                    cid = str(ec.get('id') or f'c{ci}p{pi}:code_execution')[:256]
                    entry = {
                        'sequence': next_seq(),
                        'type': 'server_tool',
                        'toolCallId': cid,
                        'toolName': 'code_execution',
                        'status': 'in_progress',
                        'input': {'language': ec.get('language', 'PYTHON'), 'code': ec.get('code', '')},
                        'output': None,
                        'providerType': 'executableCode',
                    }
                    ui_parts.append(sanitize(entry, secrets=secret_list))
                    open_server_tools[('code_execution', ec.get('id') or '')] = len(ui_parts) - 1
                elif isinstance(part.get('codeExecutionResult'), dict):
                    cer = part['codeExecutionResult']
                    raw_id = cer.get('id') or ''
                    outcome = str(cer.get('outcome') or '')
                    is_err = outcome not in ('', 'OUTCOME_OK')
                    status = 'failed' if is_err else 'completed'
                    out_payload = {'outcome': outcome, 'output': cer.get('output', '')}
                    idx = open_server_tools.pop(('code_execution', raw_id), None)
                    if idx is None and not raw_id:
                        for k in list(open_server_tools):
                            if k[0] == 'code_execution':
                                idx = open_server_tools.pop(k)
                                break
                    if idx is not None:
                        ui_parts[idx]['output'] = sanitize(out_payload, secrets=secret_list)
                        ui_parts[idx]['status'] = status
                        inp = ui_parts[idx].get('input') or {}
                        add_code_execution(ui_parts[idx]['toolCallId'], inp.get('language'), inp.get('code'),
                                           outcome, cer.get('output', ''), '', '', None, status)
                    else:
                        cid = str(raw_id or f'c{ci}p{pi}:code_execution')[:256]
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'server_tool',
                            'toolCallId': cid,
                            'toolName': 'code_execution',
                            'status': status,
                            'input': None,
                            'output': out_payload,
                            'providerType': 'codeExecutionResult',
                        }, secrets=secret_list))
                        add_code_execution(cid, '', '', outcome, cer.get('output', ''), '', '', None, status)
                elif isinstance(part.get('toolCall'), dict):
                    tc = part['toolCall']
                    tname = str(tc.get('toolType') or tc.get('name') or 'server_tool')[:120]
                    cid = str(tc.get('id') or f'c{ci}p{pi}:{tname}')[:256]
                    ui_parts.append(sanitize({
                        'sequence': next_seq(),
                        'type': 'server_tool',
                        'toolCallId': cid,
                        'toolName': tname,
                        'status': 'in_progress',
                        'input': tc.get('args'),
                        'output': None,
                        'providerType': 'toolCall',
                    }, secrets=secret_list))
                    open_server_tools[(tname, tc.get('id') or '')] = len(ui_parts) - 1
                elif isinstance(part.get('toolResponse'), dict):
                    tr = part['toolResponse']
                    tname = str(tr.get('toolType') or tr.get('name') or 'server_tool')[:120]
                    raw_id = tr.get('id') or ''
                    idx = open_server_tools.pop((tname, raw_id), None)
                    if idx is not None:
                        ui_parts[idx]['output'] = sanitize(tr.get('response'), secrets=secret_list)
                        ui_parts[idx]['status'] = 'completed'
                    else:
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'server_tool',
                            'toolCallId': str(raw_id or f'c{ci}p{pi}:{tname}')[:256],
                            'toolName': tname,
                            'status': 'completed',
                            'input': None,
                            'output': tr.get('response'),
                            'providerType': 'toolResponse',
                        }, secrets=secret_list))
                elif isinstance(part.get('functionCall'), dict):
                    fc = part['functionCall']
                    fname = str(fc.get('name') or 'function')[:120]
                    cid = str(fc.get('id') or f'c{ci}p{pi}:{fname}')[:256]
                    ui_parts.append(sanitize({
                        'sequence': next_seq(),
                        'type': 'tool',
                        'toolCallId': cid,
                        'toolName': fname,
                        'input': json.dumps(fc.get('args') or {}, ensure_ascii=False),
                        'output': [],
                        'approvalState': 'auto',
                        'providerType': 'functionCall',
                    }, secrets=secret_list))
                    open_client_tools[(fname, fc.get('id') or '')] = len(ui_parts) - 1
                elif isinstance(part.get('functionResponse'), dict):
                    fr = part['functionResponse']
                    fname = str(fr.get('name') or 'function')[:120]
                    raw_id = fr.get('id') or ''
                    idx = open_client_tools.pop((fname, raw_id), None)
                    resp_text = json.dumps(fr.get('response') or {}, ensure_ascii=False)
                    if idx is not None:
                        ui_parts[idx]['output'] = [sanitize({'type': 'text', 'text': resp_text}, secrets=secret_list)]
                    else:
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'tool',
                            'toolCallId': str(raw_id or f'c{ci}p{pi}:{fname}')[:256],
                            'toolName': fname,
                            'input': '{}',
                            'output': [{'type': 'text', 'text': resp_text}],
                            'approvalState': 'auto',
                            'providerType': 'functionResponse',
                        }, secrets=secret_list))
                elif isinstance(part.get('inlineData'), dict) or isinstance(part.get('fileData'), dict):
                    key = 'inlineData' if isinstance(part.get('inlineData'), dict) else 'fileData'
                    val = part[key]
                    mime = str(val.get('mimeType') or '')
                    ptype = 'image' if mime.startswith('image/') else 'document'
                    ui_parts.append(sanitize({
                        'sequence': next_seq(),
                        'type': ptype,
                        'mimeType': mime,
                        'uri': val.get('fileUri', ''),
                        'inline': key == 'inlineData',
                    }, secrets=secret_list))

        effective_reply = reply_text or '\n'.join(text_chunks)
    else:
        steps = data.get('steps') or data.get('outputs') or []
        open_server_tools = {}
        open_client_tools = {}
        text_chunks = []

        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            kind = str(step.get('type') or 'unknown')
            if kind == 'thought':
                summary = step.get('summary') or []
                if isinstance(summary, dict):
                    summary = [summary]
                if isinstance(summary, str):
                    text = summary
                else:
                    text = '\n'.join(p.get('text', '') for p in summary if isinstance(p, dict) and isinstance(p.get('text'), str))
                if text:
                    thinking_chunks.append(text)
                ui_parts.append(sanitize({
                    'sequence': next_seq(),
                    'type': 'reasoning',
                    'reasoning': text,
                    'reasoningType': 'summary_text',
                    'providerType': 'thought',
                }, secrets=secret_list))
            elif kind in ('model_output', 'text'):
                blocks = step.get('content', []) if kind == 'model_output' else [step]
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    for ann in block.get('annotations') or []:
                        if isinstance(ann, dict) and ann.get('url'):
                            annotations.append({'title': ann.get('title', ''), 'url': ann.get('url', '')})
                    if block.get('type') == 'text' and isinstance(block.get('text'), str):
                        text_chunks.append(block['text'])
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'text',
                            'text': block['text'],
                        }, secrets=secret_list))
                    elif block.get('type') == 'image':
                        ui_parts.append(sanitize({
                            'sequence': next_seq(),
                            'type': 'image',
                            'mimeType': block.get('mime_type') or block.get('mimeType') or 'image/png',
                            'uri': block.get('uri') or '',
                            'inline': bool(block.get('data')),
                        }, secrets=secret_list))
            elif kind == 'function_call':
                fname = str(step.get('name') or 'function')[:120]
                cid = str(step.get('id') or step.get('call_id') or f'step-{index}:{fname}')[:256]
                args = step.get('arguments')
                args_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
                ui_parts.append(sanitize({
                    'sequence': next_seq(),
                    'type': 'tool',
                    'toolCallId': cid,
                    'toolName': fname,
                    'input': args_str,
                    'output': [],
                    'approvalState': 'auto',
                    'providerType': kind,
                }, secrets=secret_list))
                open_client_tools[cid] = len(ui_parts) - 1
            elif kind == 'function_result':
                cid = str(step.get('call_id') or step.get('id') or '')[:256]
                idx = open_client_tools.pop(cid, None)
                out_val = step.get('result') if 'result' in step else step.get('output')
                out_str = out_val if isinstance(out_val, str) else json.dumps(out_val or {}, ensure_ascii=False)
                if idx is not None:
                    ui_parts[idx]['output'] = [sanitize({'type': 'text', 'text': out_str}, secrets=secret_list)]
                else:
                    ui_parts.append(sanitize({
                        'sequence': next_seq(),
                        'type': 'tool',
                        'toolCallId': cid or f'step-{index}:function',
                        'toolName': str(step.get('name') or 'function')[:120],
                        'input': '{}',
                        'output': [{'type': 'text', 'text': out_str}],
                        'approvalState': 'auto',
                        'providerType': kind,
                    }, secrets=secret_list))
            elif kind.endswith('_call'):
                tname = str(step.get('server_name') or step.get('name') or step.get('tool_name') or kind[:-5])[:120]
                cid = str(step.get('id') or step.get('call_id') or f'step-{index}:{tname}')[:256]
                inp = {k: v for k, v in step.items() if k not in ('type', 'id', 'call_id', 'signature')}
                ui_parts.append(sanitize({
                    'sequence': next_seq(),
                    'type': 'server_tool',
                    'toolCallId': cid,
                    'toolName': tname,
                    'status': 'in_progress',
                    'input': inp,
                    'output': None,
                    'providerType': kind,
                }, secrets=secret_list))
                open_server_tools[(kind[:-5], cid)] = len(ui_parts) - 1
            elif kind.endswith('_result'):
                base_kind = kind[:-7]
                cid = str(step.get('call_id') or step.get('id') or '')[:256]
                is_err = bool(step.get('is_error')) or (isinstance(step.get('exit_code'), int) and step['exit_code'] != 0)
                status = 'failed' if is_err else 'completed'
                out_payload = {k: v for k, v in step.items() if k not in ('type', 'id', 'call_id', 'signature')}
                idx = open_server_tools.pop((base_kind, cid), None)
                if idx is None:
                    for k in list(open_server_tools):
                        if k[0] == base_kind:
                            idx = open_server_tools.pop(k)
                            break
                if idx is not None:
                    ui_parts[idx]['output'] = sanitize(out_payload, secrets=secret_list)
                    ui_parts[idx]['status'] = status
                    if base_kind == 'code_execution':
                        inp = ui_parts[idx].get('input') or {}
                        add_code_execution(
                            ui_parts[idx]['toolCallId'],
                            inp.get('language') or 'PYTHON',
                            inp.get('code') or inp.get('command') or (inp.get('arguments') or {}).get('code', '') if isinstance(inp.get('arguments'), dict) else inp.get('code') or '',
                            out_payload.get('outcome') or '',
                            out_payload.get('output') or out_payload.get('result') or '',
                            out_payload.get('stdout') or '',
                            out_payload.get('stderr') or '',
                            out_payload.get('exit_code'),
                            status,
                        )
                else:
                    tname = str(step.get('server_name') or step.get('name') or step.get('tool_name') or base_kind)[:120]
                    ui_parts.append(sanitize({
                        'sequence': next_seq(),
                        'type': 'server_tool',
                        'toolCallId': cid or f'step-{index}:{tname}',
                        'toolName': tname,
                        'status': status,
                        'input': None,
                        'output': out_payload,
                        'providerType': kind,
                    }, secrets=secret_list))
                    if base_kind == 'code_execution':
                        add_code_execution(
                            cid or f'step-{index}:code_execution', '', '',
                            out_payload.get('outcome') or '',
                            out_payload.get('output') or out_payload.get('result') or '',
                            out_payload.get('stdout') or '',
                            out_payload.get('stderr') or '',
                            out_payload.get('exit_code'),
                            status,
                        )

        effective_reply = reply_text or data.get('output_text') or '\n\n'.join(text_chunks)

    for p in ui_parts:
        if p['type'] in ('server_tool', 'tool'):
            tool_calls.append({
                'callId': p['toolCallId'],
                'toolName': p['toolName'],
                'kind': 'server' if p['type'] == 'server_tool' else 'client',
                'status': p.get('status', 'completed' if p.get('output') else 'in_progress'),
                'input': p.get('input'),
                'output': p.get('output'),
                'providerType': p.get('providerType', ''),
            })

    return {
        'protocol': protocol,
        'parts': ui_parts,
        'thinkingSummary': '\n\n'.join(t for t in thinking_chunks if t),
        'toolCalls': tool_calls,
        'codeExecutions': code_executions,
        'annotations': _dedupe_annotations(annotations),
        'grounding': grounding or None,
        'structuredOutput': _try_parse_structured_output(effective_reply, secrets=secret_list),
    }

