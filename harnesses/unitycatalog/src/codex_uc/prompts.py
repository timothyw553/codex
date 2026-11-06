"""Small instructions for each phase. Large evidence stays in local artifacts."""

from pathlib import Path


def design_prompt(store, discussion: str) -> str:
    notes = Path(__file__).with_name("unitycatalog.md")
    return f"""Discuss and propose a Unity Catalog design. Do not implement it.
Read the task at {store.root / "task.txt"}, the source at {store.state.worktree},
the repository's instructions and CODE_REVIEW.md, and the history lessons at {notes}.
Inspect architecture, callers, exact dependency interfaces and lifecycle contracts,
persisted state, wire behavior, authorization targets, compatibility, cancellation,
concurrency, failures, and important edge cases where relevant to this task.
Explain the recommended approach and any unresolved decisions in plain English.
Derive behavior from the requested task and actual contracts. Do not invent requirements.
List each exact file you need to change and the direct reason for changing it.
Choose a realistic line budget of at most 800 added and removed lines.
Use the project's existing test commands and fixtures. Include focused regression tests.
Test commands must produce JUnit XML, including an exact path or glob relative to their cwd.
Include formatting checks. All check commands must preserve source files.
Check commands run in a sandbox that can write only to their source worktree and a
private temporary directory. Keep build-tool caches in ignored target directories.
For sbt, inspect its boot, global, Ivy, and Coursier cache options before choosing argv.
Set network_access only for checks that need dependency downloads or local test servers.
The user approves this setting with the rest of the design.
Put all questions that need the user's decision in questions. An empty list means none.
If design.json already exists at {store.root}, read it before revising the design.
User's latest design discussion: {discussion or "Propose the initial design."}
"""


def worker_prompt(store, selected: Path | None = None) -> str:
    if selected:
        action = f"""Make only the repairs selected in {selected}.
Each selected finding has a failing test and verified evidence. Read those artifacts.
Keep every reproduction test byte-for-byte unchanged. Fix the shared cause once.
Do not review again, add new findings, or address suggestions outside this list.
This is the only repair batch. Stop if a fix needs a different design or broader scope.
"""
    else:
        action = f"Implement the task at {store.root / 'task.txt'} using the approved design."
    return f"""{action}
Read the approved design at {store.root / "design.json"} and CODE_REVIEW.md in the source.
The controller has verified the user's design approval. Work only within that design.
Inspect the real interfaces and nearby tests before editing. Use existing helpers.
Every changed file needs its listed reason. Make no unrelated cleanup or refactors.
Keep the diff within the approved line budget. Run formatting and focused tests where possible.
Your session has no network access. The controller runs the approved checks separately.
Report environment or sandbox failures. Never change expected behavior to hide them.
Do not weaken assertions or change expected behavior to make tests pass.
If you find a design problem, stop and return needs_design_change with a clear reason.
"""


def reproduction_prompt(store, issue: Path) -> str:
    return f"""Write the smallest regression test for the finding at {issue}.
Read the approved behavior and checks in {store.root / "design.json"}.
The production source is the unchanged candidate snapshot. Change only approved test files.
Call actual production code. Use real dependencies when dependency behavior is the claim.
Do not mock the behavior that the finding disputes. Do not add an unconditional failure.
The test must assert the approved contract independently of the implementation.
Return a check_index from the approved checks, the exact JUnit test class and name,
and the exact assertion failure text that proves this specific claim.
Do not change test commands or production code. The controller runs the check itself.
If you cannot test the claim, explain that honestly. Never manufacture evidence.
"""


def audit_prompt(store, issue: Path, evidence: Path) -> str:
    return f"""Judge whether the test proves the finding at {issue}.
Read the approved design at {store.root / "design.json"} and all artifacts at {evidence}.
Inspect the test patch and actual production code in this checkout.
Check that the baseline failed for the claimed reason, that the expectation comes from
the approved contract, and that the test calls the relevant real behavior.
Reject skipped tests, setup/compile failures, unconditional failures, circular assertions,
and mocks that only encode the reviewer's assumption about an external dependency.
Use base-comparison evidence when available to distinguish unrelated existing defects.
Then decide whether a production fix is necessary for this task and fits the agreed design.
Agreement between reviewers and severity labels are not evidence. Default to leaving code alone.
"""
