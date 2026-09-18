import pytest

from forge import main, undo
from forge.config import Config
from forge.tools import call_tool


@pytest.fixture(autouse=True)
def _clean_journal():
    undo.clear()
    yield
    undo.clear()


def _write(base, path, content):
    return call_tool("write_file", {"path": path, "content": content}, str(base))


def _edit(base, path, old, new):
    return call_tool("edit_file", {"path": path, "old_str": old, "new_str": new}, str(base))


def test_undo_removes_a_file_forge_created(tmp_path):
    _write(tmp_path, "new.txt", "hello")
    assert undo.undo(str(tmp_path)) == ["Removed new.txt (forge created it)"]
    assert not (tmp_path / "new.txt").exists()
    assert undo.count() == 0


def test_undo_restores_overwritten_file(tmp_path):
    (tmp_path / "a.txt").write_text("original")
    _write(tmp_path, "a.txt", "replaced")
    assert undo.undo(str(tmp_path)) == ["Restored a.txt"]
    assert (tmp_path / "a.txt").read_text() == "original"


def test_undo_restores_edited_file(tmp_path):
    (tmp_path / "a.txt").write_text("one two three")
    _edit(tmp_path, "a.txt", "two", "2")
    assert (tmp_path / "a.txt").read_text() == "one 2 three"
    undo.undo(str(tmp_path))
    assert (tmp_path / "a.txt").read_text() == "one two three"


def test_undo_restores_bytes_exactly(tmp_path):
    original = b"line1\r\nline2\r\n\xff\xfe"
    (tmp_path / "a.bin").write_bytes(original)
    _write(tmp_path, "a.bin", "text now")
    undo.undo(str(tmp_path))
    assert (tmp_path / "a.bin").read_bytes() == original


def test_repeated_changes_to_one_file_unwind_in_order(tmp_path):
    (tmp_path / "a.txt").write_text("v0")
    for version in ("v1", "v2", "v3"):
        _write(tmp_path, "a.txt", version)
    undo.undo(str(tmp_path))
    assert (tmp_path / "a.txt").read_text() == "v2"
    undo.undo(str(tmp_path), n=2)
    assert (tmp_path / "a.txt").read_text() == "v0"


def test_undo_n_reverts_several_files(tmp_path):
    _write(tmp_path, "a.txt", "a")
    _write(tmp_path, "b.txt", "b")
    _write(tmp_path, "c.txt", "c")
    results = undo.undo(str(tmp_path), n=2)
    assert len(results) == 2
    assert not (tmp_path / "c.txt").exists() and not (tmp_path / "b.txt").exists()
    assert (tmp_path / "a.txt").exists()
    assert undo.count() == 1


def test_undo_more_than_available_reverts_all_without_error(tmp_path):
    _write(tmp_path, "a.txt", "a")
    results = undo.undo(str(tmp_path), n=5)
    assert results == ["Removed a.txt (forge created it)"]


def test_nothing_to_undo():
    assert undo.undo(".") == ["Nothing to undo."]


def test_refuses_to_overwrite_changes_made_after_forge(tmp_path):
    (tmp_path / "a.txt").write_text("original")
    _write(tmp_path, "a.txt", "forge version")
    (tmp_path / "a.txt").write_text("user edited this since")

    results = undo.undo(str(tmp_path))
    assert "changed after forge wrote it" in results[0] and "/undo force" in results[0]
    assert (tmp_path / "a.txt").read_text() == "user edited this since"
    assert undo.count() == 1  # entry kept

    assert undo.undo(str(tmp_path), force=True) == ["Restored a.txt"]
    assert (tmp_path / "a.txt").read_text() == "original"


def test_refuses_when_file_was_deleted_afterwards(tmp_path):
    _write(tmp_path, "a.txt", "x")
    (tmp_path / "a.txt").unlink()
    assert "changed after" in undo.undo(str(tmp_path))[0]


def test_stops_at_a_changed_file_but_keeps_earlier_undos(tmp_path):
    _write(tmp_path, "a.txt", "a")
    _write(tmp_path, "b.txt", "b")
    (tmp_path / "b.txt").write_text("tampered")
    results = undo.undo(str(tmp_path), n=2)
    assert len(results) == 1 and "Stopped" in results[0]
    assert (tmp_path / "a.txt").exists()  # not reached


def test_failed_edit_records_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("abc")
    out = _edit(tmp_path, "a.txt", "zzz", "y")
    assert out.startswith("Error:")
    assert undo.count() == 0


def test_shell_changes_are_not_tracked(tmp_path):
    call_tool("run_shell", {"command": "echo hi > made.txt"}, str(tmp_path))
    assert (tmp_path / "made.txt").exists()
    assert undo.count() == 0


def test_journal_is_capped(tmp_path):
    for i in range(undo.MAX_ENTRIES + 5):
        undo.record(str(tmp_path / f"{i}.txt"), None, b"x")
    assert undo.count() == undo.MAX_ENTRIES


def test_history_lists_newest_first(tmp_path):
    (tmp_path / "old.txt").write_text("x")
    _write(tmp_path, "old.txt", "y")
    _write(tmp_path, "sub/new.txt", "z")
    assert undo.history(str(tmp_path)) == ["created sub/new.txt", "modified old.txt"]


def test_undo_reports_os_errors_instead_of_raising(tmp_path, monkeypatch):
    _write(tmp_path, "a.txt", "a")

    def boom(_):
        raise PermissionError("nope")

    monkeypatch.setattr(undo.os, "remove", boom)
    results = undo.undo(str(tmp_path))
    assert results[0].startswith("Error: could not undo a.txt")
    assert undo.count() == 1


# --- /undo command ----------------------------------------------------------


def _state(tmp_path):
    return main.REPLState(config=Config(working_dir=str(tmp_path)), client=None)


def test_command_undoes_last_change(tmp_path):
    _write(tmp_path, "a.txt", "a")
    assert "Removed a.txt" in main.handle_slash_command("/undo", _state(tmp_path))
    assert not (tmp_path / "a.txt").exists()


def test_command_accepts_a_count(tmp_path):
    _write(tmp_path, "a.txt", "a")
    _write(tmp_path, "b.txt", "b")
    out = main.handle_slash_command("/undo 2", _state(tmp_path))
    assert out.count("Removed") == 2


def test_command_list(tmp_path):
    state = _state(tmp_path)
    assert main.handle_slash_command("/undo list", state) == "Nothing to undo."
    _write(tmp_path, "a.txt", "a")
    out = main.handle_slash_command("/undo list", state)
    assert "created a.txt" in out


def test_command_force(tmp_path):
    (tmp_path / "a.txt").write_text("orig")
    _write(tmp_path, "a.txt", "forge")
    (tmp_path / "a.txt").write_text("tampered")
    state = _state(tmp_path)
    assert "Stopped" in main.handle_slash_command("/undo", state)
    assert "Restored a.txt" in main.handle_slash_command("/undo force", state)
    assert (tmp_path / "a.txt").read_text() == "orig"


@pytest.mark.parametrize("arg", ["abc", "0", "-1", "1 2", "list now"])
def test_command_rejects_bad_arguments(tmp_path, arg):
    _write(tmp_path, "a.txt", "a")
    out = main.handle_slash_command(f"/undo {arg}", _state(tmp_path))
    assert out.startswith("Usage:")
    assert (tmp_path / "a.txt").exists()


def test_help_mentions_undo():
    assert "/undo" in main.HELP_TEXT
