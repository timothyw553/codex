import io
import sys
from pathlib import Path

import pytest

from codex_uc import cli
from codex_uc.contracts import WorkResult
from codex_uc.storage import Store, git


@pytest.mark.parametrize("location", ["explicit", "current", "home"])
@pytest.mark.parametrize("task_in_argv", [False, True])
def test_start_uses_the_current_repository_or_an_explicit_path(
    store, runtime, tmp_path, monkeypatch, capsys, location, task_in_argv
):
    repo = Path(store.state.repo)
    test_home = tmp_path / "home"
    test_home.mkdir()
    default_repo = repo if location == "home" else Path(store.state.worktree)
    (test_home / "unitycatalog").symlink_to(default_repo, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: test_home)
    monkeypatch.chdir(repo / "tests" if location == "current" else test_home)
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
    if location == "explicit":
        argv = ["--repo", str(repo), *argv]
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


@pytest.mark.parametrize("argv", [[], ["chat"], ["Something feels wrong with listing tables."]])
def test_default_command_opens_chat_without_starting_design(monkeypatch, argv):
    launched = []
    monkeypatch.setattr(sys, "argv", ["codex-uc", *argv])
    monkeypatch.setattr(cli, "launch_chat", lambda args: launched.append(args) or 0, raising=False)
    monkeypatch.setattr(cli, "start", lambda args: pytest.fail("A chat is not yet a task."))

    assert cli.main() == 0

    assert len(launched) == 1
    assert launched[0].command == "chat"
    assert launched[0].repo is None
    assert launched[0].task == (argv[0] if argv and argv[0] != "chat" else None)


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

    monkeypatch.setattr(sys, "argv", ["codex-uc", "start"])
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
