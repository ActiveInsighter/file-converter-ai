"""Resolve task repositories and check actual Git write access using only GETs."""
from __future__ import annotations

import base64
import re

import httpx

REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
PROJECT_TASK = re.compile(r'github|\bgit\b|\bgh\b|repo(?:sitory)?|codebase|项目|仓库|代码库|分支|部署|推送|重构', re.I)
WRITE_TASK = re.compile(r'推送|重新部署|部署下|帮我部署|提交|重构|修复|修改|实现|创建分支|\b(?:push|redeploy|refactor|implement|commit)\b', re.I)


def task_text(job, message):
    return '\n'.join(str(v or '') for v in (job.get('prompt'), message.get('inputText')))


def repository_candidates(text, repositories):
    """Prefer explicit owner/name, then exact normalized name, then distinct suffix."""
    normalized = re.sub(r'[^a-z0-9]', '', text.casefold())
    words = set(re.findall(r'[a-z0-9]+', text.casefold()))
    ranked = []
    for repo in repositories:
        name = repo['full_name']
        short = name.split('/')[1]
        if re.search(r'(?<![A-Za-z0-9_.-])' + re.escape(name) + r'(?![A-Za-z0-9_.-])', text, re.I):
            score = 1000 + len(name)
        elif len(re.sub(r'[^a-z0-9]', '', short.casefold())) >= 4 and re.sub(r'[^a-z0-9]', '', short.casefold()) in normalized:
            score = 100 + len(short)
        else:
            suffix = re.split(r'[-_.]', short.casefold())[-1]
            score = 10 + len(suffix) if len(suffix) >= 4 and suffix in words else 0
            if score and re.search(re.escape(suffix) + r'\s*(?:项目|仓库|project\b|repository\b)', text, re.I):
                score += 50
        if score:
            ranked.append((score, repo))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]['full_name']))
    if not ranked:
        return []
    top = ranked[0][0]
    return [r for score, r in ranked if score == top]


async def github_request(client, token, path, *, params=None):
    response = await client.get('https://api.github.com' + path, params=params,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'AnyWorkflow-Agent'}, timeout=20, follow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError(f'GitHub 读取检查失败（HTTP {response.status_code}），请检查 Agent GitHub 密钥及仓库访问权限。')
    if len(response.content) > 6 * 1024 * 1024:
        raise RuntimeError('GitHub 读取响应过大；请在任务中指定 owner/repository。')
    return response


async def git_write_access(client, token, repo):
    """Receive-pack discovery authenticates a push, but never uploads or changes a ref."""
    if not REPO.fullmatch(repo):
        raise RuntimeError('Invalid GitHub repository')
    basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    response = await client.get(f'https://github.com/{repo}.git/info/refs', params={'service': 'git-receive-pack'},
        headers={'Authorization': f'Basic {basic}', 'User-Agent': 'git/2.45.0'},
        timeout=20, follow_redirects=False)
    if response.status_code in (401, 403, 404):
        return False
    if response.status_code != 200:
        raise RuntimeError(f'GitHub 写权限检查暂时失败（HTTP {response.status_code}）；未执行任何写入。')
    if 'application/x-git-receive-pack-advertisement' not in response.headers.get('content-type', ''):
        raise RuntimeError('GitHub 未返回 Git 写权限检查结果；未执行任何写入。')
    return True


async def prepare_github_context(client, token, text, *, require_write=None, trace=None):
    if not PROJECT_TASK.search(text) and not re.search(r'(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', text):
        return None
    if not token:
        raise RuntimeError('当前项目任务未配置 Agent GitHub 密钥；请配置 ANTIGRAVITY_GITHUB_TOKEN 后再执行。')
    identity = (await github_request(client, token, '/user')).json()
    login = identity.get('login', '')
    if not re.fullmatch(r'[A-Za-z0-9-]+', login):
        raise RuntimeError('GitHub 身份检查未返回有效账号。')
    # An explicit repository needs no inventory or global search.
    explicit = re.findall(r'(?:github\.com/|(?<![A-Za-z0-9_.-]))([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])', text)
    repositories = []
    for name in dict.fromkeys(explicit):
        name = name.removesuffix('.git')
        if not REPO.fullmatch(name):
            continue
        data = (await github_request(client, token, f'/repos/{name}')).json()
        repositories.append({'full_name': data['full_name'], 'default_branch': data.get('default_branch', 'main')})
    if not repositories:
        for page in range(1, 6):
            response = await github_request(client, token, '/user/repos', params={
                'per_page': 100, 'page': page, 'sort': 'full_name',
                'affiliation': 'owner,collaborator,organization_member'})
            for repo in response.json():
                if REPO.fullmatch(repo.get('full_name', '')):
                    repositories.append({'full_name': repo['full_name'], 'default_branch': repo.get('default_branch', 'main')})
            if 'rel="next"' not in response.headers.get('link', ''):
                break
    candidates = repository_candidates(text, repositories)
    selected = candidates[0] if len(candidates) == 1 else None
    needs_write = bool(WRITE_TASK.search(text)) if require_write is None else require_write
    context = {'login': login, 'candidates': candidates[:10], 'selected': selected,
               'requires_write': needs_write, 'write_verified': None,
               'repository_count': len(repositories)}
    if selected and needs_write:
        context['write_verified'] = await git_write_access(client, token, selected['full_name'])
    if trace:
        trace.emit('github_preflight', **context)
    if needs_write and len(candidates) > 1:
        raise RuntimeError('目标仓库有歧义，请明确 owner/repository；候选：' + ', '.join(r['full_name'] for r in candidates[:5]))
    if selected and needs_write and context['write_verified'] is False:
        raise RuntimeError(f'Agent GitHub 密钥无法写入 {selected["full_name"]}（Git receive-pack 拒绝）。'
                           '请为该密钥授予目标仓库 Contents: Read and write；本次未启动远程 Agent。')
    return context
