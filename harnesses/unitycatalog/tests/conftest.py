import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from openai_codex.generated.v2_all import CommandExecResponse

from codex_uc.contracts import (
    Check,
    Choice,
    Design,
    EvidenceVerdict,
    Finding,
    PullRequest,
    Reproduction,
    Review,
    Rewrite,
    RunState,
    ScopeFile,
    Selection,
    WorkResult,
)
from codex_uc.storage import Store, checkout, git, load, save


def execute_local(worktree, check):
    """Use real local processes for fixtures; production uses the app-server sandbox."""
    try:
        result = subprocess.run(
            check.argv,
            cwd=worktree / check.cwd,
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
            check=False,
        )
        return CommandExecResponse(
            exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )
    except subprocess.TimeoutExpired as error:
        return CommandExecResponse(
            exit_code=124,
            stdout=(error.stdout or b"").decode(),
            stderr=(error.stderr or b"").decode(),
        )


@pytest.fixture
def local_executor():
    return execute_local


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_WORKBENCH_SESSION", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "value.py").write_text("def value(x):\n    return x\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_value.py").write_text(
        "from value import value\n\n\ndef test_positive():\n    assert value(2) == 2\n"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ruff_cache/\ntarget/\n")
    (repo / ".github/omnigent/reviewer").mkdir(parents=True)
    (repo / ".github/omnigent/reviewer/REVIEW.md").write_text("Run the repository reviewers.\n")
    (repo / ".github/pull_request_template.md").write_text(
        "**PR Checklist**\n\n- [ ] Add tests.\n\n**Description of changes**\n"
    )
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Test fixture",
    )
    root = tmp_path / "run"
    root.mkdir()
    worktree = root / "worktree"
    base = git(repo, "rev-parse", "HEAD")
    checkout(repo, worktree, base)
    skill = tmp_path / "plugin/skills/kernel-review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Run the separately installed review workflow.\n")
    state = RunState(
        repo=str(repo),
        worktree=str(worktree),
        base=base,
        task="Return the absolute value.",
        kernel_skill=str(skill),
    )
    save(root / "state.json", state.model_dump())
    (root / "task.txt").write_text(state.task)
    return Store(root)


@pytest.fixture
def design():
    return Design(
        summary="Return the absolute value of an integer.",
        behavior=["value(-2) returns 2; value(2) returns 2."],
        architecture="Keep the existing function and fix its result.",
        interfaces=["The function keeps its name and argument."],
        edge_cases=["Zero returns zero."],
        dependencies=["The function uses no external dependency."],
        change_budget=120,
        files=[
            ScopeFile(path="value.py", role="production", reason="Return the requested value."),
            ScopeFile(path="tests/test_value.py", role="test", reason="Check the negative input."),
        ],
        checks=[
            Check(
                name="regression tests",
                argv=[
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--junitxml=target/result.xml",
                    "tests/test_value.py",
                ],
                cwd=".",
                purpose="test",
                junit_glob="target/result.xml",
                timeout_seconds=30,
            ),
            Check(
                name="formatting",
                argv=[
                    sys.executable,
                    "-m",
                    "ruff",
                    "format",
                    "--check",
                    "value.py",
                    "tests/test_value.py",
                ],
                cwd=".",
                purpose="format",
                junit_glob=None,
                timeout_seconds=30,
            ),
        ],
        non_goals=["Do not add a new API."],
        questions=[],
    )


class FakeRuntime:
    """Record agent boundaries; use real source changes and test commands."""

    execute = staticmethod(execute_local)

    def __init__(self, store, design):
        self.store = store
        self.design = design
        self.calls = []
        self.completed_reviewers = {}
        self.barrier = threading.Barrier(2)
        self.tamper_test = False
        self.outside_scope = False
        self.audit_valid = True
        self.no_findings = False
        self.wrong_failure = False
        self.review_coverage = None

    def ask(
        self, cwd, prompt, contract, *, label, writable=False, reviewers=False, review_context=""
    ):
        self.calls.append((label, contract, prompt, cwd, writable))
        if contract == Design:
            assert not writable
            return self.design
        if contract == WorkResult:
            assert writable
            sources = list(self.store.sources(cwd).values())
            if label.startswith("implementation"):
                for source in sources[:-1]:
                    (source / "value.py").write_text("def value(x):\n    return abs(x)\n")
                (sources[-1] / "value.py").write_text(
                    "def value(x):\n    return x if x > 0 else 0\n"
                )
                if self.outside_scope:
                    (sources[-1] / "extra.py").write_text("unexpected = True\n")
            else:
                assert "uc-review" not in prompt and "kernel-review" not in prompt
                selected = load(self.store.artifacts / "selected.json")
                assert selected and all(issue["baseline"] for issue in selected)
                assert all(
                    finding["kind"] != "style"
                    for issue in selected
                    for finding in issue["findings"]
                )
                (sources[-1] / "value.py").write_text("def value(x):\n    return abs(x)\n")
                if self.tamper_test:
                    test = sources[-1] / "tests/test_value.py"
                    test.write_text(test.read_text().replace("value(-2) == 2", "value(-2) == 0"))
            return WorkResult(summary="Updated the result.", needs_design_change=False, reason="")
        if contract == Review:
            assert "Pinned base:" in review_context
            source = Path(re.search(r"(?m)^Source: (.+)$", prompt).group(1))
            assert self.store.snapshot(source) == self.store.state.candidate
            assert not (self.store.artifacts / "uc-review.json").exists()
            assert not (self.store.artifacts / "kernel-review.json").exists()
            self.barrier.wait(timeout=10)
            uc = label.startswith("uc-")
            names = (
                ["maintainer-codex-reviewer", "docs-reviewer", "disprove-reviewer"]
                if uc
                else ["api-reviewer", "shape-reviewer", "claim-checker"]
            )
            self.completed_reviewers[label] = set(names)
            target = list(self.store.sources(source))[-1]
            prefix = target + "/" if self.store.state.repositories else ""
            defect = Finding(
                id="Blocker1" if uc else "B1",
                kind="correctness",
                severity="blocker",
                file=prefix + "value.py",
                claim="Negative input returns zero.",
                trigger="value(-2)",
                effect="The caller gets 0 instead of 2.",
                suggested_fix="Return the absolute value.",
                reviewers=[names[0]],
            )
            nit = Finding(
                id="N1",
                kind="style",
                severity="nit",
                file=prefix + "value.py",
                claim="Rename x.",
                trigger="Read the argument.",
                effect="The name is short.",
                suggested_fix="Choose a longer name.",
                reviewers=[names[1]],
            )
            findings = [] if self.no_findings else ([defect] if uc else [defect, nit])
            return Review(
                report="Complete original report, including code and attribution.",
                findings=findings,
                completed_reviewers=names,
                failed_reviewers=[],
                required_reviewers=names[:2],
                required_gates=names[2:],
                completed_gates=names[2:],
                gates_complete=True,
                reviewed_repositories=self.review_coverage
                if self.review_coverage is not None
                else list(self.store.sources(source)),
            )
        if contract == Selection:
            if self.no_findings:
                return Selection(choices=[])
            return Selection(
                choices=[
                    Choice(
                        refs=["uc:Blocker1", "kernel:B1"],
                        action="verify",
                        reason="The requested negative input still gives the wrong result.",
                        behavior_index=0,
                        previous_key=None,
                    ),
                    Choice(
                        refs=["kernel:N1"],
                        action="leave",
                        reason="The rename is optional.",
                        behavior_index=None,
                        previous_key=None,
                    ),
                ]
            )
        if contract == Reproduction:
            source = list(self.store.sources(cwd).values())[-1]
            test = source / "tests/test_value.py"
            test.write_text(
                test.read_text() + "\n\ndef test_negative():\n    assert value(-2) == 2\n"
            )
            return Reproduction(
                check_index=next(
                    index
                    for index, check in enumerate(self.design.checks)
                    if check.purpose == "test" and (cwd / check.cwd).resolve() == source.resolve()
                ),
                test_class="tests.test_value",
                test_name="test_negative",
                failure_text="unrelated failure" if self.wrong_failure else "assert 0 == 2",
                explanation="The test calls the function with the input in the approved behavior.",
            )
        if contract == EvidenceVerdict:
            assert not writable
            return EvidenceVerdict(
                valid=self.audit_valid,
                necessary=True,
                within_design=True,
                reason="The test calls the function and checks the approved result.",
            )
        if contract == PullRequest:
            evidence = (
                "Tests are pending."
                if label.startswith("candidate-pr-")
                else "The regression tests passed."
            )
            return PullRequest(
                title="[Server] Return the absolute value",
                body="**PR Checklist**\n\n- [x] Add tests.\n\n**Description of changes**\n\nNegative inputs returned the wrong result. Return the absolute value. "
                + evidence,
            )
        if contract == Rewrite:
            paragraphs = json.loads(prompt.splitlines()[-1])["paragraphs"]
            return Rewrite(paragraphs=paragraphs, facts_preserved=True)
        raise AssertionError(contract)


@pytest.fixture
def runtime(store, design):
    return FakeRuntime(store, design)


@pytest.fixture
def workspace_design(design):
    def qualify(names):
        return design.model_copy(
            update={
                "files": [
                    entry.model_copy(update={"path": name + "/" + entry.path})
                    for name in names
                    for entry in design.files
                ],
                "checks": [
                    check.model_copy(
                        update={
                            "name": name + " " + check.name,
                            "cwd": name if check.cwd == "." else name + "/" + check.cwd,
                        }
                    )
                    for name in names
                    for check in design.checks
                ],
            }
        )

    return qualify


@pytest.fixture
def workspace_runtime(workspace_design):
    def create(store):
        return FakeRuntime(store, workspace_design(list(store.sources())))

    return create
