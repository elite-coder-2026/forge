import os
import subprocess

import pytest

from forge import tools
from forge.tools import (
    TOOL_SCHEMAS,
    ToolError,
    call_tool,
    edit_file,
    list_dir,
    read_file,
    run_shell,
    tool_schemas_for,
    write_file,
)


# ---------------------------------------------------------------------------
# _safe_path
# ---------------------------------------------------------------------------


def test_safe_path_allows_path_inside_base(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    result = tools._safe_path(str(tmp_path), "a.txt")
    assert result == os.path.realpath(str(tmp_path / "a.txt"))


def test_safe_path_rejects_dotdot_traversal(tmp_path):
    with pytest.raises(ToolError):
        tools._safe_path(str(tmp_path), "../outside.txt")


def test_safe_path_rejects_absolute_path_outside_base(tmp_path, monkeypatch):
    outside = tmp_path.parent / "definitely_outside_forge_sandbox.txt"
    with pytest.raises(ToolError):
        tools._safe_path(str(tmp_path), str(outside))


def test_safe_path_rejects_symlink_escaping_sandbox(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("top secret")

    link = sandbox / "escape"
    link.symlink_to(secret)

    with pytest.raises(ToolError):
        tools._safe_path(str(sandbox), "escape")


def test_safe_path_allows_symlink_inside_sandbox(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    real_file = sandbox / "real.txt"
    real_file.write_text("hi")
    link = sandbox / "link.txt"
    link.symlink_to(real_file)

    result = tools._safe_path(str(sandbox), "link.txt")
    assert result == os.path.realpath(str(real_file))


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_file_happy_path(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    assert read_file(str(tmp_path), "a.txt") == "hello world"


def test_read_file_missing_raises_tool_error(tmp_path):
    with pytest.raises(ToolError):
        read_file(str(tmp_path), "does_not_exist.txt")


def test_read_file_permission_denied(tmp_path):
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")

    target = tmp_path / "locked.txt"
    target.write_text("secret")
    target.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            read_file(str(tmp_path), "locked.txt")
    finally:
        target.chmod(0o644)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def test_write_file_creates_parent_dirs(tmp_path):
    msg = write_file(str(tmp_path), "nested/dir/out.txt", "content")
    assert (tmp_path / "nested" / "dir" / "out.txt").read_text() == "content"
    assert "out.txt" in msg or "7 bytes" in msg


def test_write_file_overwrites_existing(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    write_file(str(tmp_path), "a.txt", "new")
    assert (tmp_path / "a.txt").read_text() == "new"


def test_write_file_rejects_escape(tmp_path):
    with pytest.raises(ToolError):
        write_file(str(tmp_path), "../escape.txt", "content")


def test_write_file_permission_denied(tmp_path):
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")

    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            write_file(str(readonly_dir), "new.txt", "content")
    finally:
        readonly_dir.chmod(0o700)


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def test_edit_file_replaces_unique_match(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    edit_file(str(tmp_path), "a.txt", "world", "there")
    assert (tmp_path / "a.txt").read_text() == "hello there"


def test_edit_file_raises_when_old_str_missing(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    with pytest.raises(ToolError):
        edit_file(str(tmp_path), "a.txt", "not present", "x")


def test_edit_file_raises_when_old_str_ambiguous(tmp_path):
    (tmp_path / "a.txt").write_text("dup dup dup")
    with pytest.raises(ToolError):
        edit_file(str(tmp_path), "a.txt", "dup", "single")


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def test_list_dir_lists_files_and_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("y")

    listing = list_dir(str(tmp_path), ".")
    assert "a.txt" in listing
    assert "sub/" in listing
    assert os.path.join("sub", "b.txt") in listing


def test_list_dir_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_dir(str(tmp_path), "empty") == "<empty>"


def test_list_dir_not_a_directory_raises(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ToolError):
        list_dir(str(tmp_path), "a.txt")


def test_list_dir_handles_unreadable_subdirectory_gracefully(tmp_path):
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")

    (tmp_path / "visible.txt").write_text("x")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("y")
    locked.chmod(0o000)
    try:
        # Must not raise, even though a subdirectory can't be scanned.
        listing = list_dir(str(tmp_path), ".")
        assert "visible.txt" in listing
    finally:
        locked.chmod(0o700)


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------


def test_run_shell_happy_path(tmp_path):
    result = run_shell(str(tmp_path), "echo hi")
    assert "exit code: 0" in result
    assert "hi" in result


def test_run_shell_nonzero_exit_reports_code_and_stderr(tmp_path):
    result = run_shell(str(tmp_path), "echo oops 1>&2; exit 3")
    assert "exit code: 3" in result
    assert "oops" in result


def test_run_shell_timeout_propagates(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep 100", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        run_shell(str(tmp_path), "sleep 100", timeout=1)


def test_run_shell_oserror_when_spawn_fails(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("cannot spawn shell")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OSError):
        run_shell(str(tmp_path), "echo hi")


# ---------------------------------------------------------------------------
# call_tool — the dispatcher that must never raise (bug #5)
# ---------------------------------------------------------------------------


def test_call_tool_happy_path(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    result = call_tool("read_file", {"path": "a.txt"}, str(tmp_path))
    assert result == "hi"


def test_call_tool_unknown_tool_returns_error_string(tmp_path):
    result = call_tool("does_not_exist", {}, str(tmp_path))
    assert result.startswith("Error:")


def test_call_tool_bad_arguments_returns_error_string_not_raise(tmp_path):
    # missing required "path"
    result = call_tool("read_file", {}, str(tmp_path))
    assert result.startswith("Error:")


def test_call_tool_tool_error_returns_error_string(tmp_path):
    result = call_tool("read_file", {"path": "missing.txt"}, str(tmp_path))
    assert result.startswith("Error:")


def test_call_tool_permission_error_returns_error_string_not_raise(tmp_path):
    """Regression test for bug #5: call_tool used to only catch ToolError
    and TypeError, so a PermissionError (or any other unexpected
    exception) would propagate uncaught and kill the whole agent loop.
    """
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")

    target = tmp_path / "locked.txt"
    target.write_text("secret")
    target.chmod(0o000)
    try:
        result = call_tool("read_file", {"path": "locked.txt"}, str(tmp_path))
        assert isinstance(result, str)
        assert result.startswith("Error:")
    finally:
        target.chmod(0o644)


def test_call_tool_catches_arbitrary_unexpected_exception(tmp_path, monkeypatch):
    """The general form of bug #5: call_tool must convert *any* exception
    class into a string, not just the ones anyone thought to enumerate.
    """

    def exploding_read_file(base_dir, path):
        raise RuntimeError("disk caught fire")

    monkeypatch.setitem(tools._DISPATCH, "read_file", exploding_read_file)

    result = call_tool("read_file", {"path": "a.txt"}, str(tmp_path))
    assert result == "Error: RuntimeError: disk caught fire"


def test_call_tool_run_shell_timeout_returns_error_string(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep 100", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = call_tool("run_shell", {"command": "sleep 100"}, str(tmp_path), shell_timeout=1)
    assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# plan mode (read-only)
# ---------------------------------------------------------------------------


def test_tool_schemas_for_full_access_returns_everything():
    assert tool_schemas_for(read_only=False) == TOOL_SCHEMAS


def test_tool_schemas_for_read_only_excludes_write_tools():
    names = {s["function"]["name"] for s in tool_schemas_for(read_only=True)}
    assert names == {"read_file", "list_dir"}


def test_call_tool_read_only_allows_read_file(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    result = call_tool("read_file", {"path": "a.txt"}, str(tmp_path), read_only=True)
    assert result == "hi"


def test_call_tool_read_only_allows_list_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    result = call_tool("list_dir", {}, str(tmp_path), read_only=True)
    assert "a.txt" in result


def test_call_tool_read_only_blocks_write_file(tmp_path):
    result = call_tool(
        "write_file", {"path": "a.txt", "content": "x"}, str(tmp_path), read_only=True
    )
    assert result.startswith("Error:")
    assert "plan mode" in result
    assert not (tmp_path / "a.txt").exists()


def test_call_tool_read_only_blocks_edit_file(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    result = call_tool(
        "edit_file",
        {"path": "a.txt", "old_str": "hello", "new_str": "bye"},
        str(tmp_path),
        read_only=True,
    )
    assert result.startswith("Error:")
    assert (tmp_path / "a.txt").read_text() == "hello"


def test_call_tool_read_only_blocks_run_shell(tmp_path):
    result = call_tool("run_shell", {"command": "echo hi"}, str(tmp_path), read_only=True)
    assert result.startswith("Error:")
    assert "plan mode" in result
