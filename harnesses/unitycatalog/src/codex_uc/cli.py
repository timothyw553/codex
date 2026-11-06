"""The codex-uc command. Design approval is an explicit user action."""

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from .contracts import RunState
from .runtime import Runtime
from .storage import Store, checkout, git, save
from .workflow import Workflow


def find_kernel_skill(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError("The kernel-review SKILL.md does not exist.")
        return path
    roots = {Path.home() / ".codex", Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))}
    paths = [
        path
        for root in roots
        for path in root.glob("plugins/cache/**/skills/kernel-review/SKILL.md")
    ]
    if not paths:
        raise ValueError("Install kernel-review or pass --kernel-review /path/to/SKILL.md.")
    return max(paths, key=lambda path: path.stat().st_mtime_ns).resolve()


def start(args) -> Store:
    repo = Path(git(args.repo.resolve(), "rev-parse", "--show-toplevel")).resolve()
    skill = find_kernel_skill(args.kernel_review)
    if args.base:
        base = git(repo, "rev-parse", args.base + "^{commit}")
    else:
        git(repo, "fetch", "origin", "main")
        base = git(repo, "rev-parse", "origin/main^{commit}")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    root = args.state_dir.expanduser().resolve() / run_id
    if root.is_relative_to(repo):
        raise ValueError("Store run state outside the source repository.")
    root.mkdir(parents=True)
    worktree = root / "worktree"
    checkout(repo, worktree, base)
    git(worktree, "switch", "-c", f"codex-uc/{run_id}")
    if not (worktree / ".github/omnigent/reviewer/REVIEW.md").is_file():
        raise ValueError("The base does not contain Unity Catalog's REVIEW.md workflow.")
    state = RunState(
        repo=str(repo),
        worktree=str(worktree),
        base=base,
        task=args.task,
        kernel_skill=str(skill),
        model=args.model,
        codex_bin=args.codex_bin,
    )
    save(root / "state.json", state.model_dump())
    (root / "task.txt").write_text(args.task + "\n", encoding="utf-8")
    return Store(root)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Build focused Unity Catalog changes with Codex.")
    commands = cli.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("start", help="Investigate a task and propose its design.")
    initial.add_argument("task")
    initial.add_argument("--repo", type=Path, required=True)
    initial.add_argument("--base", help="Use this existing ref instead of fetching origin/main.")
    initial.add_argument("--kernel-review", help="Path to the installed kernel-review SKILL.md.")
    initial.add_argument("--model", help="Use this model instead of the user's configured default.")
    initial.add_argument("--codex-bin", help="Use this Codex executable.")
    initial.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-uc",
    )
    discussion = commands.add_parser(
        "discuss", help="Discuss or revise a design before implementation."
    )
    discussion.add_argument("run", type=Path)
    discussion.add_argument("message")
    approval = commands.add_parser("approve", help="Approve the exact design that you read.")
    approval.add_argument("run", type=Path)
    approval.add_argument(
        "--design", required=True, help="The full design hash shown by start or show."
    )
    for name, help_text in (
        ("show", "Read the current design and its approval hash."),
        ("run", "Implement, review, test claims, repair once, and draft PR text."),
        ("status", "Read the saved phase and artifact paths."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run", type=Path)
    return cli


def main() -> int:
    args = parser().parse_args()
    store = None
    try:
        store = start(args) if args.command == "start" else Store(args.run)
        with store.locked():
            workflow = Workflow(store, Runtime(store), lambda message: print(message, flush=True))
            if args.command in {"start", "discuss", "show"}:
                text = (
                    workflow.show_design()
                    if args.command == "show"
                    else workflow.propose(getattr(args, "message", ""))
                )
                print(text)
                print(f"\nRun: {store.root}\nDesign hash: {store.approval_hash()}")
                if store.design().questions:
                    print(f'Continue with: codex-uc discuss {store.root} "your answer"')
                elif store.state.phase == "design":
                    print(
                        f"Approve with: codex-uc approve {store.root} --design {store.approval_hash()}"
                    )
            elif args.command == "approve":
                workflow.approve(args.design)
                print(f"Design approved. Continue with: codex-uc run {store.root}")
            elif args.command == "run":
                print(workflow.run())
            elif args.command == "status":
                print(
                    f"Phase: {store.state.phase}\n{store.state.message}\nRun: {store.root}\nWorktree: {store.state.worktree}"
                )
        return 0
    except KeyboardInterrupt:
        print("Stopped. The run state and worktrees are saved.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"codex-uc: {error}", file=sys.stderr)
        if store:
            print(f"Run state: {store.root}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
