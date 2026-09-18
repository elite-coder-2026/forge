"""An in-memory journal of file changes forge made, so `/undo` can revert
them without touching git.

`write_file` and `edit_file` record each change (what the file held before
and what forge left there). `undo()` walks the journal newest-first and
restores the previous contents, or deletes a file forge created.

Limits, on purpose:
- The journal lives for the process. Nothing survives a restart, and a
  one-shot `forge "task"` run has nothing to undo afterwards (use git).
- Only `write_file`/`edit_file` are tracked. Files changed by `run_shell`
  commands are not.
- A file changed by someone else after forge wrote it is never overwritten
  unless the user forces it.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

MAX_ENTRIES = 100


@dataclass
class Entry:
    path: str  # absolute
    before: bytes | None  # None: forge created the file
    after: bytes | None  # what forge left on disk
    scope: str = ""  # realpath of the project directory that made the change


_journal: list[Entry] = []
_lock = threading.Lock()


def record(path: str, before: bytes | None, after: bytes | None, scope: str = "") -> None:
    with _lock:
        _journal.append(Entry(path, before, after, scope))
        del _journal[:-MAX_ENTRIES]


def clear() -> None:
    with _lock:
        _journal.clear()


def count() -> int:
    with _lock:
        return len(_journal)


def _scope_of(base_dir: str) -> str:
    return os.path.realpath(base_dir)


def _matches(entry: Entry, scope: str) -> bool:
    """Entries recorded without a scope belong to every directory."""
    return not entry.scope or entry.scope == scope


def _current(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _shown(base_dir: str, path: str) -> str:
    base = os.path.realpath(base_dir)
    try:
        if os.path.commonpath([base, path]) == base:
            return os.path.relpath(path, base)
    except ValueError:
        pass
    return path


def history(base_dir: str, limit: int = 10) -> list[str]:
    """Most recent changes first, one line each."""
    scope = _scope_of(base_dir)
    with _lock:
        recent = [e for e in reversed(_journal) if _matches(e, scope)][:limit]
    lines = []
    for entry in recent:
        action = "created" if entry.before is None else "modified"
        lines.append(f"{action} {_shown(base_dir, entry.path)}")
    return lines


def undo(base_dir: str, n: int = 1, force: bool = False) -> list[str]:
    """Revert up to `n` of the most recent changes; returns a line per
    result. Stops (leaving the entry in place) at a file that has changed
    since forge wrote it, unless `force`.
    """
    results: list[str] = []
    scope = _scope_of(base_dir)
    for _ in range(n):
        with _lock:
            entry = next((e for e in reversed(_journal) if _matches(e, scope)), None)
        if entry is None:
            if not results:
                results.append("Nothing to undo.")
            break

        shown = _shown(base_dir, entry.path)
        if not force and _current(entry.path) != entry.after:
            results.append(
                f"Stopped: {shown} was changed after forge wrote it. "
                f"/undo force overwrites it anyway."
            )
            break

        try:
            if entry.before is None:
                if os.path.exists(entry.path):
                    os.remove(entry.path)
                results.append(f"Removed {shown} (forge created it)")
            else:
                with open(entry.path, "wb") as f:
                    f.write(entry.before)
                results.append(f"Restored {shown}")
        except OSError as e:
            results.append(f"Error: could not undo {shown}: {e}")
            break

        with _lock:
            # Drop exactly the entry we reverted (by identity: equal-looking
            # entries from earlier changes must stay).
            for i in range(len(_journal) - 1, -1, -1):
                if _journal[i] is entry:
                    del _journal[i]
                    break
    return results
