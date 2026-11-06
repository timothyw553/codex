"""Run the two review workflows independently, then select very few repairs."""

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .contracts import Review, Selection
from .storage import checkout, digest, load, save, snapshot

ADAPTER = """This is a local, review-only invocation for Unity Catalog.
Use the supplied source snapshot, base tree, design, and task. Do not discover a PR.
The controller supplies the local target and workspace. Use these instead of repository
discovery, GitHub account selection, workspace allocation, or publication setup.
Do not publish, send messages, make fixes, or use any auto-fix/loop mode.
The source checkout is outside your writable directory. Write artifacts only in your cwd.
Use native Codex collaboration tools instead of unavailable dispatch tools.
Native children inherit the snapshot binding through their developer instructions.
Dispatch with fork_turns none and no agent_type override so reviewers and gates get
only their assigned inputs. This replaces the hosted runner's metadata attachment.
Use read-only shell commands to inspect the supplied source and instruction files.
These replace the hosted source-reading tools. Do not run tests, read environment
variables, make network calls, or edit source during review.
Set each task_name to the original reviewer name with hyphens replaced by underscores.
For a replacement agent after a failure, append _retry1 to that same name.
Read each reviewer's actual instructions. Start every dispatch prompt with exactly
REVIEWER: <the original reviewer name>
Keep all reviewers, review passes, direction review, disproof, full finding blocks,
code excerpts, fix examples, attribution, and the execution ledger required by the workflow.
Adapt repository-specific checks to Unity Catalog. Do not merge this workflow with another.
Use the affected UC, Delta, and Iceberg contracts for protocol checks where relevant.
When instructions conflict, finish the detailed reviews for all direction verdicts.
Normalize NEEDS DISCUSSION to CONTESTED. Prefer this repo's PR title convention.
The workflow's original report is the report field in your result. Preserve it in full.
Return its findings separately without changing their IDs or claims.
List the reviewers and gates required by this workflow for this specific diff.
List completed coordinator steps in completed_gates. List only agent names in
completed_reviewers. These lists serve different purposes.
Do not mark gates_complete unless all required verification passes finished.
"""


def validate_review(name: str, report: Review, observed: set[str]) -> None:
    completed = set(report.completed_reviewers)
    if not completed <= observed or not report.gates_complete:
        raise ValueError(f"The {name} review did not complete its recorded reviewer steps.")
    required = set(report.required_reviewers)
    if (
        not required <= completed
        or required.intersection(report.failed_reviewers)
        or not set(report.required_gates) <= set(report.completed_gates)
    ):
        raise ValueError(f"The {name} review has unfinished required steps.")
    if len({finding.id for finding in report.findings}) != len(report.findings):
        raise ValueError(f"The {name} review repeated a finding ID.")
    if name == "uc":
        maintainers = {"maintainer-claude-reviewer", "maintainer-codex-reviewer"}
        primary = {
            "uc-domain-reviewer",
            "architecture-reviewer",
            "test-coverage-reviewer",
            "docs-reviewer",
        }
        if (
            not completed & maintainers
            or not completed & primary
            or "docs-reviewer" not in completed
        ):
            raise ValueError("The UC review did not reach its required coverage.")
        if any(f.severity in {"blocker", "should_fix"} for f in report.findings):
            if "disprove-reviewer" not in completed:
                raise ValueError("The UC review did not finish its disprove pass.")
    else:
        if len(set(report.required_reviewers)) < 2:
            raise ValueError("The second review did not finish its required reviewers.")
        if any(
            f.kind in {"correctness", "authorization", "security", "compatibility"}
            for f in report.findings
        ):
            if not report.required_gates:
                raise ValueError("The second review did not finish its disprove pass.")


def review_pair(store, runtime) -> dict[str, Review]:
    state = store.state
    candidate = state.candidate
    if not candidate or snapshot(Path(state.worktree)) != candidate:
        raise ValueError("The source changed after its review snapshot was saved.")

    def review_one(name: str) -> Review:
        source = store.artifacts / f"{name}-source"
        artifacts = store.artifacts / f"{name}-artifacts"
        artifacts.mkdir(exist_ok=True)
        if not source.exists():
            checkout(Path(state.repo), source, state.base, candidate)
        if snapshot(source) != candidate:
            raise ValueError(f"The saved {name} source changed. Start a new design revision.")
        policy = (
            source / ".github/omnigent/reviewer/REVIEW.md"
            if name == "uc"
            else Path(state.kernel_skill)
        )
        prompt = (
            ADAPTER
            + f"\nRead the complete workflow at {policy}.\n"
            + f"For the skill, derive PLUGIN_ROOT from {policy.parent.parent.parent}.\n"
            + "Use the running Python interpreter if a helper names an unavailable Python version.\n"
            + f"Source: {source}\nBase: {state.base}\nCandidate tree: {candidate}\n"
            + f"REPO_SLUG: unitycatalog/unitycatalog\nREPO_NAME: unitycatalog\nPARENT_REF: {state.base}\n"
            + f"WORKSPACE: {artifacts}\nLocal diff range: {state.base}\n"
            + f"Approved design: {store.root / 'design.json'}\n"
            + f"Task: {store.root / 'task.txt'}\n"
            + f"PR title and description: {store.artifacts / 'candidate-pr.json'}\n"
            + "UC review markers: <!-- CODEX_UC_REVIEW_START --> and <!-- CODEX_UC_REVIEW_END -->\n"
            + "Inspect the diff with git diff <base> against this checkout, including its index.\n"
        )
        label = f"{name}-{store.approval_hash()}"
        binding = (
            f"\nReview source: {source}\nPinned base: {state.base}\nCandidate tree: {candidate}\n"
            f"Task: {store.root / 'task.txt'}\nPR metadata: {store.artifacts / 'candidate-pr.json'}\n"
            f"Approved design: {store.root / 'design.json'}\n"
            "Read only this workflow's source and artifacts. Keep source unchanged.\n"
            "For a direction review, first derive the approach from the task and base alone. "
            "Record it before reading the candidate, PR description, or approved design.\n"
        )
        report = runtime.ask(
            artifacts,
            prompt,
            Review,
            label=label,
            writable=True,
            reviewers=True,
            review_context=binding,
        )
        if snapshot(source) != candidate:
            raise ValueError(f"The {name} reviewer changed its source snapshot.")
        validate_review(name, report, runtime.completed_reviewers.get(label, set()))
        return report

    # Neither workflow receives the other's output. Save both only after they finish.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(review_one, name) for name in ("uc", "kernel")}
        reports = {}
        errors = []
        for name, future in futures.items():
            try:
                reports[name] = future.result()
            except Exception as error:
                errors.append(f"{name}: {error}")
    for name, report in reports.items():
        save(store.artifacts / f"{name}-review.json", report.model_dump())
        (store.artifacts / f"{name}-review.md").write_text(report.report, encoding="utf-8")
    if errors:
        raise ValueError("Review is incomplete. " + "; ".join(errors))
    return reports


def finding_key(finding) -> str:
    # Run-local IDs and line numbers do not identify a defect.
    words = [finding.file, finding.trigger, finding.effect]
    return digest([re.sub(r"\W+", " ", word.lower()).strip() for word in words])


def select(store, runtime, reports: dict[str, Review]) -> list[dict]:
    findings = {
        f"{name}:{finding.id}": finding
        for name, report in reports.items()
        for finding in report.findings
    }
    save(
        store.artifacts / "findings.json",
        {key: item.model_dump() for key, item in findings.items()},
    )
    ledger_path = store.root / "decisions.json"
    ledger = load(ledger_path) if ledger_path.exists() else []
    prompt = f"""Select findings conservatively. Read {store.artifacts / "findings.json"},
{store.root / "design.json"}, and {ledger_path} if it exists.
Default to leaving code unchanged. A valid finding is not a reason to edit code.
Choose verify only for a necessary, small fix to a concrete user-visible defect within
the approved behavior. Most should-fix advice must remain unchanged. Leave naming advice,
equivalent rewrites, refactors, speculative checks, optional tests, and abstractions unchanged.
Group duplicate defects from both reports. Cover every finding reference exactly once.
Use behavior_index to identify the exact approved behavior that requires a proposed fix.
Map repeated concerns to previous_key even if the wording or finding IDs changed.
Previous rejected/deferred decisions stay closed. Do not create follow-up tasks or TODOs.
Choose at most three defects to verify. Empirical verification will decide eligibility.
"""
    selection = runtime.ask(Path(store.state.worktree), prompt, Selection, label="selection")
    refs = [ref for choice in selection.choices for ref in choice.refs]
    if sorted(refs) != sorted(findings):
        raise ValueError("The selection did not account for each finding exactly once.")
    closed = {key for entry in ledger if entry["action"] == "leave" for key in entry["keys"]}
    candidates = []
    for choice in selection.choices:
        items = [findings[ref] for ref in choice.refs]
        keys = sorted({finding_key(item) for item in items})
        eligible = (
            choice.action == "verify"
            and not closed.intersection(keys)
            and choice.previous_key is None
            and choice.behavior_index is not None
            and 0 <= choice.behavior_index < len(store.design().behavior)
            and all(
                item.kind in {"correctness", "authorization", "security", "compatibility"}
                for item in items
            )
            and all(item.severity in {"blocker", "should_fix"} for item in items)
        )
        entry = {
            **choice.model_dump(),
            "keys": keys,
            "action": "verify" if eligible else "leave",
            "findings": [item.model_dump() for item in items],
            "candidate": store.state.candidate,
        }
        ledger.append(entry)
        if eligible:
            candidates.append(entry)
    if len(candidates) > 3:
        raise ValueError("The selection exceeded the three-defect verification limit.")
    save(ledger_path, ledger)
    return candidates
