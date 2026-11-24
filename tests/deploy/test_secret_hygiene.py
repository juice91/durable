"""Grep-based secret hygiene over deploy/ and .dockerignore."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
SK_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{10,}")
B64_32_RE = re.compile(r"(?<![A-Za-z0-9+/_=])[A-Za-z0-9+/_-]{43}=(?![A-Za-z0-9+/_=])")
PASSWORD_RE = re.compile(r"(?i)(?:password|passwd)\s*[:=]\s*(\S+)")
OK_VALUES = {"", '""', "''", "{}", "[]", "-", "null", "~"}


def _deploy_files() -> list[Path]:
    files = [p for p in DEPLOY_DIR.rglob("*") if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES]
    files.append(DOCKERIGNORE)
    return files


def test_no_key_like_values_in_deploy_tree():
    hits: list[str] = []
    for path in _deploy_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(REPO_ROOT)
        for i, line in enumerate(text.splitlines(), 1):
            if SK_RE.search(line) or B64_32_RE.search(line):
                hits.append(f"{rel}:{i}")
            for match in PASSWORD_RE.finditer(line):
                value = match.group(1).strip().strip(",").strip('"').strip("'")
                if value in OK_VALUES or value.startswith(("$", "{", "op:" "//")):
                    continue
                hits.append(f"{rel}:{i}")
    assert hits == [], f"key-like values in deploy files: {hits}"


def test_dockerignore_excludes_env_dev_node_modules_and_git():
    lines = {line.strip() for line in DOCKERIGNORE.read_text().splitlines() if line.strip() and not line.startswith("#")}
    assert any(line in {".env", ".env.*", ".env*"} or line.startswith(".env") for line in lines)
    assert any(line in {".dev/", ".dev", "**/.dev/"} for line in lines)
    assert any("node_modules" in line for line in lines)
    assert any(line in {".git", ".git/"} for line in lines)
