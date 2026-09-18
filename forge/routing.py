"""Automatic model selection by task size.

Quick edits go to a small, fast model; big or ambiguous work goes to the
main (larger) model. The decision is a cheap heuristic on the task text,
not another model call, so routing adds no latency.

Two design choices worth knowing:
- Ambiguous tasks stay on whichever model was used last. Ollama has to
  unload and load weights when the model changes, which costs seconds, so
  flip-flopping on borderline tasks would be slower than either model.
  With no history, ambiguity resolves to the main model.
- Routing only happens when a fast model is configured (`fast_model` /
  `FORGE_FAST_MODEL`). Otherwise every task uses the main model, as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# net score = complexity points - simplicity points
BIG_AT = 2  # net >= this: main model
FAST_AT = -2  # net <= this: fast model
FOLLOW_UP_WORDS = 4  # replies this short stay on the last model

_STRONG_COMPLEX = (
    "refactor", "redesign", "rearchitect", "architecture", "migrate",
    "migration", "rewrite", "implement", "overhaul", "from scratch",
)  # fmt: skip
_COMPLEX = (
    "across", "all files", "every file", "multiple files", "entire",
    "codebase", "whole project", "integrate", "add support", "end-to-end",
    "performance", "optimize", "concurrency", "investigate", "debug", "bug",
    "root cause", "failing", "crash", "security",
)  # fmt: skip
_SIMPLE = (
    "typo", "rename", "docstring", "comment", "format", "formatting", "bump",
    "whitespace", "unused", "explain", "what does", "what is", "where is",
    "show",
)  # fmt: skip

_FILE_REF = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|cpp|h|md|json|toml|ya?ml|html|css|sh|rb)\b"
)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+", re.MULTILINE)


@dataclass
class Choice:
    model: str
    reason: str = ""  # empty when routing isn't active


def _count_matches(text: str, terms: tuple[str, ...]) -> int:
    hits = 0
    for term in terms:
        pattern = r"\b" + re.escape(term) if " " not in term else re.escape(term)
        if re.search(pattern, text):
            hits += 1
    return hits


def complexity_score(task: str) -> int:
    """Positive: looks big. Negative: looks like a quick job."""
    text = task.lower()
    words = len(task.split())

    complex_points = 0
    if words > 60:
        complex_points += 3
    elif words > 30:
        complex_points += 2
    elif words > 15:
        complex_points += 1

    complex_points += 3 * _count_matches(text, _STRONG_COMPLEX)
    complex_points += min(6, 2 * _count_matches(text, _COMPLEX))

    files = len(set(_FILE_REF.findall(task)))
    complex_points += 3 if files >= 3 else 1 if files == 2 else 0

    items = len(_LIST_ITEM.findall(task))
    complex_points += 3 if items >= 3 else 1 if items == 2 else 0

    if "```" in task or "Traceback" in task:
        complex_points += 2

    simple_points = 2 if words <= 6 else 1 if words <= 12 else 0
    simple_points += min(4, 2 * _count_matches(text, _SIMPLE))

    return complex_points - simple_points


def choose_model(
    task: str,
    main: str,
    fast: str,
    *,
    images: bool = False,
    plan_mode: bool = False,
    last: str | None = None,
) -> Choice:
    """Pick the model for one task. `fast` empty (or same as `main`) turns
    routing off. Tasks with images or in plan mode always get `main`.
    """
    if not fast or fast == main:
        return Choice(main)
    if images:
        return Choice(main, "image-to-code work")
    if plan_mode:
        return Choice(main, "planning")

    # "yes, do it" / "now the other one": a short reply continues the work
    # on the model already loaded, whatever its own score says.
    if (
        last in (main, fast)
        and len(task.split()) <= FOLLOW_UP_WORDS
        and not _count_matches(task.lower(), _SIMPLE)
    ):
        return Choice(last, "short follow-up, staying on the loaded model")

    score = complexity_score(task)
    if score >= BIG_AT:
        return Choice(main, "larger task")
    if score <= FAST_AT:
        return Choice(fast, "quick task")
    if last in (main, fast):
        return Choice(last, "borderline task, staying on the loaded model")
    return Choice(main, "borderline task")
