"""Keep approvals, snapshots, and agent output outside writable source trees."""

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .contracts import Design, Repository, RepositoryTarget, RunState


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
    use_worktree(destination)
    if tree:
        git(destination, "read-tree", "--reset", "-u", tree)


def use_worktree(path: Path) -> None:
    if os.environ.get("CODEX_WORKBENCH_SESSION"):
        subprocess.run(["codex-workbench", "use-worktree", str(path)], check=True)


def validate_design(worktree: Path, design: Design, repositories: set[str] | None = None) -> None:
    paths = [entry.path for entry in design.files]
    if len(paths) != len(set(paths)):
        raise ValueError("The design lists the same file more than once.")
    for path in paths:
        if repositories and (not Path(path).parts or Path(path).parts[0] not in repositories):
            raise ValueError(f"Start the path with its repository name: {path}")
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
        if repositories and (
            not Path(check.cwd).parts or Path(check.cwd).parts[0] not in repositories
        ):
            raise ValueError(f"Run the check inside a selected repository: {check.name}")
        inside(worktree, check.cwd)
        if check.purpose == "test" and not check.junit_glob:
            raise ValueError(f"The test needs a JUnit XML report path: {check.name}")
        if check.junit_glob:
            inside(worktree, str(Path(check.cwd) / check.junit_glob))
            if not check.junit_glob.endswith(".xml"):
                raise ValueError("JUnit report patterns must end with .xml.")
    if repositories:
        changed_repositories = {
            Path(entry.path).parts[0]
            for entry in design.files
            if entry.role in {"production", "test"}
        }
        tested = {Path(check.cwd).parts[0] for check in design.checks if check.purpose == "test"}
        if changed_repositories - tested:
            raise ValueError(
                "The design needs tests for each changed repository: "
                + ", ".join(sorted(changed_repositories - tested))
            )


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

    @property
    def repositories(self) -> list[Repository]:
        state = self.state
        return state.repositories or [
            Repository(
                name=Path(state.repo).name, path=state.repo, base=state.base, target=state.base
            )
        ]

    def sources(self, root: Path | None = None) -> dict[str, Path]:
        root = Path(self.state.worktree) if root is None else root
        return {
            repo.name: root / repo.name if self.state.repositories else root
            for repo in self.repositories
        }

    def canonical_target(self, target: RepositoryTarget) -> RepositoryTarget:
        path = resolve_repository(Path(target.path))
        common = Path(
            git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        for repo in self.repositories:
            known = Path(
                git(Path(repo.path), "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
            if common == known:
                path = Path(repo.path)
                break
        return target.model_copy(update={"path": str(path)})

    def source_context(self, root: Path, revision: str | None = None) -> str:
        sources = self.sources(root)
        trees = self.trees(revision) if revision else {}
        return "\n\n".join(
            f"Repository: {repo.name}\nSource path: {sources[repo.name]}\n"
            f"Target ref: {repo.target}\nBase commit: {repo.base}\n"
            + (f"Candidate tree: {trees[repo.name]}\n" if trees else "")
            + f"Path prefix: {repo.name + '/' if self.state.repositories else '(none)'}"
            for repo in self.repositories
        )

    def validate_design(self, design: Design) -> None:
        names = set(self.sources()) if self.state.repositories else None
        validate_design(Path(self.state.worktree), design, names)

    def _source_trees(self, root: Path) -> dict[str, str]:
        sources = self.sources(root)
        if self.state.repositories:
            outside = {path.name for path in root.iterdir()} - sources.keys()
            if outside:
                raise NeedsDiscussion(
                    "The workspace has paths outside its repositories: "
                    + ", ".join(sorted(outside))
                )
        return {name: snapshot(path) for name, path in sources.items()}

    def _snapshot_id(self, trees: dict[str, str]) -> str:
        return digest(trees) if self.state.repositories else next(iter(trees.values()))

    def snapshot(self, root: Path) -> str:
        return self._snapshot_id(self._source_trees(root))

    @property
    def base_tree(self) -> str:
        return self._snapshot_id(
            {
                repo.name: git(Path(repo.path), "rev-parse", repo.base + "^{tree}")
                for repo in self.repositories
            }
        )

    def trees(self, revision: str) -> dict[str, str]:
        if not self.state.repositories:
            return {self.repositories[0].name: revision}
        if revision == self.state.base:
            return {repo.name: repo.base for repo in self.repositories}
        trees = load(self.root / "snapshots" / f"{revision}.json")
        if digest(trees) != revision or set(trees) != set(self.sources()):
            raise NeedsDiscussion(
                "The saved repository snapshot changed. Discuss the design again."
            )
        return trees

    def checkout(self, destination: Path, revision: str | None = None) -> None:
        trees = self.trees(revision) if revision else {}
        if self.state.repositories:
            destination.mkdir(parents=True, exist_ok=True)
        sources = self.sources(destination)
        for repo in self.repositories:
            checkout(Path(repo.path), sources[repo.name], repo.base, trees.get(repo.name))

    def diff(self, before: str, after: str) -> str:
        old, new = self.trees(before), self.trees(after)
        patches = []
        for repo in self.repositories:
            prefix = repo.name + "/" if self.state.repositories else ""
            patches.append(
                git(
                    Path(repo.path),
                    "diff",
                    "--binary",
                    f"--src-prefix=a/{prefix}",
                    f"--dst-prefix=b/{prefix}",
                    old[repo.name],
                    new[repo.name],
                )
            )
        return "\n".join(patch for patch in patches if patch)

    def changes(self, before: str, after: str) -> tuple[list[str], int]:
        old, new = self.trees(before), self.trees(after)
        paths = []
        total = 0
        for repo in self.repositories:
            root = Path(repo.path)
            prefix = repo.name + "/" if self.state.repositories else ""
            paths.extend(prefix + path for path in changed(root, old[repo.name], new[repo.name]))
            counts = git(root, "diff", "--numstat", old[repo.name], new[repo.name])
            for line in counts.splitlines():
                added, removed, _ = line.split("\t", 2)
                if "-" in (added, removed):
                    raise NeedsDiscussion(
                        "Binary source changes need a separate design discussion."
                    )
                total += int(added) + int(removed)
        return paths, total

    def restore(self, root: Path, revision: str) -> None:
        trees = self.trees(revision)
        sources = self.sources(root)
        for repo in self.repositories:
            source = sources[repo.name]
            git(source, "read-tree", "--reset", "-u", trees[repo.name])
            git(source, "reset", "--mixed", repo.base)

    def design(self) -> Design:
        return Design.model_validate(load(self.root / "design.json"))

    @property
    def design_ready(self) -> bool:
        return (
            self.state.phase not in {"design_generating", "design_failed"}
            and (self.root / "design.json").is_file()
        )

    def require_design(self) -> Design:
        if not self.design_ready:
            raise ValueError(
                "No completed design is ready. Request the design again. "
                "The saved discussion will be reused."
            )
        return self.design()

    def approval_hash(self) -> str:
        state = self.state
        contract = {"base": state.base, "task": state.task, "design": self.design().model_dump()}
        if state.repositories:
            contract["repositories"] = [repo.model_dump() for repo in state.repositories]
        return digest(contract)

    def require_approval(self) -> Design:
        design = self.require_design()
        if self.state.approved_design != self.approval_hash():
            raise ValueError("Review and approve the current design before running implementation.")
        if design.questions:
            raise ValueError("Resolve the design questions before implementation.")
        self.validate_design(design)
        return design

    @property
    def artifacts(self) -> Path:
        path = self.root / "revisions" / self.approval_hash()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def freeze(self, label: str, worktree: Path) -> str:
        trees = self._source_trees(worktree)
        revision = self._snapshot_id(trees)
        if self.state.repositories:
            save(self.root / "snapshots" / f"{revision}.json", trees)
        for source_name, source in self.sources(worktree).items():
            git(
                source,
                "update-ref",
                f"refs/codex-uc/{self.root.name}/{self.approval_hash()}/{label}",
                trees[source_name],
            )
        return revision

    def scope(self, base: str, tree: str, *, tests_only: bool = False) -> list[str]:
        allowed = {
            file.path for file in self.design().files if not tests_only or file.role == "test"
        }
        paths, total = self.changes(base, tree)
        outside = set(paths) - allowed
        if outside:
            raise NeedsDiscussion("Changes need a new design: " + ", ".join(sorted(outside)))
        for path in paths:
            inside(Path(self.state.worktree), path)
        if total > self.design().change_budget:
            raise NeedsDiscussion("The change exceeds the line budget in the approved design.")
        return paths
