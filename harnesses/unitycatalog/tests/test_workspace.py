import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_uc import cli
from codex_uc.contracts import Check, RepositoryTarget, WorkResult
from codex_uc.runtime import Runtime
from codex_uc.storage import Store, git, load, save
from codex_uc.workflow import Workflow


@pytest.fixture
def targets(store, tmp_path):
    delta = tmp_path / "delta"
    git(Path(store.state.repo), "clone", store.state.repo, str(delta))
    return [
        RepositoryTarget(path=store.state.repo, base="main"),
        RepositoryTarget(path=str(delta), base="main"),
    ]


@pytest.fixture
def workspace(store, targets, tmp_path):
    return cli.start(
        SimpleNamespace(
            repo=Path(store.state.repo),
            repositories=targets,
            base=None,
            task="Return the absolute value in both repositories.",
            kernel_review=store.state.kernel_skill,
            model=None,
            codex_bin=None,
            state_dir=tmp_path / "runs",
        )
    )


def approved(workspace, workspace_runtime):
    runtime = workspace_runtime(workspace)
    workflow = Workflow(workspace, runtime, announce=lambda message: None)
    workflow.propose()
    workflow.approve(workspace.approval_hash())
    return workflow, runtime


def test_one_workflow_checks_and_repairs_both_repositories(workspace, workspace_runtime):
    workflow, runtime = approved(workspace, workspace_runtime)

    assert "Required checks passed" in workflow.run()

    assert workspace.state.phase == "ready"
    assert workspace.state.repair_batches == 1
    for name, source in workspace.sources().items():
        assert (source / "value.py").read_text() == "def value(x):\n    return abs(x)\n"
        assert (workspace.artifacts / "prs" / name / "pr.md").is_file()
    for repo in workspace.repositories:
        assert git(Path(repo.path), "status", "--porcelain") == ""
    reports = [load(workspace.artifacts / f"{name}-review.json") for name in ("uc", "kernel")]
    assert all(set(report["reviewed_repositories"]) == {"repo", "delta"} for report in reports)
    selected = load(workspace.artifacts / "selected.json")
    assert len(selected) == 1
    assert selected[0]["test_paths"] == ["delta/tests/test_value.py"]
    baseline = load(Path(selected[0]["baseline"]))
    after = load(workspace.artifacts / "after/0/result.json")
    assert baseline["exit_code"] == 1
    assert after["exit_code"] == 0
    assert baseline["command"] == after["command"]
    assert baseline["command"]["cwd"] == "delta"
    assert set(load(workspace.artifacts / "pr.json")["repositories"]) == {"repo", "delta"}
    assert len([call for call in runtime.calls if call[1] == WorkResult]) == 2
    count = len(runtime.calls)
    assert "Required checks passed" in Workflow(Store(workspace.root), runtime).run()
    assert len(runtime.calls) == count

    (workspace.sources()["delta"] / "value.py").write_text("def value(x):\n    return 0\n")
    with pytest.raises(ValueError, match="source changed"):
        workflow.run()


@pytest.mark.parametrize("name", ["repo", "delta"])
def test_snapshots_and_scope_include_each_repository(workspace, workspace_runtime, name):
    workflow, _ = approved(workspace, workspace_runtime)
    root = Path(workspace.state.worktree)
    before = workspace.freeze("before", root)
    (workspace.sources()[name] / "value.py").write_text("def value(x):\n    return abs(x)\n")
    after = workspace.freeze("after", root)

    assert before != after
    assert workspace.snapshot(root) == after
    assert workspace.scope(before, after) == [name + "/value.py"]
    (workspace.sources()[name] / "extra.py").write_text("unexpected = True\n")
    outside = workspace.freeze("outside", root)
    with pytest.raises(ValueError, match=name + "/extra.py"):
        workspace.scope(before, outside)
    assert workflow.store.state.approved_design is not None


def test_each_base_commit_is_bound_to_the_joint_approval(workspace, workspace_runtime):
    workflow, runtime = approved(workspace, workspace_runtime)
    repositories = list(workspace.state.repositories)
    repo = repositories[1]
    tree = git(Path(repo.path), "rev-parse", repo.base + "^{tree}")
    other = git(
        Path(repo.path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        tree,
        "-p",
        repo.base,
        "-m",
        "Another target commit",
    )
    repositories[1] = repo.model_copy(update={"base": other})
    workspace.update(repositories=repositories)

    with pytest.raises(ValueError, match="approve"):
        workflow.run()

    assert not any(call[1] == WorkResult for call in runtime.calls)


def test_both_reviews_must_cover_the_second_repository(workspace, workspace_runtime):
    workflow, runtime = approved(workspace, workspace_runtime)
    runtime.review_coverage = ["repo"]

    with pytest.raises(ValueError, match="every selected repository"):
        workflow.run()

    assert workspace.state.phase == "reviewing"
    assert workspace.state.repair_batches == 0
    assert not (workspace.artifacts / "pr.md").exists()


def test_adding_a_repository_keeps_edits_and_requires_the_revised_design(
    store, runtime, targets, workspace_design
):
    workflow = Workflow(store, runtime)
    workflow.propose()
    previous = store.approval_hash()
    workflow.approve(previous)
    original_source = Path(store.state.worktree)
    (original_source / "value.py").write_text("def value(x):\n    return abs(x)\n")
    runtime.design = workspace_design(["repo", "delta"])

    workflow.propose("The task spans both repositories.", repositories=targets)

    assert not original_source.exists()
    assert store.state.approved_design is None
    assert store.state.phase == "design"
    assert store.approval_hash() != previous
    assert (store.root / "designs" / f"{previous}.json").is_file()
    assert (
        store.sources()["repo"] / "value.py"
    ).read_text() == "def value(x):\n    return abs(x)\n"
    assert (store.sources()["delta"] / "value.py").read_text() == "def value(x):\n    return x\n"
    for repo in store.repositories:
        assert git(Path(repo.path), "status", "--porcelain") == ""


def test_an_existing_worktree_path_refers_to_the_same_repository(
    store, runtime, targets, workspace_design
):
    runtime.design = workspace_design(["repo", "delta"])
    targets[0] = targets[0].model_copy(update={"path": store.state.worktree})
    workflow = Workflow(store, runtime)

    workflow.propose("Use both repositories.", repositories=targets)

    assert set(store.sources()) == {"repo", "delta"}
    saved = load(store.root / "design-request.json")
    assert saved["repositories"][0]["path"] == store.state.repo
    workflow.propose()
    assert store.state.phase == "design"


@pytest.mark.parametrize("after_checkout", [False, True])
def test_a_failed_repository_addition_preserves_edits_and_can_retry(
    store, runtime, targets, workspace_design, monkeypatch, after_checkout
):
    workflow = Workflow(store, runtime)
    workflow.propose()
    workflow.approve(store.approval_hash())
    source = Path(store.state.worktree)
    (source / "value.py").write_text("def value(x):\n    return abs(x)\n")
    runtime.design = workspace_design(["repo", "delta"])
    checkout = cli.checkout

    def fail(*args, **kwargs):
        if after_checkout:
            checkout(*args, **kwargs)
        raise OSError("Checkout failed.")

    monkeypatch.setattr(cli, "checkout", fail)
    with pytest.raises(OSError, match="Checkout failed"):
        workflow.propose("Keep both repositories.", repositories=targets)
    assert store.state.phase == "design_failed"
    assert store.state.approved_design is None
    assert Path(store.state.worktree) == source
    assert (source / "value.py").read_text() == "def value(x):\n    return abs(x)\n"
    monkeypatch.setattr(cli, "checkout", checkout)

    workflow.propose()

    assert store.state.phase == "design"
    assert set(store.sources()) == {"repo", "delta"}


def test_each_repository_uses_its_own_remote_default_branch(store, targets, tmp_path):
    for index, target in enumerate(targets):
        repo = Path(target.path)
        branch = "main" if index == 0 else "master"
        if index:
            git(repo, "switch", "-c", branch)
        origin = tmp_path / f"origin-{index}"
        git(repo, "clone", "--bare", str(repo), str(origin))
        git(origin, "symbolic-ref", "HEAD", "refs/heads/" + branch)
        if index:
            git(repo, "remote", "set-url", "origin", str(origin))
        else:
            git(repo, "remote", "add", "origin", str(origin))

        selected = cli.resolve_target(RepositoryTarget(path=str(repo)))

        assert selected.target == "refs/heads/" + branch
        assert selected.base == git(origin, "rev-parse", "HEAD")


def test_design_requires_test_commands_in_each_changed_repository(workspace, workspace_runtime):
    runtime = workspace_runtime(workspace)
    runtime.design = runtime.design.model_copy(update={"checks": runtime.design.checks[:2]})

    with pytest.raises(ValueError, match="tests for each changed repository: delta"):
        Workflow(workspace, runtime).propose()

    assert workspace.state.phase == "design_failed"


def test_a_saved_snapshot_cannot_be_replaced_with_another_repository_tree(
    workspace, workspace_runtime
):
    approved(workspace, workspace_runtime)
    revision = workspace.freeze("candidate", Path(workspace.state.worktree))
    path = workspace.root / "snapshots" / f"{revision}.json"
    trees = load(path)
    save(path, {**trees, "delta": "changed"})

    with pytest.raises(ValueError, match="snapshot changed"):
        workspace.checkout(workspace.root / "other", revision)


@pytest.mark.skipif(
    os.environ.get("CODEX_UC_LIVE_TESTS") != "1", reason="Needs the installed Codex model service"
)
def test_live_native_agent_can_change_both_repositories(workspace, workspace_runtime):
    approved(workspace, workspace_runtime)
    root = Path(workspace.state.worktree)
    result = Runtime(workspace).ask(
        root,
        "Implement this approved fixture change in both sibling Git worktrees. "
        "Read the approved design at "
        + str(workspace.root / "design.json")
        + ". Set repo/value.py and delta/value.py to exactly this code:\n"
        "def value(x):\n    return abs(x)\n"
        "Keep other files unchanged. Do not run tests, spawn agents, or use network tools. "
        "Return the requested work result.",
        WorkResult,
        label="live-workspace-implementation",
        writable=True,
    )

    assert result.needs_design_change is False
    revision = workspace.freeze("live-candidate", root)
    assert set(workspace.scope(workspace.state.base, revision)) == {
        "repo/value.py",
        "delta/value.py",
    }
    for source in workspace.sources().values():
        assert (source / "value.py").read_text() == "def value(x):\n    return abs(x)\n"
    for repo in workspace.repositories:
        assert git(Path(repo.path), "status", "--porcelain") == ""


@pytest.mark.skipif(
    os.environ.get("CODEX_UC_LIVE_TESTS") != "1", reason="Needs the installed Codex sandbox"
)
def test_live_check_can_use_both_repositories_but_cannot_change_controller_state(workspace):
    original = (workspace.root / "state.json").read_bytes()
    script = """
import errno
import json
from pathlib import Path
Path('command-output.txt').write_text('allowed')
Path('../delta/command-output.txt').write_text('allowed')
try:
    Path(%r).write_text('changed')
except OSError as error:
    if error.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
        raise
    protected = True
else:
    protected = False
print(json.dumps({'protected': protected}))
""" % str(workspace.root / "state.json")
    check = Check(
        name="workspace sandbox",
        argv=[sys.executable, "-c", script],
        cwd="repo",
        purpose="format",
        junit_glob=None,
        timeout_seconds=20,
    )

    result = Runtime(workspace).execute(Path(workspace.state.worktree), check)

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"protected": True}
    assert (workspace.root / "state.json").read_bytes() == original
    for source in workspace.sources().values():
        assert (source / "command-output.txt").read_text() == "allowed"
