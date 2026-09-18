# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"      # install with pytest, pyright, mcp
pytest tests/ -v             # full suite
pytest tests/test_x.py::test_name -v   # single test
forge "task"                 # one-shot run (entry point: forge.main:main)
forge -i                     # interactive REPL
```

Optional extras: `.[lsp]` (pyright tools), `.[voice]` (faster-whisper). The MCP integration test is skipped if `mcp` is absent.

## Architecture

forge is a local agentic coding CLI (Claude Code-shaped) running against an Ollama server. Python >= 3.10, package `forge/`.

- **Tool loop**: the model calls tools for up to `max_iterations` rounds per task. Tools include `read_file`, `list_dir`, `edit_file`, `write_file`, `run_shell`, `run_tests`, LSP tools (`find_definition`, `find_references`, `hover`, `get_diagnostics`, via pyright), MCP tools (`mcp__<server>__<tool>`), and plugin tools.
- **`forge.tools`**: `_safe_path` sandboxes all file/shell tools to the working directory (resolves symlinks and `..` before checking containment). `call_tool` must never raise; every failure returns an `"Error: ..."` string for the model to see.
- **`forge.llm`**: `new_history()` deliberately returns `[]` — no system-prompt persona. The model sees only tool schemas and the task.
- **Plan mode** (`--plan`, `/plan`, `/build`): read-only; only read-only tools are offered, and mutating tools are refused even if the model calls them. MCP tools are allowed only with `readOnlyHint`; plugins only with `read_only=True`.
- **Config precedence**: CLI flags > env vars (`FORGE_*`) > `forge.toml` in the working dir > defaults. Unknown keys, wrong types, or invalid TOML are errors, not ignored.
- **Trust model**: project `forge.toml` MCP servers and `<project>/.forge/plugins/*.py` run arbitrary code, so they require approval (prompt or `--trust-mcp` / `--trust-plugins`), remembered per exact content and re-asked on change. User plugins in `~/.forge/plugins/` always load. Plugins register via `@tool` from `forge.plugins`; files starting with `_` are ignored and built-in names can't be reused.
- **Model routing**: with `FORGE_FAST_MODEL` set, a text heuristic (no extra model call) picks fast vs. main model per task; explicit `/model` or `--model` disables it. Images use a separate vision model whose description is prepended to the task, since vision models can't call tools.
- **Sessions/state**: REPL supports multiple project sessions (own history, mode, images, `/undo`), sharing the Ollama server, usage totals (`~/.forge/usage.json`) and budget. `/undo` tracks only `write_file`/`edit_file`. Budget alerts warn, never block.
- **Optional surfaces**: read-only web dashboard (`--web`, localhost only, GET only, strict CSP, data rendered as text), voice input (`--voice`, always confirms transcript before running).

README.md documents every env var, `forge.toml` key, and REPL command in detail.
