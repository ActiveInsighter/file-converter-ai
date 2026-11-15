"""Route direct workflow dispatch commands through the journalled deployment helper."""
import json
import re
import sys


def decision(payload):
    args = (payload.get('tool_call') or {}).get('args') or {}
    code = str(args.get('code', ''))
    if re.search(r'/actions/workflows/[^\s"\x27]+/dispatches|\bgh\s+workflow\s+run\b', code):
        return {'decision': 'deny', 'reason':
            'Use python3 /workspace/.anyworkflow/github.py deploy with --repo --workflow --ref --inputs-json. '
            'It records the first dispatch and reuses its exact run ID. A 204 is accepted, not failure; '
            'never create a second deployment because it is not visible immediately.'}
    return {'decision': 'allow'}


if __name__ == '__main__':
    print(json.dumps(decision(json.load(sys.stdin))))
