import io
import sys
from pathlib import Path

import pytest

from codex_uc import cli
from codex_uc.contracts import WorkResult
from codex_uc.storage import Store, git


@pytest.mark.parametrize("explicit_start", [False, True])
@pytest.mark.parametrize("explicit_repo", [False, True])
@pytest.mark.parametrize("task_in_argv", [False, True])
def test_start_uses_the_current_repository_or_an_explicit_path(
    store, runtime, tmp_path, monkeypatch, capsys, explicit_start, explicit_repo, task_in_argv
):
    repo = Path(store.state.repo)
    monkeypatch.chdir(tmp_path if explicit_repo else repo / "tests")
    argv = [
        "--base",
        store.state.base,
        "--kernel-review",
        store.state.kernel_skill,
        "--state-dir",
        str(tmp_path / "runs"),
    ]
    if task_in_argv:
        argv.insert(0, store.state.task)
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(store.state.task + "\n"))
    if explicit_repo:
        argv = ["--repo", str(repo), *argv]
    if explicit_start:
        argv = ["start", *argv]
    monkeypatch.setattr(sys, "argv", ["codex-uc", *argv])
    monkeypatch.setattr(cli, "Runtime", lambda active_store: runtime)

    assert cli.main() == 0

    roots = list((tmp_path / "runs").iterdir())
    assert len(roots) == 1
    started = Store(roots[0])
    assert started.state.repo == str(repo)
    assert started.state.task == store.state.task
    assert started.state.phase == "design"
    assert Path(started.state.worktree) != repo
    assert git(repo, "status", "--porcelain") == ""
    assert not any(call[1] == WorkResult for call in runtime.calls)
    assert "Design hash:" in capsys.readouterr().out


def test_bare_command_prompts_for_a_task_before_starting_design(
    store, runtime, tmp_path, monkeypatch, capsys
):
    repo = Path(store.state.repo)
    git(repo, "remote", "add", "origin", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["codex-uc"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n  \n" + store.state.task + "\n"))
    monkeypatch.setattr(cli, "find_kernel_skill", lambda explicit: Path(store.state.kernel_skill))
    monkeypatch.setattr(cli, "Runtime", lambda active_store: runtime)

    assert cli.main() == 0

    roots = list((tmp_path / "state" / "codex-uc").iterdir())
    assert len(roots) == 1
    started = Store(roots[0])
    assert started.state.repo == str(repo)
    assert started.state.task == store.state.task
    assert started.state.phase == "design"
    assert started.state.approved_design is None
    assert git(repo, "status", "--porcelain") == ""
    assert not any(call[1] == WorkResult for call in runtime.calls)
    output = capsys.readouterr().out
    assert output.count("What would you like to work on?") == 3
    assert "Design hash:" in output


@pytest.mark.parametrize(
    "error,exit_code,message",
    [
        (EOFError, 1, "No task entered."),
        (KeyboardInterrupt, 130, "Stopped."),
    ],
)
def test_cancelling_the_task_prompt_creates_no_run(monkeypatch, capsys, error, exit_code, message):
    def cancel(prompt):
        raise error

    monkeypatch.setattr(sys, "argv", ["codex-uc"])
    monkeypatch.setattr("builtins.input", cancel)
    monkeypatch.setattr(cli, "start", lambda args: pytest.fail("No run should be created."))

    assert cli.main() == exit_code
    output = capsys.readouterr().err
    assert message in output
    assert "saved" not in output


@pytest.mark.parametrize(
    "argv",
    [
        ["discuss", "saved-run", "Keep the existing API."],
        ["approve", "saved-run", "--design", "design-hash"],
        ["show", "saved-run"],
        ["run", "saved-run"],
        ["status", "saved-run"],
    ],
)
def test_saved_run_commands_keep_their_meaning(argv):
    args = cli.parse_args(argv)
    assert args.command == argv[0]
    assert args.run == Path("saved-run")


def test_help_explains_the_short_command(capsys):
    with pytest.raises(SystemExit) as result:
        cli.parse_args(["--help"])
    assert result.value.code == 0
    assert 'codex-uc "Describe the change"' in capsys.readouterr().out
