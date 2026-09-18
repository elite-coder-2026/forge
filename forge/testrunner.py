"""A test-runner tool that summarizes instead of dumping raw output.

`run_shell("pytest")` hands the model hundreds of lines. This runs the
suite and returns one line of counts plus the failing test names, which
is what the model actually needs to decide its next step. pytest and
unittest output is recognized; any other command still runs, with the
exit code and the tail of its output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 300
MAX_FAILURES_LISTED = 20
MAX_LINE = 200
TAIL_LINES = 40
MAX_VERBOSE_CHARS = 10_000

# Newer Pythons and many runners colorize output even when captured.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_PYTEST_COUNT = re.compile(
    r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_TIMED = re.compile(r"\bin (\d+(?:\.\d+)?)s\b")
_PYTEST_FAILURE = re.compile(r"^(FAILED|ERROR) (\S.*)$")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in (\d+(?:\.\d+)?)s", re.MULTILINE)
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.MULTILINE)
_UNITTEST_FAILURE = re.compile(r"^(FAIL|ERROR): (.+)$")


@dataclass
class Summary:
    framework: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


def parse_pytest(output: str) -> Summary | None:
    """Parse pytest's closing line ("2 failed, 10 passed in 0.12s") and its
    short-summary FAILED/ERROR lines. None if this isn't pytest output."""
    lines = output.splitlines()
    for line in reversed(lines):
        timed = _PYTEST_TIMED.search(line)
        if not timed:
            continue
        counts = _PYTEST_COUNT.findall(line)
        if not counts and "no tests ran" not in line:
            continue

        summary = Summary("pytest", duration=float(timed.group(1)))
        for number, kind in counts:
            n = int(number)
            if kind == "passed":
                summary.passed = n
            elif kind == "failed":
                summary.failed = n
            elif kind.startswith("error"):
                summary.errors = n
            elif kind == "skipped":
                summary.skipped = n
        summary.failures = [
            f"{m.group(1)} {m.group(2)}"[:MAX_LINE]
            for m in (_PYTEST_FAILURE.match(l) for l in lines)
            if m
        ]
        return summary
    return None


def parse_unittest(output: str) -> Summary | None:
    ran = _UNITTEST_RAN.search(output)
    if not ran:
        return None
    total, seconds = int(ran.group(1)), float(ran.group(2))
    summary = Summary("unittest", duration=seconds)

    counts: dict[str, int] = {}
    result = _UNITTEST_RESULT.search(output, ran.end())
    if result and result.group(2):
        for part in result.group(2).split(","):
            key, _, value = part.strip().partition("=")
            if value.isdigit():
                counts[key] = int(value)

    summary.failed = counts.get("failures", 0)
    summary.errors = counts.get("errors", 0)
    summary.skipped = counts.get("skipped", 0)
    summary.passed = max(0, total - summary.failed - summary.errors - summary.skipped)
    summary.failures = [
        f"{m.group(1)} {m.group(2)}"[:MAX_LINE]
        for m in (_UNITTEST_FAILURE.match(l) for l in output.splitlines())
        if m
    ]
    return summary


def parse_output(output: str) -> Summary | None:
    return parse_pytest(output) or parse_unittest(output)


def _project_python(base_dir: str) -> str:
    """The project's own virtualenv interpreter if it has one, else ours."""
    for candidate in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        path = os.path.join(base_dir, candidate)
        if os.path.isfile(path):
            return path
    return sys.executable


def _has_module(python: str, module: str) -> bool:
    try:
        return (
            subprocess.run(
                [python, "-c", f"import {module}"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _unittest_target(base_dir: str, path: str) -> list[str]:
    full = os.path.join(base_dir, path)
    if os.path.isdir(full):
        return ["discover", "-s", path]
    if path.endswith(".py"):
        return [path[:-3].replace(os.sep, ".").replace("/", ".")]
    return [path]  # already a dotted test id


def detect_command(base_dir: str, path: str | None = None) -> list[str]:
    """pytest if the project's Python has it, otherwise unittest."""
    python = _project_python(base_dir)
    if _has_module(python, "pytest"):
        command = [python, "-m", "pytest", "-q", "-rfE", "--color=no"]
        return command + ([path] if path else [])
    command = [python, "-m", "unittest"]
    return command + (_unittest_target(base_dir, path) if path else ["discover"])


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    kept = text.strip().splitlines()[-lines:]
    return "\n".join(line[:MAX_LINE * 2] for line in kept)


def format_result(
    summary: Summary | None, returncode: int, output: str, verbose: bool = False
) -> str:
    if summary is None:
        text = f"tests: exit code {returncode} (output not recognized as pytest or unittest)"
        return f"{text}\n{_tail(output)}" if output.strip() else text

    parts = [
        f"{n} {label}"
        for n, label in (
            (summary.failed, "failed"),
            (summary.errors, "error" if summary.errors == 1 else "errors"),
            (summary.passed, "passed"),
            (summary.skipped, "skipped"),
        )
        if n
    ]
    counts = ", ".join(parts) if parts else "no tests ran"
    timing = f" in {summary.duration:g}s" if summary.duration is not None else ""
    status = "PASSED" if returncode == 0 else "FAILED"
    lines = [f"{summary.framework}: {status}: {counts}{timing} (exit code {returncode})"]

    if summary.failures:
        lines.append("Failures:")
        lines.extend(f"  {f}" for f in summary.failures[:MAX_FAILURES_LISTED])
        if len(summary.failures) > MAX_FAILURES_LISTED:
            lines.append(f"  ... and {len(summary.failures) - MAX_FAILURES_LISTED} more")
    if returncode != 0 and not verbose:
        lines.append(
            "Re-run one test with path=<file>::<test> for its details, or verbose=true for the full output."
        )
    if verbose:
        full = output.strip()
        if len(full) > MAX_VERBOSE_CHARS:
            full = "... [earlier output omitted]\n" + full[-MAX_VERBOSE_CHARS:]
        lines.append("--- full output ---")
        lines.append(full)
    return "\n".join(lines)


def run_tests(
    base_dir: str,
    path: str | None = None,
    command: str | None = None,
    verbose: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run the project's tests and return a compact summary."""
    argv: list[str] | str = command if command else detect_command(base_dir, path)
    try:
        result = subprocess.run(
            argv,
            shell=isinstance(argv, str),
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return f"Error: tests timed out after {timeout}s\n{_tail(partial, 15)}".rstrip()

    output = _ANSI.sub("", (result.stdout or "") + (result.stderr or ""))
    return format_result(parse_output(output), result.returncode, output, verbose)
