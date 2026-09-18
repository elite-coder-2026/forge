# forge — Feature Roadmap

What's already built vs. what would close the gap with Claude Code / OpenCode,
grouped by priority. "Already built" items are marked so this stays an
accurate snapshot, not a wishlist that ignores progress.

---

## ✅ Already built

- Core agent loop: read/write/edit files, run shell commands
- Sandboxed to the project directory (blocks path traversal)
- Diff shown + confirmation before every write/edit
- Multi-turn session history (`/clear` actually resets it)
- Slash commands: `/help`, `/model`, `/yes`, `/confirm`, `/cwd`, `/usage`, `/clear`
- Live per-step dashboard (tokens, tok/s, elapsed time, session totals)
- Malformed tool-call JSON no longer crashes the loop (model gets the error, retries)
- Unhandled exceptions in any tool (permission errors, OSErrors, etc.) no longer crash the loop
- Pull any Ollama model from within forge
- 70+ passing pytest tests covering the above

---

## 🔴 Must-haves

The gaps that most affect whether this is usable as a daily driver, not just
a working prototype.

- **Streaming output** — right now forge waits for a full response before
  printing anything. At 12–20 tok/s on a 14B model, staring at nothing for
  10–15+ seconds per step feels broken even when it isn't. This is the
  single biggest perceived-speed fix available and doesn't require a bigger
  model or faster hardware.
- **Git awareness** — auto-diff summary across all files touched in a task,
  and ideally an auto-commit (or at least a suggested commit message) once a
  task completes successfully. Right now there's no record of what forge
  changed beyond the per-file confirmation prompts you already approved.
- **Config file, not just env vars** — a `forge.toml` or `.forgerc` in the
  project root (model, step limit, confirm settings) so config travels with
  the project instead of living only in your shell environment.
- **Persistent session across restarts** — right now closing forge loses all
  conversation history. Even a simple "save/resume last session" would
  matter for anything that spans more than one sitting.
- **Better error surfacing on Ollama connection failure** — right now a
  dropped/unreachable Ollama server mid-task produces a raw Python traceback.
  Should catch it and give a clear "Ollama isn't reachable at <host>" message
  instead.

---

## 🟡 Good to have

Real improvements, but the tool is genuinely usable without them.

- **Plan/build mode split** — a read-only "plan" pass before forge is
  allowed to write anything, like OpenCode's two-mode system. Useful for
  scoping out a task before committing to changes.
- **LSP integration** — real semantic understanding (go-to-definition,
  type-checking, find-references) instead of raw text search/edit. Biggest
  lift on this list, but the biggest capability jump too.
- **Vision support for image-to-code** — Gemma 3 (4B/12B/27B) already
  supports image input; wiring forge to accept a screenshot/mockup and hand
  it to a vision-capable local model would enable a basic "screenshot → UI
  code" workflow.
- **Undo / rollback** — since every write already keeps a diff, a `/undo`
  that reverts the last file change (or last N) without needing to touch git
  manually would be a natural extension of what's already tracked.
- **Auto model selection by task size** — route quick/simple edits to a
  smaller, faster model (7B) and reserve the 14B+ for complex multi-file
  tasks, automatically rather than manually switching with `/model`.
- **Cost/token budget alerts** — since `/usage` already tracks real token
  counts, a soft warning ("this session has used X tokens, Y minutes of
  compute") would help you notice runaway sessions before they eat 15+
  minutes on something that should've been quick.

---

## 🟢 Bonus / nice-to-have

Not essential, but would round things out or add polish.

- **MCP server support** — connect forge to external tools/data sources the
  way Claude Code and OpenCode do via the Model Context Protocol.
- **Test-runner integration** — a dedicated tool (separate from generic
  `run_shell`) that recognizes pytest/unittest output and summarizes
  pass/fail counts instead of dumping raw stdout.
- **Multiple concurrent sessions** — run forge against more than one project
  directory at once without restarting.
- **Plugin/extension system** — let you add custom tools without editing
  `tools.py` directly.
- **Simple local web dashboard** — an optional `--web` flag that serves a
  minimal read-only view of `/usage` stats and session history in a browser,
  for glancing at without leaving the terminal focused.
- **Voice input** — dictate a task instead of typing it. Low priority, but
  easy enough to bolt on later given how modular the CLI entry point is.

---

## Suggested order of attack

If picking up ordered by impact-to-effort ratio:

1. Streaming output (highest daily-use impact, moderate effort)
2. Ollama-unreachable error handling (small effort, removes a real failure mode)
3. Git diff summary / auto-commit
4. Config file support
5. Everything else, driven by what you actually miss while using it day to day
