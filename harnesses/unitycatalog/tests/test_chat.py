import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_uc import chat, chat_server
from codex_uc.storage import Store, git, load, save


@pytest.fixture
def session(store, tmp_path):
    root = tmp_path / "chat"
    root.mkdir()
    save(
        root / "chat.json",
        {
            "repo": store.state.repo,
            "base": store.state.base,
            "kernel_review": store.state.kernel_skill,
            "model": None,
            "codex_bin": None,
            "state_dir": str(tmp_path / "runs"),
            "problem": None,
            "run": None,
        },
    )
    return root


@pytest.fixture
def chat_design(store, design):
    save(store.root / "design.json", design.model_dump())
    text = design.model_dump_json(indent=2)
    (store.root / "design.md").write_text(text)
    save(
        store.root / "chat-design.json",
        {"design_hash": store.approval_hash(), "text": text},
    )
    return design


@pytest.mark.parametrize("location", ["explicit", "current", "home"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_launcher_opens_native_chat_without_a_task_or_worktree(
    store, tmp_path, monkeypatch, capsys, location, wrapped
):
    repo = Path(store.state.repo)
    test_home = tmp_path / "home"
    test_home.mkdir()
    default_repo = repo if location == "home" else Path(store.state.worktree)
    (test_home / "unitycatalog").symlink_to(default_repo, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: test_home)
    monkeypatch.chdir(repo / "tests" if location == "current" else test_home)
    executable = tmp_path / "codex"
    record = tmp_path / "arguments.json"
    executable.write_text(
        "#!" + sys.executable + "\nimport json, os, sys\n"
        "if sys.argv[1:] == ['mcp', 'list', '--json']:\n"
        "    print(json.dumps([{'name': 'other_tools'}]))\n"
        "    raise SystemExit(0)\n"
        "with open(os.environ['CHAT_ARGUMENTS'], 'w') as output:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'config': os.environ.get('CODEX_HOME'), 'wrapped': os.environ.get('WRAPPED_CHAT')}, output)\n"
    )
    executable.chmod(0o755)
    launcher = tmp_path / "chat launcher"
    launcher.write_text(
        "#!" + sys.executable + "\nimport os, sys\n"
        "assert sys.argv[1] == '--native'\n"
        "os.environ['WRAPPED_CHAT'] = '1'\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv(
        "CODEX_UC_CHAT_COMMAND", shlex.join([str(launcher), "--native", str(executable)])
    )
    monkeypatch.setenv("CHAT_ARGUMENTS", str(record))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-home"))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("WRAPPED_CHAT", raising=False)
    before = git(Path(store.state.repo), "worktree", "list", "--porcelain")
    args = SimpleNamespace(
        repo=repo / "tests" if location == "explicit" else None,
        task=None,
        base=None,
        kernel_review=None,
        model=None,
        codex_bin=None if wrapped else str(executable),
        state_dir=tmp_path / "sessions",
    )

    assert chat.launch_chat(args) == 0

    launched = json.loads(record.read_text())
    argv = launched["argv"]
    assert launched["cwd"] == store.state.repo
    assert launched["config"] == os.environ["CODEX_HOME"]
    assert launched["wrapped"] == ("1" if wrapped else None)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == store.state.repo
    assert any("codex_uc.chat_server" in value for value in argv)
    assert not any(value.startswith("mcp_servers=") for value in argv)
    assert "features.apps=false" not in argv
    assert "features.plugins=false" not in argv
    assert "--ask-for-approval" not in argv
    assert (
        "approval_policy={granular={sandbox_approval=false,rules=false,skill_approval=false,request_permissions=false,mcp_elicitations=true}}"
        in argv
    )
    assert 'approvals_reviewer="user"' in argv
    sessions = list(args.state_dir.glob("chats/*/chat.json"))
    assert len(sessions) == 1
    state = load(sessions[0])
    assert state["problem"] is None and state["run"] is None
    assert f"Repository: {store.state.repo}" in capsys.readouterr().out
    assert before == git(Path(store.state.repo), "worktree", "list", "--porcelain")
    assert not list(args.state_dir.glob("*/state.json"))

    assert (
        chat.resume_chat(SimpleNamespace(run=None, repo=args.repo, state_dir=args.state_dir)) == 0
    )
    resumed_record = json.loads(record.read_text())
    assert resumed_record["wrapped"] == launched["wrapped"]
    resumed = resumed_record["argv"]
    assert resumed[resumed.index("resume") + 1] == "--last"
    assert next(value for value in resumed if value.startswith("sqlite_home=")) == next(
        value for value in argv if value.startswith("sqlite_home=")
    )
    assert list(args.state_dir.glob("chats/*/chat.json")) == sessions


@pytest.mark.parametrize("location", ["no_default", "explicit_missing", "explicit_not_git"])
def test_invalid_repository_does_not_create_chat(store, tmp_path, monkeypatch, location):
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: test_home)
    monkeypatch.chdir(test_home)
    if location != "no_default":
        (test_home / "unitycatalog").symlink_to(Path(store.state.repo), target_is_directory=True)
    paths = {
        "no_default": None,
        "explicit_missing": test_home / "missing",
        "explicit_not_git": test_home,
    }
    args = SimpleNamespace(repo=paths[location], state_dir=tmp_path / "sessions")

    with pytest.raises(ValueError, match="--repo"):
        chat.launch_chat(args)

    assert not args.state_dir.exists()


def test_problem_agreement_precedes_design_and_does_not_create_a_run(session):
    async def exercise():
        server = chat_server.ChatServer(session)
        with pytest.raises(ValueError, match="agree on the problem"):
            await server.call_tool("propose_design", {"discussion": "Use a small fix."})

        async def declined(message):
            return False

        server.confirm = declined
        problem = {
            "problem": "Negative inputs return the wrong value.",
            "success_criteria": ["value(-2) returns 2."],
            "constraints": ["Keep the current API."],
        }
        assert "not confirmed" in await server.call_tool("agree_problem", problem)
        assert load(session / "chat.json")["problem"] is None

        async def accepted(message):
            assert problem["problem"] in message
            assert problem["success_criteria"][0] in message
            return True

        server.confirm = accepted
        await server.call_tool("agree_problem", problem)
        state = load(session / "chat.json")
        assert state["problem"] == problem
        assert state["run"] is None
        assert not Path(state["state_dir"]).exists()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "response,approved",
    [
        ({"action": "accept", "content": {"approve": True}}, True),
        ({"action": "accept", "content": {"approve": "true"}}, False),
        ({"action": "accept", "content": None}, False),
        ({"action": "decline", "content": None}, False),
    ],
)
def test_problem_confirmation_requires_a_real_client_response(session, response, approved):
    async def exercise():
        server = chat_server.ChatServer(session)
        server.elicitation = True
        messages = []
        server.send = messages.append
        problem = {
            "problem": "Negative inputs return the wrong value.",
            "success_criteria": ["value(-2) returns 2."],
            "constraints": [],
        }
        task = asyncio.create_task(server.call_tool("agree_problem", problem))
        await asyncio.sleep(0)
        assert load(session / "chat.json")["problem"] is None
        assert messages[0]["method"] == "elicitation/create"
        await server.receive({"id": messages[0]["id"], "result": response})
        await task
        assert load(session / "chat.json")["problem"] == (problem if approved else None)

    asyncio.run(exercise())


def test_cancelling_an_agreement_leaves_the_chat_in_discussion(session):
    async def exercise():
        server = chat_server.ChatServer(session)
        server.elicitation = True
        messages = []
        server.send = messages.append
        request = {
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "agree_problem",
                "arguments": {
                    "problem": "A possible bug.",
                    "success_criteria": ["Understand the behavior."],
                    "constraints": [],
                },
            },
        }
        await server.receive(request)
        task = server.tasks[7]
        await asyncio.sleep(0)
        assert "problem_discussion" in await server.call_tool("read_workflow", {})
        with pytest.raises(ValueError, match="Another command"):
            await chat_server.ChatServer(session).call_tool(
                "agree_problem", request["params"]["arguments"]
            )
        await server.receive({"method": "notifications/cancelled", "params": {"requestId": 7}})
        await task
        assert server.pending == {}
        assert load(session / "chat.json")["problem"] is None

    asyncio.run(exercise())


def test_cancelling_a_worker_stops_its_process_group(session, tmp_path, monkeypatch):
    async def exercise():
        marker = tmp_path / "child-stopped"
        child = (
            "import signal,sys,time\nfrom pathlib import Path\n"
            f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(marker)!r}).write_text('stopped'), sys.exit(0)))\n"
            "print('ready', flush=True)\ntime.sleep(300)\n"
        )
        parent = (
            "import subprocess,sys,time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.PIPE, text=True)\n"
            "print(child.stdout.readline().strip(), flush=True)\ntime.sleep(300)\n"
        )
        spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []

        async def spawn_worker(*command, **options):
            process = await spawn(sys.executable, "-c", parent, **options)
            assert await process.stdout.readline() == b"ready\n"
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_worker)
        task = asyncio.create_task(chat_server.ChatServer(session).run_worker("design"))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].returncode is not None

        async def child_stopped():
            while not marker.exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(child_stopped(), timeout=2)
        assert marker.read_text() == "stopped"
        assert load(session / "chat.json")["run"] is None

    asyncio.run(exercise())


def test_agreed_problem_reaches_the_existing_design_controller(
    session, store, runtime, monkeypatch
):
    problem = {
        "problem": "Negative inputs return the wrong value.",
        "success_criteria": ["value(-2) returns 2."],
        "constraints": ["Keep the current API."],
    }
    state = load(session / "chat.json")
    save(session / "chat.json", {**state, "problem": problem})
    monkeypatch.setattr(chat_server, "Runtime", lambda active_store: runtime)

    chat_server.work(session, "design", "Consider zero too.")

    state = load(session / "chat.json")
    active = Store(Path(state["run"]))
    assert active.state.approved_design is None
    assert active.state.phase == "design"
    assert all(
        value in active.state.task for value in [problem["problem"], *problem["constraints"]]
    )
    assert active.design().summary == runtime.design.summary
    assert json.loads(chat_server.describe(session))["design_text"] == (
        active.root / "design.md"
    ).read_text().rstrip("\n")
    assert git(Path(store.state.repo), "status", "--porcelain") == ""


@pytest.mark.parametrize("accepted", [False, True])
def test_code_changes_require_confirmation_of_the_current_design(
    session, store, runtime, chat_design, monkeypatch, accepted
):
    design = chat_design
    state = load(session / "chat.json")
    save(
        session / "chat.json",
        {**state, "problem": {"problem": store.state.task}, "run": str(store.root)},
    )
    calls = []

    async def exercise():
        server = chat_server.ChatServer(session)

        async def confirm(message):
            assert design.summary in message
            return accepted

        async def worker(action, message="", expected_hash="", progress=None):
            calls.append((action, expected_hash))
            return "done"

        server.confirm = confirm
        server.run_worker = worker
        await server.call_tool("implement_design", {})

    asyncio.run(exercise())
    assert calls == ([("implement", store.approval_hash())] if accepted else [])
    assert store.state.approved_design is None


def test_a_stale_design_report_cannot_be_approved(session, store, chat_design):
    save(
        store.root / "design.json",
        chat_design.model_copy(update={"architecture": "The design has changed."}).model_dump(),
    )
    state = load(session / "chat.json")
    save(
        session / "chat.json",
        {**state, "problem": {"problem": store.state.task}, "run": str(store.root)},
    )

    async def exercise():
        server = chat_server.ChatServer(session)

        async def confirm(message):
            pytest.fail("The old report must not be offered for approval.")

        server.confirm = confirm
        with pytest.raises(ValueError, match="design report.*stale"):
            await server.call_tool("implement_design", {})
        report = json.loads(await server.call_tool("read_workflow", {}))
        assert "design_text" not in report

    asyncio.run(exercise())
    assert store.state.approved_design is None


def test_worker_rejects_a_design_changed_after_confirmation(
    session, store, runtime, design, monkeypatch
):
    save(store.root / "design.json", design.model_dump())
    expected = store.approval_hash()
    save(
        store.root / "design.json",
        design.model_copy(update={"architecture": "Changed."}).model_dump(),
    )
    state = load(session / "chat.json")
    save(
        session / "chat.json",
        {**state, "problem": {"problem": store.state.task}, "run": str(store.root)},
    )
    monkeypatch.setattr(chat_server, "Runtime", lambda active_store: runtime)

    with pytest.raises(ValueError, match="design changed"):
        chat_server.work(session, "implement", "", expected)

    assert store.state.approved_design is None
    assert git(Path(store.state.worktree), "status", "--porcelain") == ""


def test_confirmed_chat_design_uses_existing_reviews_and_regression_evidence(
    session, store, runtime, design, monkeypatch
):
    save(store.root / "design.json", design.model_dump())
    state = load(session / "chat.json")
    save(
        session / "chat.json",
        {**state, "problem": {"problem": store.state.task}, "run": str(store.root)},
    )
    monkeypatch.setattr(chat_server, "Runtime", lambda active_store: runtime)

    chat_server.work(session, "implement", expected_hash=store.approval_hash())

    assert store.state.phase == "ready"
    assert store.state.repair_batches == 1
    selected = load(store.artifacts / "selected.json")
    assert len(selected) == 1 and selected[0]["baseline"]
    assert (store.artifacts / "uc-review.json").is_file()
    assert (store.artifacts / "kernel-review.json").is_file()
    report = json.loads(chat_server.describe(session))
    final = load(store.artifacts / "final.json")
    assert report["final_source"] == final["source"]
    assert report["checked_tree"] == final["tree"]
    assert git(Path(store.state.repo), "status", "--porcelain") == ""


def test_stdio_server_handles_questions_and_rejects_early_design(session):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_workflow", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "propose_design",
                "arguments": {"discussion": "No problem agreed yet."},
            },
        },
    ]
    result = subprocess.run(
        [sys.executable, "-m", "codex_uc.chat_server", str(session)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    replies = {
        message["id"]: message
        for line in result.stdout.splitlines()
        if "id" in (message := json.loads(line))
    }
    assert replies[1]["result"]["capabilities"]["tools"] == {}
    names = {tool["name"] for tool in replies[2]["result"]["tools"]}
    assert names == {"agree_problem", "propose_design", "implement_design", "read_workflow"}
    assert "discussion" in replies[3]["result"]["content"][0]["text"].lower()
    assert replies[4]["result"]["isError"]
    assert "agree on the problem" in replies[4]["result"]["content"][0]["text"]
    assert load(session / "chat.json")["run"] is None
