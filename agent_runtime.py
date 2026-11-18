"""Small, versioned workspace tools and execution instructions for repository tasks."""
import json
from pathlib import Path

HELPER = '/workspace/.anyworkflow/github.py'

GUIDE = f'''GitHub HTTPS Git and REST authentication is configured by the environment.
Never request, print or store credentials. Access is determined by the configured token.
Use the repositories named in the request. If a name is abbreviated or unclear,
`python3 {HELPER}` has `identity` and `repos` commands returning a compact inventory.
Ask for the full repository name if the inventory still leaves the target ambiguous.
Clone only the repositories relevant to the request.
Read each repository's AGENTS.md and relevant project skills. Use `cd /workspace/<repo>` explicitly
in shell commands. After one filesystem search timeout, switch to targeted rg/rg --files
instead of repeating find_by_name/list_dir over the same tree. Check a component's
actual export/path before importing it. Use project scripts to install Chromium when
missing. Preserve nonzero test exit codes; do not hide failures behind `|| tail`.
For UI changes, compile first, repair the exact failing browser assertion, then verify
desktop/mobile. Never rerun an unchanged failing test or call a failed check passed.
When a push is requested, push a feature branch and verify its remote SHA.
If push returns 403, stop and report the permission failure, retaining the commit.
For a deployment-only task, inspect workflow configuration through GitHub REST,
not a repository clone, npm install, or local build. The workflow performs the build.
List available workflows with `python3 {HELPER} workflows --repo OWNER/REPO`.
For workflow deployments use ONLY:
python3 {HELPER} deploy --repo OWNER/REPO --workflow FILE.yml --ref BRANCH --inputs-json '{{}}'
The helper normalizes inputs, journals BEFORE POST, binds the exact workflow run ID,
and reuses that run on every repeated invocation. HTTP 204 means accepted. To wait
again use its watch command with --repo and --run-id. A completed/success run means
the deployment succeeded; stop. Never redispatch because another workflow appears
above it in a repository-wide list, or because boolean input types differ. Failed or
uncertain dispatches also must be reconciled, never blindly sent again.
Only report delivery when the requested branch exists, required checks passed and,
if requested, the preview deployment succeeded. State any missing delivery clearly.'''


def mount_runtime(environment):
    root = Path(__file__).resolve().parent
    hooks = {'anyworkflow-deploy': {'pre_tool_execution': [{
        'matcher': 'code_execution', 'hooks': [{'type': 'command',
        'command': 'python3 /workspace/.anyworkflow/deploy_gate.py', 'timeout': 5}]}]}}
    environment.setdefault('sources', []).extend([
        {'type': 'inline', 'target': 'workspace/.anyworkflow/github.py',
         'content': (root / 'agent_workspace_github.py').read_text()},
        {'type': 'inline', 'target': 'workspace/.anyworkflow/deploy_gate.py',
         'content': (root / 'agent_deploy_gate.py').read_text()},
        {'type': 'inline', 'target': '.agents/hooks.json', 'content': json.dumps(hooks)},
    ])
