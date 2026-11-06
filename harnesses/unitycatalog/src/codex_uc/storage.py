"""Keep approvals, snapshots, and agent output outside writable source trees."""

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .contracts import Design, RunState


class NeedsDiscussion(ValueError):
    """The saved design or evidence no longer authorizes the next source change."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        except BaseException:
            temporary.unlink()
            raise
    os.replace(temporary, path)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, encoding="utf-8", check=True
    )
    return result.stdout.rstrip("\n")


def resolve_repository(path: Path | None) -> Path:
    candidates = (
        [path.expanduser()] if path is not None else [Path.cwd(), Path.home() / "unitycatalog"]
    )
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            return Path(git(candidate, "rev-parse", "--show-toplevel")).resolve()
        except subprocess.CalledProcessError:
            continue
    if path is not None:
        raise ValueError(f"Not a Git repository: {path}. Use --repo /path/to/unitycatalog.")
    raise ValueError(
        "No Git repository found here or at ~/unitycatalog. Use --repo /path/to/unitycatalog."
    )


def inside(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Use a path inside the worktree: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or ".git" in path.parts:
        raise ValueError(f"Use a source path inside the worktree: {relative}")
    return resolved


def snapshot(worktree: Path) -> str:
    # A temporary index includes new files without changing the user's index.
    with TemporaryDirectory(prefix="codex-uc-index-") as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(worktree, "read-tree", "HEAD", env=env)
        git(worktree, "add", "-A", "--", ".", env=env)
        return git(worktree, "write-tree", env=env)


def changed(worktree: Path, base: str, tree: str) -> list[str]:
    output = git(worktree, "diff", "--name-only", "-z", base, tree)
    return [path for path in output.split("\0") if path]


def checkout(repo: Path, destination: Path, base: str, tree: str | None = None) -> None:
    git(repo, "worktree", "add", "--detach", str(destination), base)
    if os.environ.get("CODEX_WORKBENCH_SESSION"):
        subprocess.run(["codex-workbench", "use-worktree", str(destination)], check=True)
    if tree:
        git(destination, "read-tree", "--reset", "-u", tree)


def validate_design(worktree: Path, design: Design) -> None:
    paths = [entry.path for entry in design.files]
    if len(paths) != len(set(paths)):
        raise ValueError("The design lists the same file more than once.")
    for path in paths:
        resolved = inside(worktree, path)
        if resolved.is_dir() or any(char in path for char in "*?["):
            raise ValueError(f"List an exact file in the design: {path}")
    for entry in design.files:
        if entry.role == "test":
            parts = Path(entry.path).parts
            if not ("tests" in parts or "/src/test/" in "/" + entry.path):
                raise ValueError(f"Use a path in a test directory: {entry.path}")
    if not any(check.purpose == "test" for check in design.checks):
        raise ValueError("The design needs at least one test command.")
    for check in design.checks:
        inside(worktree, check.cwd)
        if check.purpose == "test" and not check.junit_glob:
            raise ValueError(f"The test needs a JUnit XML report path: {check.name}")
        if check.junit_glob:
            inside(worktree, str(Path(check.cwd) / check.junit_glob))
            if not check.junit_glob.endswith(".xml"):
                raise ValueError("JUnit report patterns must end with .xml.")


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()

    @contextmanager
    def locked(self):
        # SQLite releases the lock if the controller exits or is killed.
        connection = sqlite3.connect(self.root / "lock.sqlite", timeout=0)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            yield self
        except sqlite3.OperationalError as error:
            raise ValueError("Another command is using this run.") from error
        finally:
            connection.close()

    @property
    def state(self) -> RunState:
        return RunState.model_validate(load(self.root / "state.json"))

    def update(self, **changes) -> RunState:
        state = self.state.model_copy(update=changes)
        save(self.root / "state.json", state.model_dump())
        return state

    def design(self) -> Design:
        return Design.model_validate(load(self.root / "design.json"))

    def approval_hash(self) -> str:
        state = self.state
        return digest(
            {"base": state.base, "task": state.task, "design": self.design().model_dump()}
        )

    def require_approval(self) -> Design:
        if self.state.approved_design != self.approval_hash():
            raise ValueError("Review and approve the current design before running implementation.")
        design = self.design()
        if design.questions:
            raise ValueError("Resolve the design questions before implementation.")
        validate_design(Path(self.state.worktree), design)
        return design

    @property
    def artifacts(self) -> Path:
        path = self.root / "revisions" / self.approval_hash()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def freeze(self, label: str, worktree: Path) -> str:
        tree = snapshot(worktree)
        git(
            worktree,
            "update-ref",
            f"refs/codex-uc/{self.root.name}/{self.approval_hash()}/{label}",
            tree,
        )
        return tree

    def scope(self, base: str, tree: str, *, tests_only: bool = False) -> list[str]:
        state = self.state
        allowed = {
            file.path for file in self.design().files if not tests_only or file.role == "test"
        }
        paths = changed(Path(state.worktree), base, tree)
        outside = set(paths) - allowed
        if outside:
            raise NeedsDiscussion("Changes need a new design: " + ", ".join(sorted(outside)))
        for path in paths:
            inside(Path(state.worktree), path)
        counts = git(Path(state.worktree), "diff", "--numstat", base, tree)
        total = 0
        for line in counts.splitlines():
            added, removed, _ = line.split("\t", 2)
            if "-" in (added, removed):
                raise NeedsDiscussion("Binary source changes need a separate design discussion.")
            total += int(added) + int(removed)
        if total > self.design().change_budget:
            raise NeedsDiscussion("The change exceeds the line budget in the approved design.")
        return paths
