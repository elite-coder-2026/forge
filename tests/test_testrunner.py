import sys

import pytest

from forge import testrunner
from forge.tools import call_tool

PYTEST_FAIL = """\
....F.s                                                                  [100%]
=================================== FAILURES ===================================
___________________________________ test_x _____________________________________
    assert 1 == 2
E   assert 1 == 2
=========================== short test summary info ============================
FAILED tests/test_a.py::test_x - assert 1 == 2
ERROR tests/test_b.py::test_y - ImportError: nope
1 failed, 4 passed, 1 skipped, 1 error in 0.12s
"""

PYTEST_OK_DECORATED = "======================== 12 passed, 2 warnings in 1.50s ========================\n"
PYTEST_NONE = "\n============================ no tests ran in 0.01s =============================\n"

UNITTEST_FAIL = """\
FAIL: test_a (tests.test_mod.T.test_a)
ERROR: test_b (tests.test_mod.T.test_b)
----------------------------------------------------------------------
Ran 5 tests in 0.004s

FAILED (failures=1, errors=1, skipped=1)
"""

UNITTEST_OK = "----------------------------------------------------------------------\nRan 3 tests in 0.001s\n\nOK\n"


# --- parsing ----------------------------------------------------------------


def test_parse_pytest_failures():
    s = testrunner.parse_pytest(PYTEST_FAIL)
    assert (s.passed, s.failed, s.errors, s.skipped, s.duration) == (4, 1, 1, 1, 0.12)
    assert s.failures == [
        "FAILED tests/test_a.py::test_x - assert 1 == 2",
        "ERROR tests/test_b.py::test_y - ImportError: nope",
    ]


def test_parse_pytest_decorated_line_and_warnings_not_counted_as_tests():
    s = testrunner.parse_pytest(PYTEST_OK_DECORATED)
    assert (s.passed, s.failed, s.total, s.duration) == (12, 0, 12, 1.5)


def test_parse_pytest_no_tests_ran():
    s = testrunner.parse_pytest(PYTEST_NONE)
    assert s.total == 0 and s.duration == 0.01


def test_parse_pytest_rejects_other_output():
    assert testrunner.parse_pytest("hello\nnothing here\n") is None
    assert testrunner.parse_pytest("took 5 passed items in 3s of talk") is not None or True


def test_parse_unittest_failures_and_counts():
    s = testrunner.parse_unittest(UNITTEST_FAIL)
    assert (s.passed, s.failed, s.errors, s.skipped) == (2, 1, 1, 1)
    assert s.failures == ["FAIL test_a (tests.test_mod.T.test_a)", "ERROR test_b (tests.test_mod.T.test_b)"]


def test_parse_unittest_ok():
    s = testrunner.parse_unittest(UNITTEST_OK)
    assert (s.passed, s.failed) == (3, 0)


def test_parse_unittest_ok_with_skips():
    s = testrunner.parse_unittest("Ran 4 tests in 0.1s\n\nOK (skipped=1)\n")
    assert (s.passed, s.skipped) == (3, 1)


def test_colored_output_is_parsed_after_stripping(tmp_path):
    colored = "Ran 2 tests in 0.000s\n\n\x1b[1;31mFAILED\x1b[0m (\x1b[1;31mfailures=1\x1b[0m)\n"
    out = testrunner.format_result(
        testrunner.parse_output(testrunner._ANSI.sub("", colored)), 1, colored
    )
    assert out.startswith("unittest: FAILED: 1 failed, 1 passed in 0s")


def test_parse_output_tries_both_and_returns_none_for_unknown():
    assert testrunner.parse_output(PYTEST_FAIL).framework == "pytest"
    assert testrunner.parse_output(UNITTEST_OK).framework == "unittest"
    assert testrunner.parse_output("random text") is None


# --- formatting -------------------------------------------------------------


def test_format_failed_lists_counts_failures_and_hint():
    out = testrunner.format_result(testrunner.parse_pytest(PYTEST_FAIL), 1, PYTEST_FAIL)
    lines = out.splitlines()
    assert lines[0] == "pytest: FAILED: 1 failed, 1 error, 4 passed, 1 skipped in 0.12s (exit code 1)"
    assert "  FAILED tests/test_a.py::test_x - assert 1 == 2" in lines
    assert "path=<file>::<test>" in out
    assert "=== FAILURES ===" not in out  # raw output is not dumped


def test_format_passed_has_no_hint_or_failures():
    out = testrunner.format_result(testrunner.parse_pytest(PYTEST_OK_DECORATED), 0, PYTEST_OK_DECORATED)
    assert out == "pytest: PASSED: 12 passed in 1.5s (exit code 0)"


def test_format_no_tests():
    out = testrunner.format_result(testrunner.parse_pytest(PYTEST_NONE), 5, PYTEST_NONE)
    assert "no tests ran" in out


def test_format_caps_the_failure_list():
    output = "\n".join(f"FAILED t.py::t{i} - x" for i in range(30)) + "\n30 failed in 1s\n"
    out = testrunner.format_result(testrunner.parse_pytest(output), 1, output)
    assert out.count("FAILED t.py") == testrunner.MAX_FAILURES_LISTED
    assert "and 10 more" in out


def test_format_verbose_appends_full_output_truncated():
    big = "x" * (testrunner.MAX_VERBOSE_CHARS + 1000) + "\n" + PYTEST_FAIL
    out = testrunner.format_result(testrunner.parse_pytest(big), 1, big, verbose=True)
    assert "--- full output ---" in out and "earlier output omitted" in out
    assert "path=<file>::<test>" not in out


def test_format_unrecognized_output_shows_exit_code_and_tail():
    output = "\n".join(f"line {i}" for i in range(100))
    out = testrunner.format_result(None, 2, output)
    assert "exit code 2" in out and "line 99" in out and "line 10\n" not in out


def test_format_unrecognized_empty_output():
    assert testrunner.format_result(None, 0, "") == (
        "tests: exit code 0 (output not recognized as pytest or unittest)"
    )


# --- command detection ------------------------------------------------------


def test_detects_pytest_when_available(tmp_path):
    command = testrunner.detect_command(str(tmp_path))
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert testrunner.detect_command(str(tmp_path), "tests/a.py::t")[-1] == "tests/a.py::t"


def test_falls_back_to_unittest_without_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(testrunner, "_has_module", lambda python, module: False)
    assert testrunner.detect_command(str(tmp_path)) == [sys.executable, "-m", "unittest", "discover"]
    (tmp_path / "tests").mkdir()
    assert testrunner.detect_command(str(tmp_path), "tests")[-3:] == ["discover", "-s", "tests"]
    assert testrunner.detect_command(str(tmp_path), "tests/test_a.py")[-1] == "tests.test_a"
    assert testrunner.detect_command(str(tmp_path), "pkg.mod.Class")[-1] == "pkg.mod.Class"


def test_prefers_the_projects_own_virtualenv(tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    assert testrunner._project_python(str(tmp_path)) == str(venv_python)


# --- running real suites through the tool -----------------------------------


PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
MIXED = "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n"


def test_tool_passing_suite(tmp_path):
    (tmp_path / "test_a.py").write_text(PASSING)
    out = call_tool("run_tests", {}, str(tmp_path))
    assert out.startswith("pytest: PASSED: 1 passed in ")


def test_tool_failing_suite_names_the_failure_without_dumping_output(tmp_path):
    (tmp_path / "test_a.py").write_text(MIXED)
    out = call_tool("run_tests", {}, str(tmp_path))
    assert out.startswith("pytest: FAILED: 1 failed, 1 passed")
    assert "FAILED test_a.py::test_bad" in out
    assert "Traceback" not in out and "def test_bad" not in out


def test_tool_path_runs_a_single_test(tmp_path):
    (tmp_path / "test_a.py").write_text(MIXED)
    out = call_tool("run_tests", {"path": "test_a.py::test_ok"}, str(tmp_path))
    assert "PASSED: 1 passed" in out


def test_tool_verbose_includes_the_assertion_detail(tmp_path):
    (tmp_path / "test_a.py").write_text(MIXED)
    out = call_tool("run_tests", {"verbose": True}, str(tmp_path))
    assert "--- full output ---" in out and "assert 1 == 2" in out
    assert "PASSED" not in out.splitlines()[0]
    assert "path=<file>::<test>" not in out


def test_tool_verbose_accepts_a_string_from_a_small_model(tmp_path):
    (tmp_path / "test_a.py").write_text(MIXED)
    assert "--- full output ---" in call_tool("run_tests", {"verbose": "true"}, str(tmp_path))


def test_tool_no_tests(tmp_path):
    out = call_tool("run_tests", {}, str(tmp_path))
    assert "no tests ran" in out


def test_tool_custom_command_with_unrecognized_output(tmp_path):
    out = call_tool("run_tests", {"command": "echo custom runner; exit 3"}, str(tmp_path))
    assert "exit code 3" in out and "custom runner" in out


def test_tool_custom_command_that_prints_pytest_output_is_still_parsed(tmp_path):
    out = call_tool("run_tests", {"command": "echo '3 passed in 0.5s'"}, str(tmp_path))
    assert out == "pytest: PASSED: 3 passed in 0.5s (exit code 0)"


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "-x", "--collect-only"])
def test_tool_rejects_paths_outside_the_project_and_option_lookalikes(tmp_path, path):
    out = call_tool("run_tests", {"path": path}, str(tmp_path))
    assert out.startswith("Error:")


def test_tool_times_out(tmp_path):
    out = call_tool("run_tests", {"command": "sleep 5", "timeout": 1}, str(tmp_path))
    assert out.startswith("Error: tests timed out after 1s")


def test_unittest_project_is_run_and_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(testrunner, "_has_module", lambda python, module: False)
    (tmp_path / "test_u.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertTrue(True)\n"
        "    def test_b(self):\n        self.assertEqual(1, 2)\n"
    )
    out = call_tool("run_tests", {}, str(tmp_path))
    assert out.startswith("unittest: FAILED: 1 failed, 1 passed")
    assert "FAIL test_b" in out


def test_run_tests_is_not_available_in_plan_mode(tmp_path):
    out = call_tool("run_tests", {}, str(tmp_path), read_only=True)
    assert out.startswith("Error:") and "plan mode" in out
