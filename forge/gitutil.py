"""Git awareness: work out which files a task touched, summarize the diff,
and (on request) commit just those files.

Everything here is best-effort and never raises: forge must keep working
outside a git repo, without git installed, or when a git command fails.
forge never pushes.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field


@dataclass
class Snapshot:
    """Dirty-file state of a repo at one moment: path -> (status, digest)."""

    root: str
    state: dict[str, tuple[str, str]] = field(default_factory=dict)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _digest(root: str, path: str) -> str:
    try:
        with open(os.path.join(root, path), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return "missing"


def snapshot(cwd: str) -> Snapshot | None:
    """Capture the repo's dirty files, or None if `cwd` isn't in a git repo."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return None
    root = top.stdout.strip()

    status = _git(root, "status", "--porcelain", "-z", "-uall")
    if status is None or status.returncode != 0:
        return None

    state: dict[str, tuple[str, str]] = {}
    entries = status.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1  # renames/copies are followed by the original path
        state[path] = (xy, _digest(root, path))
    return Snapshot(root=root, state=state)


def changed_files(before: Snapshot, after: Snapshot) -> list[str]:
    """Files that are dirty now and weren't in that exact state before."""
    return sorted(p for p, v in after.state.items() if before.state.get(p) != v)


def diff_summary(after: Snapshot, files: list[str]) -> str:
    tracked = [p for p in files if after.state[p][0] != "??"]
    untracked = [p for p in files if after.state[p][0] == "??"]

    lines: list[str] = []
    if tracked:
        stat = _git(after.root, "diff", "--stat", "HEAD", "--", *tracked)
        if stat is not None and stat.returncode == 0 and stat.stdout.strip():
            lines.append(stat.stdout.rstrip())
        else:
            lines.extend(f" {p}" for p in tracked)
    lines.extend(f" {p} (new file)" for p in untracked)

    return f"Changed {len(files)} file(s):\n" + "\n".join(lines)


def suggest_message(task: str, limit: int = 72) -> str:
    first_line = task.strip().splitlines()[0] if task.strip() else "forge changes"
    message = " ".join(first_line.split())
    return message if len(message) <= limit else message[: limit - 3] + "..."


def commit(root: str, files: list[str], message: str) -> tuple[bool, str]:
    """Commit exactly `files` (other dirty files are left alone)."""
    added = _git(root, "add", "--", *files)
    if added is None or added.returncode != 0:
        return False, (added.stderr.strip() if added else "git is unavailable")

    done = _git(root, "commit", "-m", message, "--", *files)
    if done is None:
        return False, "git is unavailable"
    return done.returncode == 0, (done.stdout + done.stderr).strip()
