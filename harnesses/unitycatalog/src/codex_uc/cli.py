"""The codex-uc command. Design approval is an explicit user action."""

import argparse
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from .chat import launch_chat, resume_chat
from .contracts import Repository, RepositoryTarget, RunState
from .runtime import Runtime
from .storage import Store, checkout, git, resolve_repository, save, use_worktree
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


def resolve_target(target: RepositoryTarget) -> Repository:
    repo = resolve_repository(Path(target.path))
    ref = target.base
    if ref is None:
        remote = git(repo, "ls-remote", "--symref", "origin", "HEAD")
        match = re.search(r"(?m)^ref: (refs/heads/[^\t\n]+)\tHEAD$", remote)
        if not match:
            raise ValueError(f"Select a target ref for {repo}; origin has no default branch.")
        ref = match.group(1)
        git(repo, "fetch", "origin", ref)
        base = git(repo, "rev-parse", "FETCH_HEAD^{commit}")
    else:
        base = git(repo, "rev-parse", ref + "^{commit}")
    return Repository(name=repo.name, path=str(repo), base=base, target=ref)


def include_repositories(store: Store, targets: list[RepositoryTarget]) -> None:
    existing = {repo.path: repo for repo in store.repositories}
    additions = []
    for target in targets:
        path = str(resolve_repository(Path(target.path)))
        if path in existing:
            if (
                target.base
                and target.base not in {existing[path].target, existing[path].base}
                and git(Path(path), "rev-parse", target.base + "^{commit}") != existing[path].base
            ):
                raise ValueError("The target ref changed. Start a new task for that target.")
        else:
            repo = resolve_target(target)
            existing[path] = repo
            additions.append(repo)
    if not additions:
        return
    repositories = list(existing.values())
    if len({repo.name for repo in repositories}) != len(repositories):
        raise ValueError("Select repositories with distinct directory names.")
    state = store.state
    old_source = Path(state.worktree)
    if not old_source.is_relative_to(store.root):
        raise ValueError("Only controller worktrees can be moved into a shared workspace.")
    workspace = old_source if state.repositories else store.root / "workspace"
    if not state.repositories and workspace.exists():
        raise ValueError(f"Inspect the existing workspace before continuing: {workspace}")
    created = []
    moved = False
    try:
        if not state.repositories:
            workspace.mkdir()
            destination = workspace / repositories[0].name
            git(Path(state.repo), "worktree", "move", str(old_source), str(destination))
            moved = True
            use_worktree(destination)
        for repo in additions:
            destination = workspace / repo.name
            created.append((repo, destination))
            checkout(Path(repo.path), destination, repo.base)
            git(destination, "switch", "-c", f"codex-uc/{store.root.name}")
    except Exception as error:
        try:
            for repo, destination in reversed(created):
                if destination.exists():
                    git(Path(repo.path), "worktree", "remove", str(destination))
        finally:
            if moved:
                git(
                    Path(state.repo),
                    "worktree",
                    "move",
                    str(workspace / repositories[0].name),
                    str(old_source),
                )
                use_worktree(old_source)
            if not state.repositories and workspace.exists() and not any(workspace.iterdir()):
                workspace.rmdir()
        store.update(phase="design_failed", message=f"Could not add repositories: {error}")
        raise
    store.update(worktree=str(workspace), repositories=repositories)


def start(args) -> Store:
    targets = getattr(args, "repositories", []) or [
        RepositoryTarget(path=str(resolve_repository(args.repo)), base=args.base)
    ]
    if args.base and targets[0].base is None:
        targets = [targets[0].model_copy(update={"base": args.base}), *targets[1:]]
    repositories = [resolve_target(target) for target in targets]
    if len({repo.name for repo in repositories}) != len(repositories):
        raise ValueError("Select repositories with distinct directory names.")
    repo = repositories[0]
    skill = find_kernel_skill(args.kernel_review)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    root = args.state_dir.expanduser().resolve() / run_id
    if any(root.is_relative_to(Path(entry.path)) for entry in repositories):
        raise ValueError("Store run state outside the source repository.")
    root.mkdir(parents=True)
    worktree = root / "worktree"
    state = RunState(
        repo=repo.path,
        worktree=str(worktree),
        base=repo.base,
        repositories=repositories if len(repositories) > 1 else [],
        task=args.task,
        kernel_skill=str(skill),
        model=args.model,
        codex_bin=args.codex_bin,
    )
    save(root / "state.json", state.model_dump())
    (root / "task.txt").write_text(args.task + "\n", encoding="utf-8")
    store = Store(root)
    store.checkout(worktree)
    for source in store.sources().values():
        git(source, "switch", "-c", f"codex-uc/{run_id}")
    if not any(
        (source / ".github/omnigent/reviewer/REVIEW.md").is_file()
        for source in store.sources().values()
    ):
        raise ValueError("The selected bases do not contain Unity Catalog's REVIEW.md workflow.")
    return store


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cli = argparse.ArgumentParser(
        description="Build focused Unity Catalog changes with Codex.",
        epilog='Run codex-uc to chat, or codex-uc "Describe the change". '
        "Chat uses the directory where you launch it. Use --repo PATH to choose another directory.",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-uc"
    for name, help_text in (
        ("chat", "Open the normal Codex chat. Start by discussing the problem."),
        ("start", "Investigate a known task once and print its design."),
    ):
        initial = commands.add_parser(name, help=help_text)
        initial.add_argument("task", nargs="?", help="Optional opening message.")
        initial.add_argument(
            "--repo", type=Path, help="Directory to open. Chat defaults to the current directory."
        )
        initial.add_argument(
            "--base",
            help="Use this existing ref for the first repository instead of its default branch.",
        )
        initial.add_argument(
            "--kernel-review", help="Path to the installed kernel-review SKILL.md."
        )
        initial.add_argument(
            "--model", help="Use this model instead of the user's configured default."
        )
        initial.add_argument("--codex-bin", help="Use this Codex executable.")
        initial.add_argument("--state-dir", type=Path, default=state_dir)
    continuation = commands.add_parser("resume", help="Continue the latest chat in this workspace.")
    continuation.add_argument("run", nargs="?", type=Path)
    continuation.add_argument("--repo", type=Path)
    continuation.add_argument("--state-dir", type=Path, default=state_dir)
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
    argv = sys.argv[1:] if argv is None else argv
    if not argv or (argv[0] not in commands.choices and argv[0] not in {"-h", "--help"}):
        argv = ["chat", *argv]
    return cli.parse_args(argv)


def main() -> int:
    args = parse_args()
    store = None
    try:
        if args.command == "chat":
            return launch_chat(args)
        if args.command == "resume":
            return resume_chat(args)
        if args.command == "start":
            while not args.task or not args.task.strip():
                args.task = input("What would you like to work on? ")
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
        message = "Stopped. The run state and worktrees are saved." if store else "Stopped."
        print(message, file=sys.stderr)
        return 130
    except EOFError:
        print('No task entered. Use codex-uc "Describe the change".', file=sys.stderr)
        return 1
    except Exception as error:
        print(f"codex-uc: {error}", file=sys.stderr)
        if store:
            print(f"Run state: {store.root}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
