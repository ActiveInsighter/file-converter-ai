#!/usr/bin/env python3
"""Print the GEMINI_KEY_GROUPS value that matches a GEMINI_API_KEYS pool.

Gemini free-tier quota is counted per Google Cloud Project, so the quota pool
must know which Project every API key belongs to. This script resolves each key
to its Project by enumerating the account with gcloud and prints one Project ID
per line, in the same order as the supplied keys.

Store the output as the repository variable GEMINI_KEY_GROUPS:

    GEMINI_API_KEYS="$(cat keys.txt)" python3 deploy/print-key-groups.py > groups.txt
    gh variable set GEMINI_KEY_GROUPS -R ActiveInsighter/file-converter-ai < groups.txt

Requires an authenticated gcloud with access to every Project in the pool.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def gcloud(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["gcloud", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gcloud {' '.join(args)} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def parse_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\r\n,;]+", raw):
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def keys_of(project: str) -> list[tuple[str, str]]:
    """Return (key_string, project) for every API key of one Project."""
    names = [
        line.strip()
        for line in gcloud(
            "services", "api-keys", "list", f"--project={project}",
            "--format=value(name)",
        ).splitlines()
        if line.strip()
    ]
    resolved: list[tuple[str, str]] = []
    for name in names:
        key_string = gcloud(
            "services", "api-keys", "get-key-string", name,
            "--format=value(keyString)",
        ).strip()
        if key_string:
            resolved.append((key_string, project))
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keys-file",
        help="File with one API key per line. Defaults to the GEMINI_API_KEYS environment variable.",
    )
    parser.add_argument(
        "--project-filter",
        default="",
        help="Only scan Projects whose ID contains this substring (saves gcloud calls).",
    )
    args = parser.parse_args()

    if args.keys_file:
        with open(args.keys_file, encoding="utf-8") as handle:
            pool = parse_keys(handle.read())
    else:
        pool = parse_keys(os.getenv("GEMINI_API_KEYS"))
    if not pool:
        print("No API keys supplied (use --keys-file or GEMINI_API_KEYS).", file=sys.stderr)
        return 2

    projects = [
        line.strip()
        for line in gcloud("projects", "list", "--format=value(projectId)").splitlines()
        if line.strip()
    ]
    if args.project_filter:
        projects = [item for item in projects if args.project_filter in item]
    print(f"# scanning {len(projects)} Projects for {len(pool)} keys", file=sys.stderr)

    key_to_project: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool_executor:
        for resolved in pool_executor.map(keys_of, projects):
            key_to_project.update(dict(resolved))

    groups: list[str] = []
    unresolved: list[int] = []
    for index, key in enumerate(pool, start=1):
        project = key_to_project.get(key)
        if project is None:
            unresolved.append(index)
            groups.append(f"unresolved-key-{index}")
        else:
            groups.append(project)

    duplicates = {item for item in groups if groups.count(item) > 1}
    if unresolved:
        print(
            f"# WARNING: {len(unresolved)} key(s) could not be resolved: "
            f"{unresolved[:20]}",
            file=sys.stderr,
        )
    if duplicates:
        print(
            "# WARNING: these Projects are used by more than one key in the pool, "
            "so they share a single quota budget:",
            file=sys.stderr,
        )
        for item in sorted(duplicates):
            print(f"#   {item} x{groups.count(item)}", file=sys.stderr)
    print(
        f"# {len(pool)} keys -> {len(set(groups))} distinct Projects",
        file=sys.stderr,
    )

    print(",".join(groups))
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
