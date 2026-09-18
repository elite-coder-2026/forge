import subprocess

import pytest

from forge import gitutil, main


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "config", "user.email", "t@example.test")
    _run(tmp_path, "config", "user.name", "T")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("one\n")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _log(repo):
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
    ).stdout.split("\n")[:-1]


def test_snapshot_none_outside_repo(tmp_path):
    assert gitutil.snapshot(str(tmp_path)) is None


def test_changed_files_finds_modified_and_new(repo):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("x\n")
    after = gitutil.snapshot(str(repo))
    assert gitutil.changed_files(before, after) == ["a.txt", "new.txt"]


def test_changed_files_ignores_files_dirty_before_and_untouched(repo):
    (repo / "b.txt").write_text("already dirty\n")
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")
    after = gitutil.snapshot(str(repo))
    assert gitutil.changed_files(before, after) == ["a.txt"]


def test_changed_files_detects_further_edit_to_dirty_file(repo):
    (repo / "a.txt").write_text("edit 1\n")
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("edit 2\n")
    after = gitutil.snapshot(str(repo))
    assert gitutil.changed_files(before, after) == ["a.txt"]


def test_changed_files_empty_when_nothing_changed(repo):
    before = gitutil.snapshot(str(repo))
    assert gitutil.changed_files(before, gitutil.snapshot(str(repo))) == []


def test_diff_summary_lists_modified_and_new(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "new.txt").write_text("x\n")
    after = gitutil.snapshot(str(repo))
    summary = gitutil.diff_summary(after, ["a.txt", "new.txt"])
    assert "Changed 2 file(s)" in summary
    assert "a.txt" in summary
    assert "new.txt (new file)" in summary


def test_deleted_file_is_detected(repo):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").unlink()
    after = gitutil.snapshot(str(repo))
    assert gitutil.changed_files(before, after) == ["a.txt"]


def test_suggest_message_uses_first_line_and_truncates():
    assert gitutil.suggest_message("fix bug\nmore detail") == "fix bug"
    long = gitutil.suggest_message("x" * 200)
    assert len(long) == 72 and long.endswith("...")
    assert gitutil.suggest_message("   ") == "forge changes"


def test_commit_only_includes_listed_files(repo):
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("unrelated\n")
    (repo / "new.txt").write_text("x\n")
    ok, _ = gitutil.commit(str(repo), ["a.txt", "new.txt"], "do the thing")
    assert ok
    assert _log(repo)[0] == "do the thing"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert status.strip() == "M b.txt"


def test_commit_failure_is_reported_not_raised(repo):
    ok, output = gitutil.commit(str(repo), ["does-not-exist.txt"], "msg")
    assert not ok and output


# --- main._git_report -------------------------------------------------------


def test_report_none_snapshot_is_a_noop(capsys):
    main._git_report(None, "task", interactive=True)
    assert capsys.readouterr().out == ""


def test_report_no_changes_prints_nothing_and_does_not_prompt(repo, monkeypatch, capsys):
    before = gitutil.snapshot(str(repo))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt"))
    main._git_report(before, "task", interactive=True)
    assert capsys.readouterr().out == ""


def test_report_interactive_yes_commits(repo, monkeypatch, capsys):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    main._git_report(before, "update a", interactive=True)
    assert "a.txt" in capsys.readouterr().out
    assert _log(repo)[0] == "update a"


def test_report_interactive_no_does_not_commit(repo, monkeypatch):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    main._git_report(before, "update a", interactive=True)
    assert _log(repo) == ["init"]


def test_report_interactive_eof_does_not_commit(repo, monkeypatch):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")

    def eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    main._git_report(before, "update a", interactive=True)
    assert _log(repo) == ["init"]


def test_report_one_shot_suggests_but_never_prompts_or_commits(repo, monkeypatch, capsys):
    before = gitutil.snapshot(str(repo))
    (repo / "a.txt").write_text("two\n")
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt"))
    main._git_report(before, "update a", interactive=False)
    out = capsys.readouterr().out
    assert 'Suggested commit message: "update a"' in out
    assert _log(repo) == ["init"]
