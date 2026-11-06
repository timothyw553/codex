# Codex for Unity Catalog

`codex-uc` opens the normal Codex terminal chat with a Unity Catalog workflow attached.
Start with a question or an unclear problem. Discuss it until you agree on what needs
to change. Then discuss the design before implementation, two independent reviews, and tests.
It uses the Codex app server and its existing sandbox controls.
All work stays local. The command produces a patch and a PR draft for you to review.

## Run it

Install [uv](https://docs.astral.sh/uv/) and Codex. Sign in to Codex as usual.
Install the `kernel-review` skill separately. Its source is not included in this fork.

From this fork's checkout:

```sh
uv run --project harnesses/unitycatalog codex-uc \
  --repo /path/to/unitycatalog
```

If `codex-uc` is on your PATH, run it from your Unity Catalog checkout or any
directory inside it:

```sh
cd /path/to/unitycatalog
codex-uc
```

Talk to it as you would talk to Codex. For example, start with "Listing tables feels
slow, but I do not know why." Ask questions, give examples, and correct assumptions.
You can also supply an opening message: `codex-uc "Help me understand table listing"`.
The conversation stays open after each reply. `codex-uc resume` continues your latest
chat in this repository. A saved chat path can select a specific chat.

The chat starts with read-only source access. Opening it requires no task, model call,
worktree, or installed review skill. The first agreement records the problem, success
criteria, and constraints. Design work starts only after you confirm that agreement.
The second agreement confirms the exact saved design before source changes. Both appear
inside the terminal chat. The controller handles run paths and design hashes.

The command uses the current Git repository. Use `--repo /path/to/unitycatalog` from
another directory. When design work starts, the controller fetches `origin/main` and
creates an isolated worktree. Use `--base REF` to select an existing local ref. Your
original checkout can have unfinished work. The chat reads the original checkout during
problem discussion and uses the controller's worktree for the task. Source edits and
test evidence remain in that worktree and its saved artifacts.

The controller finds `kernel-review` in Codex's plugin cache. Use
`--kernel-review /path/to/kernel-review/SKILL.md` to select it explicitly.
The command uses your configured model. `--model` selects a different one.

For scripts, the explicit `start` command retains the original one-step interface.
It prints a design and exits. The saved-run commands also remain available:

```sh
codex-uc start "Describe a known task"
codex-uc discuss RUN "Explain the upgrade behavior"
codex-uc approve RUN --design DESIGN_HASH
codex-uc run RUN
```

`show RUN` prints the design. `status RUN` shows the saved phase. Run state defaults to
`~/.local/state/codex-uc`. `--state-dir` selects another location outside the repository.
Keep run artifacts private; they can contain local paths and unpublished task details.

## What the controller enforces

1. The chat can discuss a problem without creating a task. The controller requires
   problem confirmation before design. Design runs with read-only source access.
   Implementation requires approval of the current design hash.
   The design lists exact files, reasons, behavior, checks, and a
   change budget. Scope changes stop the run for design discussion.
2. The UC `REVIEW.md` workflow and the full `kernel-review` workflow run in separate
   Codex sessions against the same frozen patch. Each workflow keeps its own reviewers
   and disproof steps. The controller checks actual completed agent events. Review
   source trees are outside the reviewers' writable directories.
3. The selector defaults to leaving code unchanged. It groups duplicate findings and
   keeps rejected decisions. It can shortlist at most three concrete defects. Naming
   advice, optional refactors, speculative checks, and equivalent rewrites stay unchanged.
4. A shortlisted claim needs a test-only patch against the unchanged candidate. The
   controller runs an approved command in Codex's sandbox and records its actual exit code and fresh JUnit
   XML. A named assertion must fail for the claimed reason. Setup failures, skipped tests,
   stale reports, and compile failures do not qualify. A separate agent checks that the
   test proves the approved contract and that a fix is necessary.
5. The repair worker receives only selected, verified findings. It gets one repair batch,
   limited to 80 changed lines. Reproduction tests stay unchanged. Those same tests and
   all required checks must pass after the fix. There is no automatic review-and-nit loop.
6. Chat replies stream through the native terminal and use plain-English instructions.
   Design reports and PR drafts are buffered before display. A small prose
   checker guides the wording. One clarity pass checks even short, abstract sentences.
   At most one wording rewrite runs. It must preserve protected evidence and uncertainty.
   A separate meaning check can reject the rewrite. These checks cannot prove that prose
   is clear or equivalent. The original text remains available.
7. The final PR draft follows the repository template and `[Component]` title convention.
   It describes the actual diff and saved test results. The command creates no PR or review
   comments. A completed model turn does not mark the patch ready.

Canonical reports, original finding IDs, code excerpts, reviewer events, test patches,
commands, logs, and test reports remain in the run directory. Writing checks never edit
the canonical review reports. Run state uses an atomic file update and a process lock.

Test commands must produce JUnit XML. Python's pytest and the JVM test runners used by
Unity Catalog support this format. The report pattern is part of the approved design.
Checks that need unavailable external services leave their claims unverified.
The command sandbox can write to the source checkout and a private temporary directory.
It cannot write to saved approvals, reports, or decisions. A check gets network access
only when its approved design requests it. UC tests that start local servers need this.
Keep build-tool caches in ignored `target` directories. Install the required JDK and
other tools before a run. Long agent calls print a status message every 30 seconds.

## Develop and test

```sh
uv run --project harnesses/unitycatalog --group dev pytest harnesses/unitycatalog/tests
uv run --project harnesses/unitycatalog --group dev ruff check harnesses/unitycatalog
uv run --project harnesses/unitycatalog --group dev ruff format --check harnesses/unitycatalog
```

Tests use temporary Git repositories, real local commands, and recorded agent results.
They check workflow controls and evidence handling without making model calls.
Set `CODEX_UC_LIVE_TESTS=1` to also check command sandbox enforcement with installed Codex.
Real task trials are still needed to measure code quality and writing quality.
