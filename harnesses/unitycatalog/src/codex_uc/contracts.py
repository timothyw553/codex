"""Typed results exchanged between the controller and each agent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )


class ScopeFile(Record):
    path: str
    reason: str
    role: Literal["production", "test", "documentation"]


class Check(Record):
    name: str
    argv: list[str] = Field(min_length=1, max_length=40)
    cwd: str
    purpose: Literal["test", "format"]
    junit_glob: str | None
    timeout_seconds: int = Field(ge=1, le=7200)
    network_access: bool = Field(
        default=False,
        description="Needed for local test servers or dependency downloads. Included in design approval.",
    )


class Design(Record):
    summary: str
    behavior: list[str] = Field(min_length=1, max_length=20)
    architecture: str
    interfaces: list[str] = Field(max_length=20)
    edge_cases: list[str] = Field(max_length=20)
    dependencies: list[str] = Field(max_length=20)
    change_budget: int = Field(ge=1, le=800)
    files: list[ScopeFile] = Field(min_length=1, max_length=30)
    checks: list[Check] = Field(min_length=1, max_length=12)
    non_goals: list[str] = Field(max_length=20)
    questions: list[str] = Field(max_length=12)


class WorkResult(Record):
    summary: str
    needs_design_change: bool
    reason: str


class Finding(Record):
    id: str
    kind: Literal[
        "correctness",
        "authorization",
        "security",
        "compatibility",
        "architecture",
        "tests",
        "docs",
        "style",
    ]
    severity: Literal["blocker", "should_fix", "nit", "contested"]
    file: str = Field(description="Exact repository-relative file path without a line number.")
    claim: str
    trigger: str
    effect: str
    suggested_fix: str
    reviewers: list[str] = Field(min_length=1)


class Review(Record):
    report: str
    findings: list[Finding] = Field(max_length=60)
    completed_reviewers: list[str]
    failed_reviewers: list[str]
    required_reviewers: list[str] = Field(min_length=1)
    required_gates: list[str] = Field(
        description="Required verification steps, including steps run by the coordinator."
    )
    completed_gates: list[str] = Field(
        description="Verification steps that finished, using their required_gates names. These are separate from reviewer agents."
    )
    gates_complete: bool
    reviewed_repositories: list[str] = Field(default_factory=list)


class Choice(Record):
    refs: list[str] = Field(min_length=1)
    action: Literal["leave", "verify"]
    reason: str
    behavior_index: int | None
    previous_key: str | None


class Selection(Record):
    choices: list[Choice] = Field(max_length=120)


class Reproduction(Record):
    check_index: int
    test_class: str
    test_name: str
    failure_text: str
    explanation: str


class EvidenceVerdict(Record):
    valid: bool
    necessary: bool
    within_design: bool
    reason: str


class Presentation(Record):
    paragraphs: list[str] = Field(min_length=1, max_length=60)


class Rewrite(Record):
    paragraphs: list[str] = Field(min_length=1, max_length=60)
    facts_preserved: bool


class MeaningCheck(Record):
    same_meaning: bool
    clearer_or_equal: bool
    reason: str


class PullRequest(Record):
    title: str
    body: str


class RepositoryTarget(Record):
    path: str
    base: str | None = None


class Repository(Record):
    name: str
    path: str
    base: str
    target: str


class RunState(Record):
    version: Literal[1] = 1
    repo: str
    worktree: str
    base: str
    task: str
    kernel_skill: str
    repositories: list[Repository] = Field(default_factory=list)
    model: str | None = None
    codex_bin: str | None = None
    phase: str = "design"
    approved_design: str | None = None
    candidate: str | None = None
    repair_batches: int = 0
    message: str = ""
