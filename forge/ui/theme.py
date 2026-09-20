"""Every color and style in forge's terminal UI lives here.

Change a value below and the whole app recolors; components only ever refer
to these names. `STYLES` are rich styles, `PROMPT_STYLES` are prompt_toolkit
styles for the input box (same palette, that library's syntax).
"""

# Rich style names -> style strings.
STYLES = {
    "accent": "bold cyan",
    "muted": "dim",
    "user.border": "cyan",
    "user.text": "bold",
    "banner.name": "bold cyan",
    "banner.label": "dim",
    "banner.value": "",
    "status": "dim",
    "warning": "yellow",
    "error": "bold red",
    "permission.border": "yellow",
    "permission.title": "bold yellow",
    "tool.ok": "green",
    "tool.fail": "red",
    "tool.name": "bold",
    "tool.output": "dim",
    "diff.add": "green",
    "diff.remove": "red",
    "diff.hunk": "cyan",
}

# Syntax theme for fenced code blocks in assistant Markdown.
CODE_THEME = "monokai"

# Glyphs, so the look can change without touching components.
USER_BORDER = "▎ "
PROMPT_MARKER = "❯ "
RULE_CHAR = "─"
BANNER_MARK = "✦"

# prompt_toolkit styles for the input box.
PROMPT_STYLES = {
    "rule": "ansibrightblack",
    "prompt": "ansicyan bold",
    "hint": "ansibrightblack",
    "bottom-toolbar": "noreverse ansibrightblack",
    "toolbar.value": "noreverse ansiwhite",
    "toolbar.hint": "noreverse ansibrightblack italic",
    # Autocomplete menu
    "completion-menu": "bg:#1c1c1c #bcbcbc",
    "completion-menu.completion": "bg:#1c1c1c #bcbcbc",
    "completion-menu.completion.current": "bg:ansicyan #000000 bold",
    "completion-menu.meta.completion": "bg:#1c1c1c #808080",
    "completion-menu.meta.completion.current": "bg:ansicyan #003f4f",
    "scrollbar.background": "bg:#1c1c1c",
    "scrollbar.button": "bg:ansibrightblack",
}
