import sys
from pathlib import Path

import pytest

from codex_uc import cli
from codex_uc.contracts import WorkResult
from codex_uc.storage import Store, git


@pytest.mark.parametrize("explicit_start", [False, True])
@pytest.mark.parametrize("explicit_repo", [False, True])
def test_start_uses_the_current_repository_or_an_explicit_path(
    store, runtime, tmp_path, monkeypatch, capsys, explicit_start, explicit_repo
):
    repo = Path(store.state.repo)
    monkeypatch.chdir(tmp_path if explicit_repo else repo / "tests")
    argv = [
        store.state.task,
        "--base",
        store.state.base,
        "--kernel-review",
        store.state.kernel_skill,
        "--state-dir",
        str(tmp_path / "runs"),
    ]
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
