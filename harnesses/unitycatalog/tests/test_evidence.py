import sys
from pathlib import Path

import pytest

from codex_uc.contracts import Check, Reproduction
from codex_uc.evidence import proves_failure, proves_success, run_check


def command(tmp_path, xml, exit_code):
    script = tmp_path / "check.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        + f"Path('result.xml').write_text({xml!r})\n"
        + f"sys.exit({exit_code})\n"
    )
    return Check(
        name="behavior check",
        argv=[sys.executable, str(script)],
        cwd=".",
        purpose="test",
        junit_glob="result.xml",
        timeout_seconds=10,
    )


def reproduction():
    return Reproduction(
        check_index=0,
        test_class="ValueTest",
        test_name="negative",
        failure_text="expected 2",
        explanation="Check the returned value.",
    )


def test_real_command_and_fresh_report_prove_the_specific_assertion(tmp_path, local_executor):
    check = command(
        tmp_path,
        '<testsuite tests="1" failures="1"><testcase classname="ValueTest" name="negative"><failure type="AssertionError">expected 2; got 0</failure></testcase></testsuite>',
        1,
    )
    result = run_check(tmp_path, check, tmp_path / "evidence", local_executor)
    assert proves_failure(result, reproduction())
    assert not proves_success(result)
    assert (tmp_path / "evidence/report-0.xml").exists()


@pytest.mark.parametrize(
    "xml",
    [
        '<testsuite><testcase classname="ValueTest" name="negative"><error type="ImportError">setup failed</error></testcase></testsuite>',
        '<testsuite><testcase classname="ValueTest" name="negative"><skipped/></testcase></testsuite>',
        '<testsuite><testcase classname="ValueTest" name="other"><failure type="AssertionError">expected 2</failure></testcase></testsuite>',
        '<testsuite><testcase classname="ValueTest" name="negative"><failure type="AssertionError">unrelated assertion</failure></testcase></testsuite>',
        '<testsuite><testcase classname="ValueTest" name="negative"><failure type="TypeError">expected 2</failure></testcase></testsuite>',
        '<testsuite errors="1"><testcase classname="ValueTest" name="negative"><failure type="AssertionError">expected 2</failure></testcase></testsuite>',
        '<testsuite tests="0"/>',
        "not XML",
    ],
)
def test_invalid_evidence_never_qualifies_for_a_production_fix(tmp_path, local_executor, xml):
    result = run_check(tmp_path, command(tmp_path, xml, 1), tmp_path / "evidence", local_executor)
    assert not proves_failure(result, reproduction())


def test_compile_failure_without_a_report_is_not_bug_evidence(tmp_path, local_executor):
    check = Check(
        name="compile failure",
        argv=[sys.executable, "-c", "raise SyntaxError('compile failed')"],
        cwd=".",
        purpose="test",
        junit_glob="result.xml",
        timeout_seconds=10,
    )
    result = run_check(tmp_path, check, tmp_path / "evidence", local_executor)
    assert result["exit_code"] == 1
    assert not proves_failure(result, reproduction())


def test_old_report_cannot_make_a_new_command_pass(tmp_path, local_executor):
    (tmp_path / "result.xml").write_text(
        '<testsuite><testcase classname="ValueTest" name="negative"/></testsuite>'
    )
    check = Check(
        name="no test ran",
        argv=[sys.executable, "-c", "pass"],
        cwd=".",
        purpose="test",
        junit_glob="result.xml",
        timeout_seconds=10,
    )
    result = run_check(tmp_path, check, tmp_path / "evidence", local_executor)
    assert result["cases"] == []
    assert not proves_success(result, reproduction())
    assert not proves_success(result)


def test_passing_reproduction_requires_the_original_test_identity(tmp_path, local_executor):
    check = command(
        tmp_path, '<testsuite><testcase classname="ValueTest" name="different"/></testsuite>', 0
    )
    result = run_check(tmp_path, check, tmp_path / "evidence", local_executor)
    assert proves_success(result)
    assert not proves_success(result, reproduction())


def test_timeout_cannot_qualify_even_if_a_failure_report_exists(tmp_path, local_executor):
    check = command(
        tmp_path,
        '<testsuite><testcase classname="ValueTest" name="negative"><failure type="AssertionError">expected 2</failure></testcase></testsuite>',
        1,
    )
    script = Path(check.argv[-1])
    script.write_text(script.read_text().replace("sys.exit(1)", "import time\ntime.sleep(5)"))
    check = check.model_copy(update={"timeout_seconds": 1})
    result = run_check(tmp_path, check, tmp_path / "evidence", local_executor)
    assert result["timed_out"]
    assert not proves_failure(result, reproduction())
