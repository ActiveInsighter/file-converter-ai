"""Audit 15 manually selected formula details from HKUST PDF pages 31-35.

These are spot checks of transcription fidelity, not an overall OCR accuracy
estimate. Equivalent LaTeX spellings are normalized; review failures against
source images. See docs/page-batching-2026-11-13.md for scope and observations.

Usage: python benchmarks/score_page_batches.py work/batch-benchmark
"""
import hashlib
from datetime import datetime
import json
from pathlib import Path
import re
import sys


def normalize(text):
    text = re.sub(r'\\(?:left|right|quad|qquad)(?![A-Za-z])|\\[,;!]', '', text)
    text = text.replace(r'\tfrac', r'\frac').replace(r'\dfrac', r'\frac')
    text = text.replace(r'\overline{a_j}', r'\bar{a}_j')
    return re.sub(r'\s+', '', text).replace('$', '')


def remark_derivative(text):
    # Inspect this particular repetition, not a correct copy elsewhere on p34.
    section = text.split('**Remark**', 1)[-1].split('**Proof.', 1)[0]
    return r"\frac{f'(z)}{f(z)}" in section


CHECKS = [
    ('p31-inner-kernel-sign', 31, '内圈几何级数前的负号', lambda t: r'=-\sum_{k=-\infty}^{-1}' in t),
    ('p31-annulus-upper-bound', 31, 'A3 定义中的 |z-1|<1 上界', lambda t: r'A_3=\{z:0<|z-1|<1\}' in t),
    ('p31-cosecant-coefficient', 31, '1/sin(z) 展开系数 7z^3/360', lambda t: r'\frac{7z^3}{360}' in t),
    ('p32-essential-series', 32, 'sin(1/z) 展开中的三阶、五阶阶乘', lambda t: r'-\frac{1}{3!}\frac{1}{z^3}+\frac{1}{5!}\frac{1}{z^5}' in t or r'-\frac{1}{3!z^3}+\frac{1}{5!z^5}' in t),
    ('p32-pole-order', 32, 'k+1 阶极限等于 0', lambda t: r'\lim_{z\toz_0}(z-z_0)^{k+1}f(z)=0' in t),
    ('p32-factorization', 32, 'g 的 k 阶因子和 h(z0) 非零', lambda t: r'g(z)=(z-z_0)^kh(z)' in t and r'h(z_0)\neq0' in t),
    ('p33-residue-definition', 33, '留数定义为 a_{-1}', lambda t: r'isa_{-1}' in t),
    ('p33-winding-weight', 33, '留数定理中的绕数权重', lambda t: r'\sum_{j=1}^{n}n(\Gamma,z_j)' in t),
    ('p33-principal-part', 33, '主部求和从 -m_j 到 -1', lambda t: r'\sum_{k=-m_j}^{-1}a_{k,j}' in t),
    ('p34-cauchy-derivatives', 34, '高阶 Cauchy 公式中的 n! 和绕数分母', lambda t: r'\frac{n!}{2\piin(\Gamma,a)}' in t and r'(w-a)^{n+1}' in t),
    ('p34-logarithmic-derivative', 34, 'Remark 段积分的 f\'(z)/f(z)', remark_derivative),
    ('p34-imaginary-polynomial', 34, 'f(iy) 的负号、五次幂与系数 17', lambda t: r'-y^2-1+17y^5i' in t),
    ('p35-rouche-strict', 35, 'Rouche 不等式保留严格小于', lambda t: r'|f(z)+g(z)|<|f(z)|+|g(z)|' in t),
    ('p35-radius-example', 35, '半径例题的 49<64、96=48|z| 和 r=2', lambda t: all(x in t for x in ['49<64', '96=48|z|', 'r=2'])),
    ('p35-blaschke-conjugate', 35, 'Blaschke 乘积的共轭分母', lambda t: r'\frac{z-a_j}{1-\bar{a}_jz}' in t),
]


def summarize(root, name, size, first, last):
    directory = root / f'{name}-{size}'
    pages = {n: (directory / 'pages' / f'{n:03d}.md').read_text() for n in range(first, last + 1)}
    log = (directory / 'run.log').read_text()
    perf = json.loads(re.search(r'\[performance\] (\{[^\n]+\})', log)[1])
    times = {}
    for line in log.splitlines():
        for marker in ['config', 'quota']:
            if f'[{marker}]' in line and (marker != 'quota' or 'action complete' in line):
                stamp = re.search(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z', line)
                if stamp:
                    times[marker] = datetime.fromisoformat(stamp[0].replace('Z', '+00:00'))
    total_requests = int(re.search(r'\[quota\] action complete[^\n]* requests=(\d+)', log)[1])
    stage = re.search(r'\[ocr\] finished (\{[^\n]+\})', log)
    checks = []
    if name == 'formulas':
        for key, page, label, check in CHECKS:
            checks.append({'id': key, 'page': page, 'label': label, 'pass': check(normalize(pages[page]))})
    done = re.findall(r'\[done\] (\d+)\.md \(([^\n]+)\)', log)
    return {
        'conversion_wall_seconds': round((times['quota'] - times['config']).total_seconds(), 2),
        'pages_per_request': size, 'page_files': sorted(pages),
        'logical_batches': (last - first + size) // size,
        'actual_requests_including_hedges': total_requests,
        'hedge_events': len(re.findall(r'\[hedge\]', log)),
        'model_p50_seconds': perf['p50_seconds'], 'model_p95_seconds': perf['p95_seconds'],
        'http_429': perf['429'], 'http_503': perf['503'],
        'ocr': json.loads(stage[1]) if stage else None,
        'source_counts': {source: sum(source in row[1] for row in done) for source in ['ocr+model']},
        'spot_checks_passed': sum(c['pass'] for c in checks),
        'spot_checks_total': len(checks), 'spot_checks': checks,
        'sha256': {f'{n:03d}.md': hashlib.sha256(text.encode()).hexdigest() for n, text in pages.items()},
    }


if __name__ == '__main__':
    root = Path(sys.argv[1])
    result = {name: [summarize(root, name, size, first, last) for size in [1, 3, 5]]
              for name, first, last in [('intro', 1, 5), ('formulas', 31, 35)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
