"""Reproduce shortlisted claims before exposing them to the repair worker."""

import subprocess
from pathlib import Path

from .contracts import EvidenceVerdict, Reproduction
from .evidence import proves_failure, run_check
from .prompts import audit_prompt, reproduction_prompt
from .storage import checkout, git, load, save, snapshot


def verify_candidates(store, runtime, candidates: list[dict]) -> list[dict]:
    state = store.state
    selected = []
    for index, issue in enumerate(candidates):
        directory = store.artifacts / "evidence" / str(index)
        directory.mkdir(parents=True, exist_ok=True)
        issue_path = directory / "issue.json"
        save(issue_path, issue)
        result_path = directory / "decision.json"
        if result_path.exists():
            decision = load(result_path)
            if decision["selected"]:
                selected.append(decision["issue"])
            continue
        source = directory / "source"
        if not source.exists():
            checkout(Path(state.repo), source, state.base, state.candidate)
        try:
            reproduction = runtime.ask(
                source,
                reproduction_prompt(store, issue_path),
                Reproduction,
                label=f"reproduce-{state.candidate}-{index}",
                writable=True,
            )
            save(directory / "reproduction.json", reproduction.model_dump())
            test_tree = store.freeze(f"proof-{state.candidate}-{index}", source)
            paths = store.scope(state.candidate, test_tree, tests_only=True)
            if not paths:
                raise ValueError("The reproduction did not add or change an approved test.")
            if not 0 <= reproduction.check_index < len(store.design().checks):
                raise ValueError("The reproduction did not select an approved test command.")
            check = store.design().checks[reproduction.check_index]
            if check.purpose != "test":
                raise ValueError("A formatting command cannot prove a behavioral failure.")
            patch = directory / "test.patch"
            patch.write_text(git(source, "diff", "--binary", state.candidate, test_tree) + "\n")
            baseline = run_check(source, check, directory / "baseline", runtime.execute)
            if snapshot(source) != test_tree:
                raise ValueError("The reproduction command changed source files.")
            if not proves_failure(baseline, reproduction):
                raise ValueError("A fresh test report did not prove the claimed assertion failure.")
            base_source = directory / "base-source"
            if not base_source.exists():
                checkout(Path(state.repo), base_source, state.base)
            try:
                git(base_source, "apply", "--check", str(patch))
            except subprocess.CalledProcessError:
                save(
                    directory / "base-comparison.json",
                    {"available": False, "reason": "The test patch does not apply to the base."},
                )
            else:
                git(base_source, "apply", str(patch))
                run_check(base_source, check, directory / "base-comparison", runtime.execute)
            verdict = runtime.ask(
                source,
                audit_prompt(store, issue_path, directory),
                EvidenceVerdict,
                label=f"evidence-audit-{state.candidate}-{index}",
            )
            save(directory / "audit.json", verdict.model_dump())
            if not (verdict.valid and verdict.necessary and verdict.within_design):
                raise ValueError(verdict.reason)
            proven = {
                **issue,
                "test_patch": str(patch),
                "test_tree": test_tree,
                "test_paths": paths,
                "reproduction": reproduction.model_dump(),
                "baseline": str(directory / "baseline" / "result.json"),
                "candidate": state.candidate,
            }
            selected.append(proven)
            save(result_path, {"selected": True, "issue": proven})
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            save(result_path, {"selected": False, "reason": str(error), "issue": issue})
    ledger_path = store.root / "decisions.json"
    ledger = load(ledger_path)
    selected_keys = {key for issue in selected for key in issue["keys"]}
    for entry in ledger:
        if entry["candidate"] == state.candidate and entry["action"] == "verify":
            entry["action"] = "selected" if selected_keys.intersection(entry["keys"]) else "leave"
            entry["evidence_status"] = "verified" if entry["action"] == "selected" else "unverified"
    save(ledger_path, ledger)
    save(store.artifacts / "selected.json", selected)
    return selected
