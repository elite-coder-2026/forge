# Features to add

Tick an item off as soon as it is finished. Items marked (needs OK) change the
agent loop or existing behavior and want your approval first.

## Terminal UI

- [x] Tool call blocks: tool name, target (path or command) and status (done / failed)
- [x] Spinner while a tool is running
- [x] Colored unified diff in a bordered panel after a file edit (shown in `auto` and `dangerous`; in `default` the approval prompt already showed it)
- [x] Shell commands: show the command, trimmed output and the exit code
- [x] Ctrl+C cancels the current turn and returns to the prompt (Ctrl+C or Ctrl+D at the prompt still exits, because scripts/dev.sh restarts the app by sending it Ctrl+C)
- [x] Wire `render_tool_start`, `render_tool_result` and `render_diff` in `forge/ui/__init__.py`
- [ ] Look at all of the above in a real terminal (only checked with forced-terminal output and tests)

## Input box and status bar

- [ ] Status bar survives Cmd+K (today it is pushed off screen; needs a real-terminal look at the layout)
- [ ] Confirm the bottom border sits directly under the input text in a real terminal
- [x] Autocomplete menu shows a description beside each slash command
- [x] Tab completion for arguments: `/mode`, `/model`, `/fast` (installed models), `/auto`, `/session`
- [ ] Confirm the autocomplete menu colors and selected-item highlight look right in a real terminal
- [ ] Confirm Shift+Enter and Shift+Tab in the terminals you use (some send different sequences)

## Modes and permissions

- [ ] Single-key answers at the approval prompt (no Enter needed)
- [ ] Set the default mode in `forge.toml` / an env var (needs OK: new config key)
- [ ] Gate MCP and plugin tools in `default` mode (they are not asked about today)
- [ ] Approvals for the `--chat` web page (it has no terminal to ask on)
- [ ] Show the mode in the REPL prompt, not only in the status bar

## Voice

- [ ] A way to cancel while it is listening (Ctrl+C currently means "done speaking")
- [ ] Try real dictation end to end (needs a mic; never run in tests)

## Quality and tooling

- [ ] Real-terminal check of streamed Markdown and code highlighting
- [ ] Test that `theme.py` alone recolors the whole UI
- [ ] Add ruff or pyflakes to the `dev` extra so linting is one command
- [ ] Run scripts/dev.sh with the interactive REPL and note any terminal problems
- [ ] Decide whether the web dashboard and chat page should share the terminal theme
