"""Enforce the approved phase order and stop after one repair batch."""

import hmac
import re
from pathlib import Path

from .contracts import Design, PullRequest, RepositoryTarget, Reproduction, Review, WorkResult
from .evidence import proves_success, run_check
from .prompts import design_prompt, worker_prompt
from .proofs import verify_candidates
from .reviews import review_pair, select
from .storage import NeedsDiscussion, git, load, save
from .writing import present


class Workflow:
    def __init__(self, store, runtime, announce=print):
        self.store = store
        self.runtime = runtime
        self.announce = announce

    def propose(
        self, discussion: str = "", repositories: list[RepositoryTarget] | None = None
    ) -> str:
        store = self.store
        previous = store.root / "design.json"
        if previous.exists():
            save(store.root / "designs" / f"{store.approval_hash()}.json", load(previous))
        store.update(approved_design=None, phase="design_generating", candidate=None, message="")
        try:
            request = store.root / "design-request.json"
            saved = load(request) if request.exists() else {}
            if not discussion and request.exists():
                discussion = saved["discussion"]
            if repositories is None:
                repositories = [
                    RepositoryTarget.model_validate(item) for item in saved.get("repositories", [])
                ]
            repositories = [store.canonical_target(target) for target in repositories]
            save(
                request,
                {
                    "discussion": discussion,
                    "repositories": [repo.model_dump() for repo in repositories],
                },
            )
            if repositories:
                from .cli import include_repositories

                include_repositories(store, repositories)
            design = self.runtime.ask(
                Path(store.state.worktree),
                design_prompt(store, discussion),
                Design,
                label="design-" + str(len(list((store.root / "agents").glob("design-*")))),
            )
            store.validate_design(design)
            save(previous, design.model_dump())
            text = self.show_design()
        except (Exception, KeyboardInterrupt) as error:
            store.update(
                phase="design_failed",
                message="The design request did not finish. "
                + (str(error) or type(error).__name__),
            )
            raise
        store.update(phase="design", message="")
        return text

    def show_design(self) -> str:
        design = self.store.design()
        paragraphs = [design.summary, design.architecture]
        if self.store.state.repositories:
            paragraphs.append(
                "Repositories and target commits:\n\n"
                + self.store.source_context(Path(self.store.state.worktree))
            )
        for label, entries in (
            ("Required behavior", design.behavior),
            ("Interfaces", design.interfaces),
            ("Edge cases", design.edge_cases),
            ("Dependencies checked", design.dependencies),
            ("Outside this change", design.non_goals),
            ("Questions to resolve", design.questions),
        ):
            if entries:
                paragraphs.append(label + ":\n\n" + "\n".join(f"- {entry}" for entry in entries))
        paragraphs.append(
            "Files:\n\n" + "\n".join(f"- `{file.path}`: {file.reason}" for file in design.files)
        )
        paragraphs.append(f"Change limit: {design.change_budget} added and removed lines.")
        paragraphs.append(
            "Checks:\n\n"
            + "\n".join(
                f"- `{check.argv}` in `{check.cwd}`; {check.name}. Network access: {'enabled' if check.network_access else 'disabled'}."
                for check in design.checks
            )
        )
        text = present(self.runtime, paragraphs, Path(self.store.state.worktree))
        (self.store.root / "design.md").write_text(text + "\n", encoding="utf-8")
        return text

    def approve(self, expected_hash: str) -> None:
        store = self.store
        design = store.require_design()
        if store.state.phase != "design":
            raise ValueError("Only a design awaiting approval can be approved.")
        if not hmac.compare_digest(expected_hash, store.approval_hash()):
            raise ValueError("The design changed. Read it again before approving it.")
        if design.questions:
            raise ValueError("Resolve the design questions before approving it.")
        store.validate_design(design)
        store.update(approved_design=expected_hash, phase="approved", repair_batches=0, message="")

    def run(self) -> str:
        self.store.require_approval()
        try:
            return self._run()
        except Exception as error:
            changes = {"message": str(error)}
            if isinstance(error, NeedsDiscussion):
                changes["phase"] = "needs_discussion"
            self.store.update(**changes)
            raise

    def _run(self) -> str:
        store = self.store
        design = store.design()
        worktree = Path(store.state.worktree)
        if store.state.phase == "needs_discussion":
            raise ValueError(store.state.message)
        if store.state.phase == "ready":
            final = load(store.artifacts / "final.json")
            if store.snapshot(worktree) != final["tree"]:
                raise NeedsDiscussion(
                    "The source changed after the checks. Review the design again."
                )
            return self.ready_message()
        if store.state.phase in {"approved", "implementing"}:
            self.announce("I am implementing the approved design.")
            store.update(phase="implementing")
            result = self.runtime.ask(
                worktree,
                worker_prompt(store) + "\nDesign approval: " + store.approval_hash(),
                WorkResult,
                label="implementation-" + store.approval_hash(),
                writable=True,
            )
            if result.needs_design_change:
                raise NeedsDiscussion(result.reason)
            tree = store.freeze("candidate", worktree)
            store.scope(store.state.base, tree)
            if tree == store.base_tree:
                raise ValueError("Implementation produced no source changes.")
            store.update(candidate=tree, phase="candidate_draft")
        if store.state.phase == "candidate_draft":
            self.announce("I am preparing the PR draft for the reviewers.")
            self.draft_pr(candidate=True)
            store.update(phase="reviewing")
        if store.state.phase == "reviewing":
            self.announce("I am running both review workflows on the saved patch.")
            review_pair(store, self.runtime)
            store.update(phase="selecting")
        if store.state.phase == "selecting":
            self.announce("I am selecting only necessary bug claims for testing.")
            reports = {
                name: Review.model_validate(load(store.artifacts / f"{name}-review.json"))
                for name in ("uc", "kernel")
            }
            candidates = select(store, self.runtime, reports)
            save(store.artifacts / "candidates.json", candidates)
            store.update(phase="verifying")
        if store.state.phase == "verifying":
            self.announce("I am testing the selected claims on the unchanged patch.")
            candidates = load(store.artifacts / "candidates.json")
            selected = verify_candidates(store, self.runtime, candidates)
            store.update(phase="preparing_repair" if selected else "checking")
            if not selected:
                save(
                    store.artifacts / "final.json",
                    {"source": str(worktree), "tree": store.state.candidate},
                )
        if store.state.phase == "preparing_repair":
            if store.state.repair_batches:
                raise ValueError("The repair batch is already used. Discuss the remaining defect.")
            source = store.artifacts / "repair-source"
            if source.exists():
                raise ValueError(
                    f"Repair setup was interrupted. Inspect {source} before continuing."
                )
            store.checkout(source, store.state.candidate)
            for issue in load(store.artifacts / "selected.json"):
                git(source, "apply", "--check", issue["test_patch"])
                git(source, "apply", issue["test_patch"])
            test_tree = store.freeze("repair-tests", source)
            save(store.artifacts / "repair.json", {"source": str(source), "test_tree": test_tree})
            store.update(phase="repairing", repair_batches=1)
        if store.state.phase == "repairing":
            self.announce("I am making one repair batch for the verified findings.")
            repair = load(store.artifacts / "repair.json")
            source = Path(repair["source"])
            result = self.runtime.ask(
                source,
                worker_prompt(store, store.artifacts / "selected.json", source=source)
                + "\nDesign approval: "
                + store.approval_hash(),
                WorkResult,
                label="repair-" + store.approval_hash(),
                writable=True,
            )
            if result.needs_design_change:
                raise NeedsDiscussion(result.reason)
            tree = store.freeze("repaired", source)
            paths = store.scope(repair["test_tree"], tree)
            frozen = {
                path
                for issue in load(store.artifacts / "selected.json")
                for path in issue["test_paths"]
            }
            if frozen.intersection(paths):
                raise NeedsDiscussion(
                    "The repair changed a reproduction test. Its evidence is no longer valid."
                )
            _, lines = store.changes(repair["test_tree"], tree)
            if lines > 80:
                raise NeedsDiscussion("The repair exceeds 80 changed lines. Discuss a smaller fix.")
            store.scope(store.state.base, tree)
            save(store.artifacts / "final.json", {"source": str(source), "tree": tree})
            store.update(phase="checking")
        if store.state.phase == "checking":
            self.announce("I am checking the final patch and the original regression tests.")
            final = load(store.artifacts / "final.json")
            source = Path(final["source"])
            if store.snapshot(source) != final["tree"]:
                raise NeedsDiscussion("The source changed before the final checks.")
            for index, issue in enumerate(load(store.artifacts / "selected.json")):
                reproduction = Reproduction.model_validate(issue["reproduction"])
                result = run_check(
                    source,
                    design.checks[reproduction.check_index],
                    store.artifacts / "after" / str(index),
                    self.runtime.execute,
                )
                if not proves_success(result, reproduction):
                    raise ValueError("The original reproduction did not pass after the repair.")
            for index, check in enumerate(design.checks):
                result = run_check(
                    source, check, store.artifacts / "checks" / str(index), self.runtime.execute
                )
                if not proves_success(result):
                    raise ValueError(f"The required check did not pass: {check.name}")
            if store.snapshot(source) != final["tree"]:
                raise NeedsDiscussion(
                    "A check changed source files. Inspect the diff before continuing."
                )
            if source != worktree:
                if store.snapshot(worktree) != store.state.candidate:
                    raise NeedsDiscussion(
                        "The original worktree changed while repairs were in progress."
                    )
                store.restore(worktree, final["tree"])
            store.update(phase="drafting")
        if store.state.phase == "drafting":
            self.draft_pr()
            if store.snapshot(worktree) != load(store.artifacts / "final.json")["tree"]:
                raise NeedsDiscussion("The source changed after the final checks.")
            store.update(phase="ready", message="The patch is ready for your review.")
        return self.ready_message()

    def draft_pr(self, *, candidate: bool = False) -> None:
        store = self.store
        worktree = Path(store.state.worktree)
        sources = store.sources(worktree)
        revision = (
            store.state.candidate if candidate else load(store.artifacts / "final.json")["tree"]
        )
        changed, _ = store.changes(store.state.base, revision)
        drafts = {}
        name = "candidate-pr" if candidate else "pr"
        for repo in store.repositories:
            if store.state.repositories and not any(
                path.startswith(repo.name + "/") for path in changed
            ):
                continue
            directory = (
                store.artifacts / "prs" / repo.name if store.state.repositories else store.artifacts
            )
            directory.mkdir(parents=True, exist_ok=True)
            self._draft_pr(sources[repo.name], repo.base, directory, candidate=candidate)
            drafts[repo.name] = load(directory / f"{name}.json")
        if store.state.repositories:
            save(store.artifacts / f"{name}.json", {"repositories": drafts})
            (store.artifacts / f"{name}.md").write_text(
                "\n\n".join(
                    f"## {repo}\n\n{draft['title']}\n\n{draft['body']}"
                    for repo, draft in drafts.items()
                )
                + "\n",
                encoding="utf-8",
            )

    def _draft_pr(self, worktree: Path, base: str, directory: Path, *, candidate: bool) -> None:
        store = self.store
        test_evidence = (
            "The controller has not run its checks yet. Mark them pending. Do not claim they passed."
            if candidate
            else f"Read the actual check results at {store.artifacts / 'checks'}. Claim only tests supported by those results."
        )
        draft = self.runtime.ask(
            worktree,
            f"""Write a PR title and description for this repository's diff against {base}.
Read the repository's current PR template and CODE_REVIEW.md. Preserve template sections.
Use the [Component] title prefix. Explain the problem, a concrete example, the exact fix,
compatibility, and tests in plain English for a student finishing high-school computer science.
Use the approved design at {store.root / "design.json"}.
The full task can span several repositories. Describe only this repository's changes.
Explain any dependency on its companion changes. Do not include private links or local paths.
Describe the current implementation. {test_evidence}
Keep private information out of all text. Return a draft only. Do not publish anything.
""",
            PullRequest,
            label=("candidate-pr-" if candidate else "pr-")
            + store.approval_hash()
            + ("-" + worktree.name if store.state.repositories else ""),
        )
        if not re.match(r"^\[[^\]\n]+\] \S", draft.title):
            raise ValueError("The PR title needs the repository's [Component] prefix.")
        template = worktree / ".github/pull_request_template.md"
        if template.exists():
            headings = re.findall(r"(?m)^(?:\*\*[^\n]+\*\*|#{1,6} .+)$", template.read_text())
            if any(heading not in draft.body for heading in headings):
                raise ValueError("The PR draft omitted a template section.")
        paragraphs = present(
            self.runtime, [draft.title, *draft.body.split("\n\n")], worktree
        ).split("\n\n", 1)
        title, body = paragraphs
        if not re.match(r"^\[[^\]\n]+\] \S", title) or (
            template.exists() and any(heading not in body for heading in headings)
        ):
            title, body = draft.title, draft.body
        name = "candidate-pr" if candidate else "pr"
        save(directory / f"{name}.json", {"title": title, "body": body})
        (directory / f"{name}.md").write_text(title + "\n\n" + body + "\n", encoding="utf-8")

    def ready_message(self) -> str:
        store = self.store
        return (
            f"The patch is ready for your review. Required checks passed.\n\n"
            f"Worktree: {store.state.worktree}\n"
            f"PR draft: {store.artifacts / 'pr.md'}\n"
            f"Full reviews: {store.artifacts / 'uc-review.md'} and {store.artifacts / 'kernel-review.md'}\n"
            f"Decisions and evidence: {store.root}\n"
        )
