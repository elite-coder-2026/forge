"""Console factory. Consoles are built per call and never bind a file, so
they follow whatever `sys.stdout` / `sys.stderr` is at write time (tests that
capture output keep working). rich itself honors NO_COLOR and strips styling
when the stream isn't a terminal."""

from rich.console import Console
from rich.theme import Theme

from .theme import STYLES


def get_console(err: bool = False) -> Console:
    return Console(stderr=err, theme=Theme(STYLES), highlight=False)
