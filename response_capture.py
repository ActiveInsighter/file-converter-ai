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
                              ('executableCode', 'tool-call'), ('codeExecutionResult', 'tool-result')]:
                if isinstance(part.get(key), dict):
                    value = part[key]
                    parts.append({**base, 'id': base['id'] + key, 'type': kind,
                                  'name': str(value.get('name') or ('code_execution' if 'Code' in key or 'Execution' in key else key))[:240],
                                  'callId': str(value.get('id', ''))[:256], 'data': value})
            for key in ('fileData', 'inlineData'):
                if isinstance(part.get(key), dict):
                    value = part[key]
                    parts.append({**base, 'id': base['id'] + key, 'type': 'artifact',
                                  'data': {'mimeType': value.get('mimeType', ''), 'uri': value.get('fileUri', ''),
                                           'inline': key == 'inlineData'}})
    # 'data' is our normalized public payload, not provider base64 binary data.
    return snapshot('generateContent', parts, {
        **{key: data[key] for key in ('responseId', 'modelVersion', 'createTime', 'promptFeedback', 'usageMetadata') if key in data},
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
