# Task: Full audit and hardening pass on `forge`

## What this is

`forge` is a local agentic coding CLI — same shape as Claude Code itself:
reads a repo, plans, edits files, runs shell commands, iterates. It's a
Python package backed by a local Ollama server (tested against
Qwen2.5-Coder). Core loop: `forge/llm.py` runs a ReAct-style tool-calling
loop against `forge/tools.py`'s file/shell operations, driven by
`forge/main.py`'s CLI/REPL.

Repo root: wherever you unzip/place the delivered `forge/` project
(contains `pyproject.toml`, `forge/`, `tests/`, `README.md`).

## Why you're being asked to do this

The project was built interactively, one feature/bug-fix at a time, over
a single long session. Five real bugs were found and fixed along the way:

1. `argparse` with `nargs="?"` crashed on any single-word, dash-prefixed
   task (`forge -fix`) — fixed by switching to `parse_known_args` and
   treating all unmatched tokens as the task, verbatim.
2. `forge/__init__.py` did `from .main import main`, which shadowed the
   `forge.main` submodule with the function of the same name — anything
   doing `import forge.main` got the function back instead of the module
   and crashed with a confusing `AttributeError`. Fixed by removing that
   import; the installed CLI entry point never needed it.
3. `/clear` in the REPL only cleared the terminal screen, not the actual
   conversation history passed to the model — fixed by adding real
   multi-turn session state (`llm.new_history()` / `run_task(...,
   history=...)`) and making `/clear` reset it.
4. `json.loads()` on tool-call arguments could raise `JSONDecodeError`
   uncaught when a small local model returned malformed/truncated JSON,
   crashing the whole agent loop — fixed with `_parse_tool_arguments()`
   in `llm.py`, which now feeds a parse error back to the model as a
   tool result so it can retry, instead of crashing.
5. `call_tool()` in `tools.py` only caught `ToolError` and `TypeError` —
   any other exception (permission denied, disk full, broken symlink,
   etc.) still propagated uncaught and killed the loop. **This fix was
   written but not yet verified or merged when work stopped.**

That pattern — bugs surfacing reactively, one at a time, mid-conversation
— is exactly what this task is meant to replace. Do **one** thorough,
proactive pass now, with tests proving each fix, rather than incremental
patches.

## What to do

1. **Read the whole codebase first.** `forge/config.py`, `forge/tools.py`,
   `forge/llm.py`, `forge/main.py`, and the existing `tests/` directory
   (currently ~69 passing pytest tests — run them first to see current
   state: `pip install -e ".[dev]" && pytest tests/ -v`).

2. **Finish and verify bug #5 above.** `call_tool()` needs a catch-all
   `except Exception` (after the existing `ToolError`/`TypeError`
   handlers) that converts any unexpected failure into an `"Error: ..."`
   string instead of letting it propagate. Write a test that actually
   triggers an unhandled-exception-class failure (e.g. a permission
   error on an unreadable file, or an `OSError` writing to a full/
   read-only filesystem via mocking) and prove `call_tool` returns a
   string instead of raising.

3. **Audit for the same class of bug everywhere else.** The underlying
   pattern in bugs #4 and #5 is: *any code path that can raise, in a
   function whose result gets fed back into the agent loop, must be
   caught and converted into a result the model can see* — not just the
   two spots already found. Specifically check:
   - `run_shell()` — `subprocess.run` can raise more than just
     `TimeoutExpired` (e.g. `OSError` if the shell itself can't be
     spawned, `ValueError` on embedded null bytes).
   - `write_file()` / `edit_file()` — `open(..., "w")` and `os.makedirs()`
     can raise `PermissionError`, `IsADirectoryError`, `OSError` (disk
     full), etc.
   - `list_dir()` — `os.walk()` can raise `PermissionError` mid-walk on
     a directory it can't read.
   - `llm.py`'s `run_task()` — the `client.chat(...)` call itself: what
     happens if Ollama is unreachable mid-task (connection drop after
     the first successful call), returns a malformed response missing
     expected keys (`response["message"]` — is `"message"` guaranteed to
     exist?), or the model returns a `tool_calls` entry missing the
     `"function"` key or `"name"` field entirely?
   - `main.py`'s `handle_slash_command()` — any command that takes a
     user-supplied argument (`/model <name>`) that could be empty,
     malformed, or otherwise break something downstream.

4. **Don't stop at exception-handling.** While you're in each file, also
   check for:
   - Logic bugs (off-by-one, wrong variable used, incorrect boolean
     conditions) — not just crashes.
   - Concurrency/state issues — `_usage` in `llm.py` is module-level
     global state; is that safe for how it's actually used, or could it
     leak/corrupt across a long REPL session in some edge case?
   - Anything in the sandboxing logic (`_safe_path()` in `tools.py`)
     that could be bypassed — symlinks pointing outside the project dir,
     `..` sequences that `os.path.realpath` might not catch in some
     edge case, case-insensitive filesystem quirks, etc. This is a
     security-relevant function; treat it with extra scrutiny.
   - Resource leaks (files opened without context managers anywhere,
     subprocess handles not cleaned up on timeout).

5. **For every bug found and fixed: write a test that fails before the
   fix and passes after**, following the existing test style in
   `tests/test_tools.py`, `tests/test_llm.py`, `tests/test_main.py`
   (pytest, `unittest.mock` for the Ollama client, `tmp_path`/
   `monkeypatch` fixtures for filesystem tests). Don't just fix and
   move on — prove it.

6. **Run the full suite at the end** and report a clean pass. Don't
   leave anything half-verified.

## What NOT to do

- Don't add new features (streaming output, git integration, LSP, plan/
  build mode split) — this pass is exclusively about correctness and
  robustness of what already exists.
- Don't change the tool's behavior/scope beyond what's needed to fix a
  real bug — e.g. don't add back a system-prompt persona; that was
  deliberately removed (`llm.new_history()` returns `[]` — no system
  message, no "you are forge" framing, just tool schemas + task).
- Don't rewrite working code for style reasons alone. Fix what's
  actually broken or fragile; leave the rest.

## Deliverable

A summary of every bug found (including whether it was already known
per the list above or newly discovered), the fix for each, the test
that proves it, and a final `pytest tests/ -v` run showing everything
passing.
