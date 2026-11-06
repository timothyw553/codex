import pytest
from openai_codex.generated.v2_all import ThreadItem

from codex_uc.contracts import Choice, Finding, Review, Selection
from codex_uc.reviews import select, validate_review
from codex_uc.runtime import completed_reviewers
from codex_uc.storage import save, snapshot


def test_native_reviewer_completion_comes_from_runtime_events():
    events = [
        ThreadItem.model_validate(item)
        for item in [
            {
                "type": "subAgentActivity",
                "id": "start-a",
                "kind": "started",
                "agentPath": "/root/api_reviewer",
                "agentThreadId": "a",
            },
            {
                "type": "subAgentActivity",
                "id": "done-a",
                "kind": "completed",
                "agentPath": "/root/api_reviewer",
                "agentThreadId": "a",
            },
            {
                "type": "subAgentActivity",
                "id": "stop-b",
                "kind": "interrupted",
                "agentPath": "/root/shape_reviewer",
                "agentThreadId": "b",
            },
            {
                "type": "subAgentActivity",
                "id": "retry-b",
                "kind": "completed",
                "agentPath": "/root/shape_reviewer_retry1",
                "agentThreadId": "c",
            },
        ]
    ]
    assert completed_reviewers(events) == {"api-reviewer", "shape-reviewer"}


def test_legacy_reviewer_completion_requires_dispatch_and_finished_state():
    events = [
        ThreadItem.model_validate(item)
        for item in [
            {
                "type": "collabAgentToolCall",
                "id": "spawn",
                "tool": "spawnAgent",
                "status": "completed",
                "senderThreadId": "root",
                "receiverThreadIds": ["a"],
                "prompt": "REVIEWER: api-reviewer\nCheck the API.",
                "agentsStates": {},
            },
            {
                "type": "collabAgentToolCall",
                "id": "wait",
                "tool": "wait",
                "status": "completed",
                "senderThreadId": "root",
                "receiverThreadIds": ["a", "b"],
                "agentsStates": {"a": {"status": "completed"}, "b": {"status": "errored"}},
            },
        ]
    ]
    assert completed_reviewers(events) == {"api-reviewer"}


def test_model_claiming_review_completion_is_not_execution_evidence():
    report = Review(
        report="Done",
        findings=[],
        completed_reviewers=["api-reviewer", "shape-reviewer"],
        required_reviewers=["api-reviewer", "shape-reviewer"],
        required_gates=[],
        completed_gates=[],
        failed_reviewers=[],
        gates_complete=True,
    )
    with pytest.raises(ValueError, match="recorded reviewer"):
        validate_review("kernel", report, set())
    coordinator = report.model_copy(
        update={
            "required_gates": ["direction-review"],
            "completed_gates": ["direction-review"],
        }
    )
    validate_review("kernel", coordinator, set(report.completed_reviewers))
    with pytest.raises(ValueError, match="unfinished required"):
        validate_review(
            "kernel",
            report.model_copy(update={"required_gates": ["claim-checker"]}),
            set(report.completed_reviewers),
        )


def test_rejected_defect_stays_closed_when_its_finding_id_changes(store, design):
    save(store.root / "design.json", design.model_dump())
    store.update(candidate=snapshot(store.state.worktree))
    finding = Finding(
        id="B1",
        kind="correctness",
        severity="blocker",
        file="value.py",
        claim="Negative input returns zero.",
        trigger="value(-2)",
        effect="The caller gets the wrong result.",
        suggested_fix="Return its absolute value.",
        reviewers=["api-reviewer"],
    )
    report = Review(
        report="Full report.",
        findings=[finding],
        completed_reviewers=["api-reviewer", "shape-reviewer"],
        required_reviewers=["api-reviewer", "shape-reviewer"],
        required_gates=[],
        completed_gates=[],
        failed_reviewers=[],
        gates_complete=True,
    )

    class Selector:
        action = "leave"
        ref = "kernel:B1"

        def ask(self, *args, **kwargs):
            return Selection(
                choices=[
                    Choice(
                        refs=[self.ref],
                        action=self.action,
                        reason="Leave the code unchanged.",
                        behavior_index=0,
                        previous_key=None,
                    )
                ]
            )

    selector = Selector()
    assert select(store, selector, {"kernel": report}) == []
    selector.action = "verify"
    selector.ref = "kernel:B99"
    renamed = report.model_copy(update={"findings": [finding.model_copy(update={"id": "B99"})]})
    assert select(store, selector, {"kernel": renamed}) == []


def test_style_finding_cannot_become_a_production_repair(store, design):
    save(store.root / "design.json", design.model_dump())
    store.update(candidate=snapshot(store.state.worktree))
    finding = Finding(
        id="N1",
        kind="style",
        severity="should_fix",
        file="value.py",
        claim="Rename the variable.",
        trigger="Read x.",
        effect="The name is short.",
        suggested_fix="Use a longer name.",
        reviewers=["style-reviewer"],
    )
    report = Review(
        report="Full report.",
        findings=[finding],
        completed_reviewers=["style-reviewer"],
        required_reviewers=["style-reviewer"],
        required_gates=[],
        completed_gates=[],
        failed_reviewers=[],
        gates_complete=True,
    )

    class Selector:
        def ask(self, *args, **kwargs):
            return Selection(
                choices=[
                    Choice(
                        refs=["kernel:N1"],
                        action="verify",
                        reason="A longer name is helpful.",
                        behavior_index=0,
                        previous_key=None,
                    )
                ]
            )

    assert select(store, Selector(), {"kernel": report}) == []
