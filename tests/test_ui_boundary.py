"""The ui package is the only place that prints, reads input, or touches
rich / prompt_toolkit."""

import ast
import pathlib

FORGE = pathlib.Path(__file__).resolve().parent.parent / "forge"
TERMINAL_LIBS = {"rich", "prompt_toolkit"}


def _outside_ui():
    for path in FORGE.rglob("*.py"):
        if "ui" in path.relative_to(FORGE).parts[:1]:
            continue
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_print_or_input_outside_ui():
    offenders = [
        f"{path.relative_to(FORGE)}:{node.lineno}"
        for path, tree in _outside_ui()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("print", "input")
    ]
    assert offenders == []


def test_no_terminal_library_imports_outside_ui():
    offenders = []
    for path, tree in _outside_ui():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            if any(name.split(".")[0] in TERMINAL_LIBS for name in names):
                offenders.append(f"{path.relative_to(FORGE)}:{node.lineno}")
    assert offenders == []
