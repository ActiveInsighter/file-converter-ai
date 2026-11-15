"""GitHub helper mounted in the sandbox. Authentication comes from network transforms.

One workflow/ref is dispatched once per workspace. An uncertain POST is reconciled
by GET, never retried. The journal is written before sending the POST.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def api(path, method='GET', body=None):
    request = Request('https://api.github.com' + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'User-Agent': 'AnyWorkflow-Agent'})
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RuntimeError('GitHub response too large; narrow the query')
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as error:
        raise RuntimeError(f'GitHub HTTP {error.code}; request not automatically repeated') from None


def checked_repo(repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise RuntimeError('Use an explicit owner/repository')
    return repo


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def normalize_inputs(inputs):
    if not isinstance(inputs, dict) or len(inputs) > 25:
        raise RuntimeError('Workflow inputs must be a JSON object of at most 25 values')
    result = {}
    for key, value in inputs.items():
        if not isinstance(value, (str, bool, int, float)) or value is None:
            raise RuntimeError('Workflow input values must be strings, numbers or booleans')
        result[str(key)] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def deploy(repo, workflow, ref, inputs, *, request=api, directory=Path('/workspace/.anyworkflow/deployments'),
           sleep=time.sleep):
    repo = checked_repo(repo)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', workflow) or not ref or len(ref) > 255:
        raise RuntimeError('Invalid workflow or ref')
    inputs = normalize_inputs(inputs)
    base = f'/repos/{repo}/actions'
    workflow_id = request(f'{base}/workflows/{quote(workflow, safe="")}')['id']
    identity = hashlib.sha256(json.dumps([repo.casefold(), str(workflow_id), ref]).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (identity + '.json')
    with path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reused = path.exists()
        if reused:
            state = json.loads(path.read_text())
            if state['inputs'] != inputs:
                raise RuntimeError('This task already dispatched this workflow/ref with different inputs. '
                                   'Inspect the saved run; do not dispatch again.')
        else:
            sha = request(f'/repos/{repo}/commits/{quote(ref, safe="")}')['sha']
            before = request(f'{base}/workflows/{workflow_id}/runs?' + urlencode({
                'event': 'workflow_dispatch', 'branch': ref, 'per_page': 30}))['workflow_runs']
            state = {'repo': repo, 'workflow_id': workflow_id, 'ref': ref, 'inputs': inputs,
                     'sha': sha, 'before': [r['id'] for r in before],
                     'created': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'), 'phase': 'intent'}
            save(path, state)
            # Any interruption from here leaves an intent record; later calls only GET.
            try:
                response = request(f'{base}/workflows/{workflow_id}/dispatches', 'POST', {'ref': ref, 'inputs': inputs})
                run_id = response.get('workflow_run_id') or response.get('id')
                if run_id:
                    state['run_id'] = run_id
                state['phase'] = 'accepted'
                save(path, state)
            except Exception:
                state['phase'] = 'uncertain'
                save(path, state)
                raise
        if not state.get('run_id'):
            for attempt in range(12):
                rows = request(f'{base}/workflows/{workflow_id}/runs?' + urlencode({
                    'event': 'workflow_dispatch', 'branch': ref, 'created': '>=' + state['created'],
                    'per_page': 30}))['workflow_runs']
                matches = [r for r in rows if r['id'] not in state['before'] and r.get('head_sha') == state['sha']
                           and r.get('event') == 'workflow_dispatch' and r.get('head_branch') == ref]
                if len(matches) > 1:
                    raise RuntimeError('Multiple matching runs found; inspect them by ID. No second dispatch sent.')
                if matches:
                    state['run_id'] = matches[0]['id']
                    state['phase'] = 'bound'
                    save(path, state)
                    break
                if attempt < 11:
                    sleep(2)
        if not state.get('run_id'):
            raise RuntimeError('Dispatch identity is not yet visible. Retry this helper to reconcile; do not dispatch again.')
    run = request(f'{base}/runs/{state["run_id"]}')
    return {k: run.get(k) for k in ('id', 'html_url', 'status', 'conclusion', 'head_sha')} | {'reused': reused}


def watch(repo, run_id, seconds, *, request=api, sleep=time.sleep, clock=time.monotonic):
    repo = checked_repo(repo)
    if not str(run_id).isdigit() or not 0 <= seconds <= 900:
        raise RuntimeError('Invalid run ID or wait window')
    deadline = clock() + seconds
    while True:
        run = request(f'/repos/{repo}/actions/runs/{run_id}')
        result = {k: run.get(k) for k in ('id', 'html_url', 'status', 'conclusion', 'head_sha')}
        print(json.dumps(result), flush=True)
        if run.get('status') == 'completed' or clock() >= deadline:
            return result
        sleep(min(10, max(0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('identity')
    sub.add_parser('repos')
    workflows = sub.add_parser('workflows')
    workflows.add_argument('--repo', required=True)
    dispatch = sub.add_parser('deploy')
    dispatch.add_argument('--repo', required=True)
    dispatch.add_argument('--workflow', required=True)
    dispatch.add_argument('--ref', required=True)
    dispatch.add_argument('--inputs-json', default='{}')
    dispatch.add_argument('--wait-seconds', type=int, default=300)
    status = sub.add_parser('watch')
    status.add_argument('--repo', required=True)
    status.add_argument('--run-id', required=True)
    status.add_argument('--wait-seconds', type=int, default=300)
    args = parser.parse_args()
    if args.command == 'identity':
        print(json.dumps({'login': api('/user')['login']}))
    elif args.command == 'repos':
        # Compact inventory only; never a global repository search or giant JSON dump.
        for page in range(1, 6):
            rows = api('/user/repos?' + urlencode({'per_page': 100, 'page': page, 'sort': 'full_name'}))
            for row in rows:
                print(row['full_name'])
            if len(rows) < 100:
                break
    elif args.command == 'workflows':
        for workflow in api(f'/repos/{checked_repo(args.repo)}/actions/workflows')['workflows']:
            print(json.dumps({k: workflow[k] for k in ('id', 'name', 'path', 'state')}))
    else:
        if not 0 <= args.wait_seconds <= 900:
            parser.error('--wait-seconds must be 0..900')
        if args.command == 'deploy':
            run = deploy(args.repo, args.workflow, args.ref, json.loads(args.inputs_json))
            print(json.dumps(run), flush=True)
            run_id = run['id']
        else:
            run_id = args.run_id
        result = watch(args.repo, run_id, args.wait_seconds)
        if result['status'] == 'completed' and result['conclusion'] != 'success':
            raise SystemExit(2)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(json.dumps({'error': str(error)[:500], 'redispatched': False}), flush=True)
        raise SystemExit(2) from None
