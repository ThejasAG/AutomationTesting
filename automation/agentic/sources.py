"""Read-only access to the app source checkouts and the platform's knowledge files.

Shared by the triage and generator agents. Every path is confined to the app's
checkout (no '..', no absolute paths) — the agents read source, never write it.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPOS = os.path.join(ROOT, "repos")
KNOWLEDGE = os.path.join(ROOT, "automation", "knowledge")

# The checkout is identified by its git remote, not by a uuid that changes when a
# project is re-created.
_REMOTE_MARK = {"consumer": "vya-consumer", "business": "vya-business"}


@lru_cache(maxsize=4)
def app_repo(app: str) -> Optional[str]:
    mark = _REMOTE_MARK.get(app)
    if not mark or not os.path.isdir(REPOS):
        return None
    for d in sorted(os.listdir(REPOS)):
        path = os.path.join(REPOS, d)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue
        try:
            url = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            continue
        if mark in url and os.path.isdir(os.path.join(path, "App")):
            return path
    return None


def _repo_or_raise(app: str) -> str:
    repo = app_repo(app)
    if not repo:
        raise ValueError(f"no checkout of the {app} app under repos/")
    return repo


def search(app: str, pattern: str, max_lines: int = 80) -> str:
    repo = _repo_or_raise(app)
    out = subprocess.run(
        ["grep", "-rnIE", "--include=*.js", "--include=*.jsx", "--include=*.ts",
         "--include=*.tsx", "-m", "5", pattern, "App"],
        cwd=repo, capture_output=True, text=True, timeout=60).stdout
    lines = [ln[:300] for ln in out.splitlines()]
    if not lines:
        return f"No matches for /{pattern}/ in the {app} app."
    more = f"\n… {len(lines) - max_lines} more lines" if len(lines) > max_lines else ""
    return "\n".join(lines[:max_lines]) + more


def read_file(app: str, path: str, start: int = 1, end: int = 200) -> str:
    repo = _repo_or_raise(app)
    rel = os.path.normpath(path).lstrip("/")
    if rel.startswith(".."):
        raise ValueError("path must stay inside the app checkout")
    full = os.path.join(repo, rel)
    if not os.path.isfile(full):
        raise ValueError(f"{rel} does not exist in the {app} app")
    start = max(1, int(start or 1))
    end = min(max(start, int(end or start + 199)), start + 399)
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    body = "".join(f"{i}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
    return f"{rel} (lines {start}-{min(end, len(lines))} of {len(lines)})\n{body}"


def recent_changes(app: str, days: int = 14) -> str:
    repo = _repo_or_raise(app)
    out = subprocess.run(
        ["git", "log", f"--since={int(days)}.days", "-n", "20", "--stat", "--format=%h %ad %s",
         "--date=short"], cwd=repo, capture_output=True, text=True, timeout=30).stdout
    return out[:12000] or f"No commits in the last {days} days on the checked-out branch."


def knowledge(name: str) -> str:
    path = os.path.join(KNOWLEDGE, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def apps_available() -> Dict[str, bool]:
    return {a: bool(app_repo(a)) for a in _REMOTE_MARK}
