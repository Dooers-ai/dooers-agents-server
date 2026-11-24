"""Guard: the legacy product name must not appear in tracked files."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TERM = "mae" + "stro"

ALLOWLIST_GLOBS = [
    "docs/decisoes/**",
    "docs/plans/**",
    "docs/tasks/**",
    "docs/contexto/**",
    "CHANGELOG.md",
]


def _allowed(path: str) -> bool:
    return any(fnmatch.fnmatch(path, glob) for glob in ALLOWLIST_GLOBS)


def _is_binary(data: bytes) -> bool:
    return b"\0" in data


def test_no_legacy_dooers_threads_name() -> None:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    violations: list[str] = []
    for rel in filter(None, listing.split("\0")):
        if _allowed(rel):
            continue
        if TERM in rel.lower():
            violations.append(f"{rel} (path)")
            continue
        file_path = REPO_ROOT / rel
        if not file_path.is_file():
            continue
        data = file_path.read_bytes()
        if _is_binary(data):
            continue
        text = data.decode("utf-8", errors="replace").lower()
        if TERM in text:
            violations.append(f"{rel} (content)")
    assert violations == [], "legacy name found in:\n" + "\n".join(violations[:20])
