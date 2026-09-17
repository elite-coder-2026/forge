# forge — Audit & Hardening Pass: Game Plan

## 1. Overview

`forge` is a local agentic coding CLI — same shape as Claude Code: reads a
repo, plans, edits files, runs shell commands, iterates. Python package,
backed by a local Ollama server (tested against Qwen2.5-Coder). Core loop:
`forge/llm.py` runs a ReAct-style tool-calling loop against `forge/tools.py`'s
file/shell operations, driven by `forge/main.py`'s CLI/REPL.

**Why this pass exists:** the project was built interactively, one
feature/bug-fix at a time, over a single long session. Bugs kept surfacing
reactively, mid-conversation. This task replaces that pattern with **one**
thorough, proactive pass — read everything first, fix the whole class of
bug, prove each fix with a test — rather than more incremental patching.

**Scope boundaries:**
- Correctness and robustness only.
- No new features (no streaming output, git integration, LSP, plan/build
  mode split).
- No behavior/scope changes beyond what a real bug fix requires — in
  particular, do **not** add back a system-prompt persona. `llm.new_history()`
  returns `[]` deliberately: no system message, no "you are forge" framing,
  just tool schemas + task.
- No style-only rewrites. Fix what's actually broken or fragile; leave the
  rest.

## 2. Prerequisites

1. Place/unzip the delivered `forge/` project into this directory (expects
   `pyproject.toml`, `forge/`, `tests/`, `README.md` at the root).
2. Establish the known-good baseline:
   ```
   pip install -e ".[dev]"
   pytest tests/ -v
   ```
   Currently ~69 passing tests. Confirm this before touching anything.

## 3. Known Bugs (carried over from prior session)

| # | Bug | Status |
|---|-----|--------|
| 1 | `argparse` with `nargs="?"` crashed on any single-word, dash-prefixed task (`forge -fix`). Fixed by switching to `parse_known_args` and treating all unmatched tokens as the task, verbatim. | Fixed |
| 2 | `forge/__init__.py` did `from .main import main`, shadowing the `forge.main` submodule with the function of the same name — `import forge.main` returned the function, not the module, crashing with a confusing `AttributeError`. Fixed by removing that import. | Fixed |
| 3 | `/clear` in the REPL only cleared the terminal screen, not the actual conversation history. Fixed by adding real multi-turn session state (`llm.new_history()` / `run_task(..., history=...)`) and making `/clear` reset it. | Fixed |
| 4 | `json.loads()` on tool-call arguments could raise `JSONDecodeError` uncaught when a small local model returned malformed/truncated JSON, crashing the whole agent loop. Fixed with `_parse_tool_arguments()` in `llm.py`, which feeds the parse error back to the model as a tool result so it can retry. | Fixed |
| 5 | `call_tool()` in `tools.py` only caught `ToolError` and `TypeError` — any other exception (permission denied, disk full, broken symlink, etc.) still propagated uncaught and killed the loop. | **Written but not yet verified or merged — finish this first.** |

## 4. Step 1 — Finish and Verify Bug #5

- `call_tool()` needs a catch-all `except Exception` (after the existing
  `ToolError`/`TypeError` handlers) that converts any unexpected failure into
  an `"Error: ..."` string instead of letting it propagate.
- Write a test that actually triggers an unhandled-exception-class failure
  (e.g. a permission error on an unreadable file, or an `OSError` writing to
  a full/read-only filesystem via mocking) and prove `call_tool` returns a
  string instead of raising.

## 5. Step 2 — Read the Whole Codebase

Read before changing anything: `forge/config.py`, `forge/tools.py`,
`forge/llm.py`, `forge/main.py`, and the existing `tests/` directory.

## 6. Step 3 — Audit Checklist (same bug class as #4 and #5)

Underlying pattern: **any code path that can raise, in a function whose
result gets fed back into the agent loop, must be caught and converted into
a result the model can see.** Specifically check:

- **`forge/tools.py`**
  - `run_shell()` — `subprocess.run` can raise more than `TimeoutExpired`
    (e.g. `OSError` if the shell can't be spawned, `ValueError` on embedded
    null bytes).
  - `write_file()` / `edit_file()` — `open(..., "w")` and `os.makedirs()`
    can raise `PermissionError`, `IsADirectoryError`, `OSError` (disk full).
  - `list_dir()` — `os.walk()` can raise `PermissionError` mid-walk.
  - `_safe_path()` — **security-sensitive, treat with extra scrutiny.**
    Check for symlinks pointing outside the project dir, `..` sequences
    `os.path.realpath` might not catch in some edge case, case-insensitive
    filesystem quirks.
- **`forge/llm.py`**
  - `run_task()` — what happens if Ollama is unreachable mid-task
    (connection drop after the first successful call), returns a malformed
    response missing expected keys (`response["message"]` — is `"message"`
    guaranteed to exist?), or the model returns a `tool_calls` entry missing
    the `"function"` key or `"name"` field entirely?
  - `_usage` — module-level global state; is it safe for how it's actually
    used, or could it leak/corrupt across a long REPL session in some edge
    case?
- **`forge/main.py`**
  - `handle_slash_command()` — any command that takes a user-supplied
    argument (e.g. `/model <name>`) that could be empty, malformed, or
    otherwise break something downstream.

## 7. Step 4 — Beyond Exception Handling

While in each file, also check for:
- Logic bugs (off-by-one, wrong variable used, incorrect boolean
  conditions) — not just crashes.
- Concurrency/state issues.
- Resource leaks (files opened without context managers anywhere,
  subprocess handles not cleaned up on timeout).

## 8. Test Discipline

For every bug found and fixed: **write a test that fails before the fix and
passes after**, following the existing style in `tests/test_tools.py`,
`tests/test_llm.py`, `tests/test_main.py` (pytest, `unittest.mock` for the
Ollama client, `tmp_path`/`monkeypatch` fixtures for filesystem tests).
Don't just fix and move on — prove it.

## 9. Final Verification

Run the full suite at the end and report a clean pass:
```
pytest tests/ -v
```
Don't leave anything half-verified.

## 10. Deliverable

A summary of every bug found (noting whether it was already known per the
list above or newly discovered), the fix for each, the test that proves it,
and a final `pytest tests/ -v` run showing everything passing.
