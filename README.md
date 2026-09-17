# forge

A local agentic coding CLI, backed by a local [Ollama](https://ollama.com)
server. Reads a repo, plans, edits files, runs shell commands, iterates —
the same shape as Claude Code, running entirely against a local model
(tested against `qwen2.5-coder`).

## Install

```bash
pip install -e ".[dev]"
```

## Usage

One-shot:

```bash
forge "add a docstring to forge/config.py"
```

Interactive REPL:

```bash
forge -i
```

REPL commands: `/help`, `/clear`, `/model <name>`, `/pull <name>`, `/usage`, `/plan`, `/build`, `/exit` / `/quit`.

Pass `--plan` to either mode to start read-only: only `read_file`/`list_dir`
are available, and the model is told to describe a plan instead of acting.
`edit_file`/`write_file`/`run_shell` are refused even if the model calls
them anyway. Use `/build` in the REPL (or drop `--plan`) to get full tool
access back.

## Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_MODEL` | `qwen2.5-coder` | Ollama model to use |
| `FORGE_HOST` | `http://localhost:11434` | Ollama server URL |
| `FORGE_MAX_ITERATIONS` | `25` | Max tool-call rounds per task |
| `FORGE_SHELL_TIMEOUT` | `60` | Seconds before a shell command times out |
| `FORGE_WORKING_DIR` | `.` | Sandbox root for file/shell tools |
| `FORGE_USAGE_FILE` | `~/.forge/usage.json` | Where the cross-session running-total token count is stored |
| `FORGE_PROMPT_PRICE_PER_1M` | `0.30` | $/1M prompt tokens used to estimate savings in `/usage` |
| `FORGE_COMPLETION_PRICE_PER_1M` | `0.80` | $/1M completion tokens used to estimate savings in `/usage` |

`/usage`'s default $/1M rates are Together AI's published pricing for
hosting Qwen2.5-Coder — an open-model hosting price, not a frontier-model
(GPT-4o/Claude/etc.) price, since that's the realistic alternative to
running it locally. Override via the env vars above if you're comparing
against a different model or provider.

## Design notes

- No system-prompt persona: `forge.llm.new_history()` returns `[]`. The
  model only sees tool schemas and the task.
- All file/shell tools are sandboxed to the working directory via
  `forge.tools._safe_path`, which resolves symlinks and `..` before
  checking containment.
- `forge.tools.call_tool` never raises — every tool failure, expected or
  not, comes back as an `"Error: ..."` string the model can see and react
  to.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```
