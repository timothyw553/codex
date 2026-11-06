from pathlib import Path

import pytest

from codex_uc.contracts import Design, Rewrite, WorkResult
from codex_uc.storage import Store, git, load, save, snapshot
from codex_uc.workflow import Workflow


def approve(store, runtime):
    workflow = Workflow(store, runtime, announce=lambda message: None)
    workflow.propose()
    workflow.approve(store.approval_hash())
    return workflow


def test_approval_binds_the_exact_design_and_blocks_writes(store, runtime):
    workflow = Workflow(store, runtime)
    workflow.propose()
    original = snapshot(store.state.worktree)
    with pytest.raises(ValueError, match="approve"):
        workflow.run()
    assert not any(call[1] == WorkResult for call in runtime.calls)
    assert snapshot(store.state.worktree) == original
    old_hash = store.approval_hash()
    data = store.design().model_dump()
    data["behavior"].append("Zero returns zero.")
    save(store.root / "design.json", data)
    with pytest.raises(ValueError, match="changed"):
        workflow.approve(old_hash)


@pytest.mark.parametrize("previous_design", [False, True])
@pytest.mark.parametrize("failed_contract", [Design, Rewrite])
def test_failed_design_keeps_the_discussion_and_blocks_approval_until_retry(
    store, runtime, monkeypatch, previous_design, failed_contract
):
    workflow = Workflow(store, runtime)
    if previous_design:
        workflow.propose()
        old_hash = store.approval_hash()
        workflow.approve(old_hash)
    original = snapshot(Path(store.state.worktree))
    ask = runtime.ask

    def fail(cwd, prompt, contract, **options):
        if contract == failed_contract:
            raise ValueError("The model request failed.")
        return ask(cwd, prompt, contract, **options)

    monkeypatch.setattr(runtime, "ask", fail)
    with pytest.raises(ValueError, match="model request failed"):
        workflow.propose("Keep the existing API and cover zero.")

    assert store.state.phase == "design_failed"
    assert "model request failed" in store.state.message
    assert store.state.approved_design is None
    with pytest.raises(ValueError, match="design"):
        workflow.approve("unapproved")
    with pytest.raises(ValueError, match="design"):
        workflow.run()
    assert not any(call[1] == WorkResult for call in runtime.calls)
    assert snapshot(Path(store.state.worktree)) == original
    if previous_design:
        assert (store.root / "designs" / f"{old_hash}.json").exists()

    monkeypatch.setattr(runtime, "ask", ask)
    workflow.propose()
    assert store.state.phase == "design"
    assert store.state.message == ""
    prompt = [call[2] for call in runtime.calls if call[1] == Design][-1]
    assert "Keep the existing API and cover zero." in prompt
    assert (store.root / "design.md").is_file()
    workflow.approve(store.approval_hash())


def test_complete_workflow_uses_real_failure_one_repair_and_same_test(store, runtime):
    workflow = approve(store, runtime)
    message = workflow.run()
    assert store.state.phase == "ready"
    assert store.state.repair_batches == 1
    assert "Required checks passed" in message
    assert (
        store.artifacts / "uc-review.md"
    ).read_text() == "Complete original report, including code and attribution."
    baseline = load(store.artifacts / "evidence/0/baseline/result.json")
    after = load(store.artifacts / "after/0/result.json")
    assert baseline["exit_code"] == 1
    assert after["exit_code"] == 0
    assert baseline["command"] == after["command"]
    assert [case["name"] for case in baseline["cases"] if case["failed"]] == ["test_negative"]
    assert (store.artifacts / "pr.md").exists()
    workers = [call for call in runtime.calls if call[1] == WorkResult]
    assert len(workers) == 2
    ledger = load(store.root / "decisions.json")
    assert [entry["action"] for entry in ledger] == ["selected", "leave"]
    count = len(runtime.calls)
    resumed = Workflow(Store(store.root), runtime)
    assert resumed.run() == message
    assert len(runtime.calls) == count
    assert git(store.state.repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("audit_valid,wrong_failure", [(False, False), (True, True)])
def test_unverified_claim_cannot_reach_repair_worker(store, runtime, audit_valid, wrong_failure):
    runtime.audit_valid = audit_valid
    runtime.wrong_failure = wrong_failure
    workflow = approve(store, runtime)
    workflow.run()
    assert store.state.repair_batches == 0
    assert load(store.artifacts / "selected.json") == []
    assert [call[0] for call in runtime.calls if call[1] == WorkResult] == [
        "implementation-" + store.approval_hash()
    ]
    assert load(store.root / "decisions.json")[0]["evidence_status"] == "unverified"


def test_scope_violation_stops_before_reviews(store, runtime):
    runtime.outside_scope = True
    workflow = approve(store, runtime)
    with pytest.raises(ValueError, match="extra.py"):
        workflow.run()
    assert store.state.phase == "needs_discussion"
    assert not any(call[0].startswith("uc-") for call in runtime.calls)


def test_repair_cannot_weaken_the_original_regression_test(store, runtime):
    runtime.tamper_test = True
    workflow = approve(store, runtime)
    with pytest.raises(ValueError, match="reproduction test"):
        workflow.run()
    assert store.state.phase == "needs_discussion"
    assert store.state.repair_batches == 1
    assert not (store.artifacts / "pr.md").exists()
    assert "return abs" in (store.artifacts / "repair-source/value.py").read_text()
    assert "return x if" in (Path(store.state.worktree) / "value.py").read_text()


def test_source_changes_after_checks_invalidate_readiness(store, runtime):
    workflow = approve(store, runtime)
    workflow.run()
    (Path(store.state.worktree) / "value.py").write_text("def value(x):\n    return 0\n")
    with pytest.raises(ValueError, match="source changed"):
        workflow.run()
    assert store.state.phase == "needs_discussion"


def test_discussion_revokes_approval_and_preserves_reports(store, runtime):
    workflow = approve(store, runtime)
    workflow.run()
    original_artifacts = store.artifacts
    original_report = (original_artifacts / "uc-review.md").read_bytes()
    runtime.design = runtime.design.model_copy(
        update={"architecture": "Keep the same API. Handle each input directly."}
    )
    workflow.propose("Explain the implementation again.")
    assert store.state.approved_design is None
    assert store.state.phase == "design"
    assert (original_artifacts / "uc-review.md").read_bytes() == original_report
    assert store.artifacts != original_artifacts
