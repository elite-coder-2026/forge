# forge — Task List

Mark `[x]` as each task is completed. Source: `roadmap.md`.

## ✅ Already built
- [x] Core agent loop (read/write/edit files, run shell)
- [x] Sandboxed to project directory
- [x] Diff + confirmation before writes/edits
- [x] Multi-turn session history (`/clear`)
- [x] Slash commands (`/help`, `/model`, `/yes`, `/confirm`, `/cwd`, `/usage`, `/clear`)
- [x] Live per-step dashboard
- [x] Malformed tool-call JSON handling
- [x] Unhandled tool exception handling
- [x] Pull Ollama models from within forge
- [x] Plan mode (read-only investigation)

## 🔴 Must-haves (in order of attack)
- [x] Streaming output (`llm.py` + tests)
- [x] Wire `on_token` into the CLI/REPL so it prints live
- [x] Ollama-unreachable error handling
- [x] Git awareness (diff summary / auto-commit)
- [x] Config file (`forge.toml`)
- [x] Persistent session across restarts

## 🟡 Good to have
- [x] LSP integration
- [x] Vision support (image-to-code)
- [x] Undo / rollback (`/undo`)
- [x] Auto model selection by task size
- [x] Token/cost budget alerts

## 🟢 Bonus
- [ ] MCP server support
- [ ] Test-runner integration
- [ ] Multiple concurrent sessions
- [ ] Plugin/extension system
- [ ] Web dashboard (`--web`)
- [ ] Voice input
