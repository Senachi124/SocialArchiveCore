#!/usr/bin/env python3
"""Check tracked publication files without reading any external runtime state."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "Telegram token": r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b",
    "cloud key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "credential URL": r"(?:https?|postgres(?:ql)?)://[^\s/:]+:[^\s/@]+@",
    "production hostname": r"(?i)(?:vm1\.|sena124\.|learnfromidol\.com)",
    "private host path": r"/(?:home/linuxuser|var/backups|opt/(?:xloader|social-archive)|etc/vm1)[/\s]",
    "literal session secret": r'''(?i)["']?(?:auth_token|sessionid|csrftoken)["']?\s*[:=]\s*["'][A-Za-z0-9_%.-]{32,}["']''',
}
DISALLOWED_PARTS = {".venv", "__pycache__", "node_modules", "state", "downloads", "media", ".test-control"}
ALLOWED_SUFFIXES = {".py", ".md", ".toml", ".json", ".yml", ".yaml", ".txt", ".sql", ".example"}


def main() -> int:
    entries = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "--stage", "-z"], text=True
    ).split("\0")
    entries = sorted(filter(None, entries))
    errors: list[str] = []
    if not entries:
        errors.append("No tracked publication files")
    for entry in entries:
        metadata, relative = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        path = ROOT / relative
        parts = PurePosixPath(relative).parts
        if DISALLOWED_PARTS.intersection(parts) or mode not in {"100644", "100755"} or stage != "0":
            errors.append(f"{relative}: forbidden runtime path or nonregular file")
            continue
        if path.name != ".gitignore" and path.suffix not in ALLOWED_SUFFIXES:
            errors.append(f"{relative}: unsupported publication file type")
        raw = subprocess.check_output(["git", "-C", str(ROOT), "cat-file", "blob", blob])
        if len(raw) > 2_000_000:
            errors.append(f"{relative}: unexpected large file")
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{relative}: binary data")
            continue
        # Exact synthetic URL-sanitization fixtures, not a general test exemption.
        if relative == "packages/instagram/tests/test_gallery_diagnostics.py":
            for fixture in (
                "https://" + "user:password@i.instagram.com/api/v1/highlights/123/highlights_tray/?token=secret",
                "https://" + "u:p@example.com/path?q=secret#secret",
            ):
                content = content.replace(fixture, "https://example.invalid/redacted-fixture")
        for label, pattern in PATTERNS.items():
            if re.search(pattern, content):
                # Only report the file and rule, never the matching value.
                errors.append(f"{relative}: {label}")
        if path.suffix == ".py":
            try:
                ast.parse(content, filename=relative)
            except SyntaxError:
                errors.append(f"{relative}: Python syntax error")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"PASS: {len(entries)} indexed text blobs; runtime boundaries and secret patterns checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
