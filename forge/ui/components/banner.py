"""Small startup banner: name, version, model, working directory."""

from __future__ import annotations

import os

from rich.text import Text

from ..console import get_console
from ..theme import BANNER_MARK


def render_banner(version: str, model: str, cwd: str) -> None:
    home = os.path.expanduser("~")
    shown_cwd = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + os.sep) else cwd
    console = get_console()

    title = Text()
    title.append(f"{BANNER_MARK} forge", style="banner.name")
    title.append(f" {version}", style="banner.label")
    console.print(title)
    for label, value in (("model", model), ("cwd", shown_cwd)):
        row = Text("  ")
        row.append(f"{label:<6}", style="banner.label")
        row.append(value, style="banner.value")
        console.print(row)
    console.print("  /help for commands · /exit to quit", style="muted")
    console.print()
