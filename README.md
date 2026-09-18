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
| `FORGE_VISION_MODEL` | `gemma3:12b` | Vision model that describes attached images (`ollama pull` it first) |
| `FORGE_FAST_MODEL` | *(unset)* | Small, quick model for simple tasks; enables automatic model selection |
| `FORGE_BUDGET_TOKENS` | `200000` | Soft session token limit; warns when crossed (`0` disables) |
| `FORGE_BUDGET_MINUTES` | `15` | Soft limit on minutes of model compute per session (`0` disables) |
| `FORGE_MAX_ITERATIONS` | `25` | Max tool-call rounds per task |
| `FORGE_SHELL_TIMEOUT` | `60` | Seconds before a shell command times out |
| `FORGE_WORKING_DIR` | `.` | Sandbox root for file/shell tools |
| `FORGE_USAGE_FILE` | `~/.forge/usage.json` | Where the cross-session running-total token count is stored |
| `FORGE_PROMPT_PRICE_PER_1M` | `0.30` | $/1M prompt tokens used to estimate savings in `/usage` |
| `FORGE_COMPLETION_PRICE_PER_1M` | `0.80` | $/1M completion tokens used to estimate savings in `/usage` |

### Language-server tools (LSP)

For Python files, forge gives the model four semantic tools backed by a
language server (pyright): `find_definition`, `find_references`, `hover`
(type + docstring) and `get_diagnostics` (type-check a file). Positions
are 1-based `line`/`column`. They're read-only, so they work in plan mode.

Install the server with `pip install "forge[lsp]"` (or `pip install
pyright`). forge also finds it in the same virtualenv as forge itself.
Set `FORGE_LSP_COMMAND` to use a different server command. Without a
server, the tools return an error telling the model how to install it.

### Running tests

The model has a dedicated `run_tests` tool, separate from `run_shell`. It
runs your suite (pytest if the project's Python has it, otherwise
unittest; a project `.venv`/`venv` interpreter is used when present) and
returns a summary instead of hundreds of lines:

```
pytest: FAILED: 1 failed, 12 passed in 0.4s (exit code 1)
Failures:
  FAILED tests/test_a.py::test_x - assert 1 == 2
```

It takes an optional `path` (file, directory, or `file::test` id), an
optional custom `command` (e.g. `npm test`; unrecognized output shows the
exit code and the tail), and `verbose` for the full output. Not available
in plan mode, since it runs code.

### MCP servers

forge can use tools from any [Model Context Protocol](https://modelcontextprotocol.io)
server (filesystem, GitHub, databases, ...) over stdio. Declare them in
`forge.toml`:

```toml
[mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

[mcp_servers.github]
command = "github-mcp-server"
timeout = 120            # seconds per request (default 60)
[mcp_servers.github.env]
GITHUB_TOKEN = "${GITHUB_TOKEN}"   # ${VAR} is filled in from your shell
```

- Server tools reach the model as `mcp__<server>__<tool>`, alongside the
  built-in ones. `/mcp` lists what's connected.
- **Approval:** a `forge.toml` names programs to run, so a repo can't start
  them on its own. The first time, forge shows the commands and asks; pass
  `--trust-mcp` to approve without a prompt (needed for non-interactive
  runs). Approval is remembered for exactly those definitions and asked
  again if they change.
- **Plan mode** only offers tools the server marks `readOnlyHint`.
- One server failing to start is reported and skipped; the rest still work.
  Large results are truncated at 20,000 characters, and image/audio
  results are noted but not passed to the model.
- Only the stdio transport is supported (no HTTP/SSE servers yet).

### Budget alerts

Local models cost no money per token, but a runaway session burns real
minutes. forge warns (never blocks) when a session crosses a token or
model-compute-time limit, checked after every model call so a long task is
flagged mid-run, not after it finishes:

```
[budget] This session has used 212,340 tokens (soft limit 200,000). Long history is resent every step; /clear starts fresh.
```

Defaults are 200,000 tokens and 15 minutes; set `budget_tokens` /
`budget_minutes` in `forge.toml`, or the `FORGE_BUDGET_*` variables (`0`
turns a limit off). Each limit warns when first crossed and again at each
further multiple (2x, 3x...), not on every step. In the REPL, `/budget`
shows usage against the limits and `/budget tokens 50000`, `/budget
minutes 5`, or `/budget tokens off` change them. "Compute time" is time
spent waiting on models (including the vision model), not time you spend
typing. Notices go to stderr.

### Automatic model selection

Set a fast model (`FORGE_FAST_MODEL`, `fast_model` in `forge.toml`, or
`/fast <name>` in the REPL) and forge picks a model per task: quick edits
and questions go to the fast model, bigger work goes to your main model.
It's a text heuristic (length, words like "refactor" or "typo", how many
files are named, lists, tracebacks), so it costs no extra model call.

- Each pick is announced, e.g. `[auto] qwen2.5-coder:7b: quick task`.
- Borderline tasks and short replies ("yes, do it") stay on the model
  that's already loaded, since switching models makes Ollama reload
  weights. With no history, borderline means the main model.
- Image tasks and plan mode always use the main model.
- `/auto` shows the status, `/auto off` pins everything to the main
  model, and `/model <name>` or `--model` count as an explicit choice and
  turn routing off. Without a fast model, nothing changes.

### Voice input

Dictate a task instead of typing it: `/voice` in the REPL, or
`forge --voice` for a one-shot run. forge shows what it heard and asks
`Send this? [Y/n]` before running it, because speech-to-text mishears (it
may hear "add" as "at") and a wrong task can change files. A dictated task
always runs as a task, even if it starts with "slash".

**Setup (one time):**

```bash
pip install "forge[voice]"     # offline speech-to-text, nothing else to configure
```

The first `/voice` downloads the speech model (about 150 MB, with a
progress bar) *before* it starts listening; after that it's instant and
fully offline. Choose a different model with `FORGE_WHISPER_MODEL` (e.g.
`small.en` for accuracy, `tiny.en` for speed).

**Using it:** speak, then press **Ctrl+C** when you're done; forge keeps
what you said and transcribes it. (With sox installed it also stops by
itself after two seconds of silence.) On macOS your terminal needs
microphone permission (System Settings > Privacy & Security > Microphone).
The recording is a temporary file deleted right after transcription.

**Recorders:** auto-detected from `rec` (sox, `brew install sox`),
`arecord`, or `ffmpeg`. **Your own engine:** set `voice_transcribe` to any
command that prints the transcript, and forge uses it instead of the
built-in one:

```toml
voice_transcribe = "whisper-cli -m ~/models/ggml-base.en.bin -f {audio} -nt"
voice_record = "rec -q -r 16000 -c 1 {audio} silence 1 0.1 3% 1 2.0 3% trim 0 {seconds}"  # optional
voice_seconds = 30       # maximum recording length
```

`{audio}` is the WAV path and `{seconds}` the length limit (timestamps and
`[BLANK_AUDIO]` markers in the output are stripped). Env vars:
`FORGE_VOICE_TRANSCRIBE`, `FORGE_VOICE_RECORD`, `FORGE_VOICE_SECONDS`.

### Web dashboard

`forge -i --web` serves a read-only page at `http://localhost:8765`
(`--web-port N`; `0` picks a free port) so you can glance at usage and the
conversation without keeping the terminal focused. It shows session and
all-time tokens against your budget, model compute time, the estimated
money saved, each open session's conversation (tool calls included), and
the recent file changes that `/undo` could revert. It refreshes every two
seconds and stops when forge exits. It's most useful with `-i`; a one-shot
run ends before there's much to see.

It exposes your conversations, so it's locked down: it listens on
`127.0.0.1` only, answers `GET` only (nothing can be changed through it),
rejects requests whose `Host` isn't localhost on its port (blocking
DNS-rebinding from other web pages), sends a strict
Content-Security-Policy with no third-party assets, and renders all data as
text, never HTML. Long messages are truncated in the view.

### Browser chat

`forge -i --chat` serves a chat page at `http://localhost:8766`
(`--chat-port N`; `0` picks a free port). Type a task in the browser and
forge runs it in the active REPL session: same model, plan/build mode,
history and undo as the terminal. Enter sends, Shift+Enter adds a line.
It can be combined with `--web`.

Unlike the dashboard it accepts input, and a task can edit files or run
shell commands, so use `--plan` if you want it read-only. It's locked down:
`127.0.0.1` only, a `Host` check against DNS rebinding, a per-run secret
token required on every send, same-origin only, JSON only, a strict
Content-Security-Policy, and all text rendered as text, never HTML.

Limits: one browser task runs at a time, and there's no live streaming (the
reply appears when the task finishes). Don't run a task in the terminal and
the browser at the same moment, since they share one conversation.

### Plugins (custom tools)

Add your own tools without editing forge. Put a `.py` file in
`~/.forge/plugins/` (yours, always loaded) or `<project>/.forge/plugins/`
(shared with the repo, loaded after approval) and register tools with
`@tool`:

```python
import os
from forge.plugins import tool

@tool(
    description="Count the lines in a file.",
    parameters={"path": {"type": "string", "description": "File to count."}},
    read_only=True,          # also offer it in plan mode
)
def count_lines(base_dir, path):
    with open(os.path.join(base_dir, path)) as f:
        return f"{sum(1 for _ in f)} lines"
```

The function gets the project directory first, then the arguments the
model chose, and returns text (`required` defaults to all parameters;
`name=` overrides the function name). Errors come back to the model as
`Error: ...` instead of crashing forge, results are truncated at 20,000
characters, and a broken file is reported and skipped. `/plugins` lists
what's loaded.

Plugins are ordinary code running inside forge, with no sandbox or
timeout. Because of that, project plugins can't load unasked: forge lists
the files and asks once (or pass `--trust-plugins`), remembers that
approval for exactly those file contents, and asks again if any change.
Files starting with `_` are ignored, and a plugin can't reuse a built-in
tool's name.

### Multiple sessions

One REPL can work in several project directories without restarting:

```
/session new ../api-server     open it (its own forge.toml, history, plan mode)
/session new ~/web web         ...or give it a name
/session list                  * marks the active one
/session switch web
/session close api-server      its saved history is kept
```

Each session has its own conversation (saved and resumed per directory),
plan/build mode, attached images, and `/undo` history, which only reverts
changes made in that session's directory. The prompt shows the session name
once more than one is open (`[web] > `). All sessions share the Ollama
server, the usage totals and the budget. MCP servers and plugins come from
the directory forge was launched in. Sessions run one at a time (you switch
between them); they don't work in parallel in the background.

### Undo

`/undo` in the REPL reverts the last file change forge made: it restores
the previous contents, or deletes a file forge created. `/undo 3` reverts
the last three, and `/undo list` shows what can be undone. If a file was
changed after forge wrote it, `/undo` stops instead of overwriting your
edits; `/undo force` overrides that.

Limits: it tracks `write_file`/`edit_file` only (not changes made by shell
commands), and the history lasts for the session, so use git for anything
older or for one-shot runs.

### Images (screenshot → code)

Attach a screenshot or mockup and forge builds from it:

```
forge --image mockup.png "build this login page in React"
```

In the REPL, `/image <path>` attaches images to your next task (`/image`
lists them, `/image clear` drops them; quote paths with spaces).
PNG, JPEG, GIF and WebP up to 20 MB.

Vision models such as Gemma 3 can't call tools, and coding models can't
see images, so forge hands off in two steps: the vision model
(`FORGE_VISION_MODEL` / `vision_model`, default `gemma3:12b`) writes a
detailed description of the image, and that text is added to your task for
your normal coding model. Pull the vision model first with
`ollama pull gemma3:12b` (or `gemma3:4b` for less memory). Its token usage
counts toward `/usage`.

### Project config file

Put a `forge.toml` in the project directory (`FORGE_WORKING_DIR`, default
`.`) so settings travel with the project. Precedence, highest first: CLI
flags, environment variables, `forge.toml`, built-in defaults.

```toml
model = "qwen2.5-coder:14b"
host = "http://localhost:11434"
max_iterations = 40
shell_timeout = 120
```

Other keys: `usage_file`, `session_file` (relative paths are relative to
the project), `prompt_price_per_1m`, `completion_price_per_1m`. An unknown
key, wrong type, or invalid TOML is reported as an error instead of being
ignored.

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
