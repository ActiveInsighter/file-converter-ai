"""Add the quota route to the existing gateway config without replacing it."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


config = Path("/etc/caddy/n8n.caddy")
source = config.read_text(encoding="utf-8")
import_line = "        import /etc/caddy/file-converter-quota.caddy\n"
anchor = "    route {\n        handle_path /codex-runner/* {"
if import_line in source:
    print("quota route already imported")
else:
    if source.count(anchor) != 1:
        raise SystemExit("Expected exactly one gateway route anchor; no changes made")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = config.with_name(f"n8n.caddy.backup-{timestamp}")
    backup.write_text(source, encoding="utf-8")
    os.chmod(backup, 0o600)
    candidate = source.replace(anchor, "    route {\n" + import_line +
                               "        handle_path /codex-runner/* {", 1)
    temporary = config.with_name("n8n.caddy.quota-new")
    temporary.write_text(candidate, encoding="utf-8")
    os.chmod(temporary, config.stat().st_mode & 0o777)
    temporary.replace(config)
    print(f"quota route imported; backup: {backup}")
