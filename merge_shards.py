from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


def page_number(path: Path) -> int:
    match = re.match(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 10**9


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards-dir", default="shards")
    parser.add_argument("--output-dir", default="final")
    parser.add_argument("--total-pages", type=int, required=True)
    args = parser.parse_args()

    shards_dir = Path(args.shards_dir)
    output_dir = Path(args.output_dir)
    pages_out = output_dir / "pages"
    pages_out.mkdir(parents=True, exist_ok=True)

    candidates = sorted(
        shards_dir.glob("shard-*/pages/*.md"),
        key=page_number,
    )

    by_page: dict[int, Path] = {}
    duplicates: dict[int, list[str]] = {}
    for path in candidates:
        number = page_number(path)
        if number in by_page:
            duplicates.setdefault(number, [str(by_page[number])]).append(str(path))
            continue
        by_page[number] = path

    expected = set(range(1, args.total_pages + 1))
    found = set(by_page)
    missing = sorted(expected - found)
    unexpected = sorted(found - expected)

    blocks: list[str] = []
    for number in sorted(found & expected):
        source = by_page[number]
        destination = pages_out / f"{number:0{max(3, len(str(args.total_pages)))}d}.md"
        shutil.copy2(source, destination)
        blocks.append(source.read_text(encoding="utf-8").strip())

    merged_name = "merged.md" if not missing and not duplicates else "merged.partial.md"
    (output_dir / merged_name).write_text(
        "\n\n".join(blocks).rstrip() + "\n",
        encoding="utf-8",
    )

    shard_manifests = []
    for manifest_path in sorted(shards_dir.glob("shard-*/manifest.json")):
        try:
            shard_manifests.append(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            shard_manifests.append(
                {
                    "manifest_path": str(manifest_path),
                    "manifest_error": f"{type(exc).__name__}: {exc}",
                }
            )

    summary = {
        "total_pages": args.total_pages,
        "found_pages": len(found & expected),
        "missing_pages": missing,
        "unexpected_pages": unexpected,
        "duplicates": duplicates,
        "shard_manifests": shard_manifests,
        "merged_file": merged_name,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"[merge] found={len(found & expected)}/{args.total_pages} "
        f"missing={len(missing)} duplicates={len(duplicates)} "
        f"output={merged_name}",
        flush=True,
    )
    if missing:
        print(f"[merge] missing_pages={missing}", flush=True)
    if duplicates:
        print(f"[merge] duplicate_pages={sorted(duplicates)}", flush=True)

    return 0 if not missing and not duplicates else 2


if __name__ == "__main__":
    raise SystemExit(main())
