import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ThreadItem, TurnStatus

from codex_uc import runtime as runtime_module
from codex_uc.contracts import Check, Design, Presentation
from codex_uc.runtime import Runtime
from codex_uc.storage import Store


@pytest.fixture
def server(monkeypatch):
    state = SimpleNamespace(
        starts=[],
        resumes=[],
        requests=0,
        interrupt=False,
        items=[],
        schemas=[],
        response='{"paragraphs":["The check finished."]}',
    )

    class Thread:
        id = "saved-thread"

        def run(self, prompt, *, output_schema):
            state.requests += 1
            state.schemas.append(output_schema)
            if state.interrupt:
                state.interrupt = False
                raise KeyboardInterrupt
            return SimpleNamespace(
                id="turn",
                status=TurnStatus.completed,
                final_response=state.response,
                items=state.items,
            )

        def read(self, *, include_turns):
            return SimpleNamespace(
                thread=SimpleNamespace(turns=[SimpleNamespace(items=state.items)])
            )

    class Client:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def thread_start(self, **options):
            state.starts.append(options)
            return Thread()

        def thread_resume(self, identifier, **options):
            state.resumes.append((identifier, options))
            return Thread()

    monkeypatch.setattr(runtime_module, "Codex", Client)
    monkeypatch.setattr(runtime_module, "local_tool_config", lambda *args: (), raising=False)
    return state


def test_submitted_design_schema_requires_every_property_including_defaults(store, server, design):
    server.response = design.model_dump_json()

    result = Runtime(store).ask(store.root, "Return the design.", Design, label="design")

    def check_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node.get("required", [])) == set(node["properties"])
            for value in node.values():
                check_objects(value)
        elif isinstance(node, list):
            for value in node:
                check_objects(value)

    check_objects(server.schemas[0])
    assert result == design
    saved = design.model_dump()
    del saved["checks"][0]["network_access"]
    assert Design.model_validate(saved).checks[0].network_access is False


def test_read_only_agent_and_completed_result_survive_controller_restart(store, server):
    cwd = store.root
    result = Runtime(store).ask(cwd, "Check the connection.", Presentation, label="check")
    assert result.paragraphs == ["The check finished."]
    assert server.starts[0]["sandbox"] == Sandbox.read_only
    assert server.starts[0]["approval_mode"] == ApprovalMode.deny_all
    restarted = Runtime(Store(store.root))
    assert restarted.ask(cwd, "Check the connection.", Presentation, label="check") == result
    assert server.requests == 1


def test_interrupted_call_resumes_the_same_thread_and_access_policy(store, server):
    server.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        Runtime(store).ask(store.root, "Inspect the design.", Presentation, label="design")
    Runtime(Store(store.root)).ask(store.root, "Inspect the design.", Presentation, label="design")
    assert len(server.starts) == 1
    assert server.resumes == [("saved-thread", server.starts[0])]


def test_review_binding_and_style_reach_fresh_native_reviewers(store, server):
    server.items = [
        ThreadItem.model_validate(
            {
                "type": "subAgentActivity",
                "id": "done",
                "kind": "completed",
                "agentPath": "/root/api_reviewer",
                "agentThreadId": "a",
            }
        )
    ]
    runtime = Runtime(store)
    runtime.ask(
        store.root,
        "Review the patch.",
        Presentation,
        label="review",
        writable=True,
        reviewers=True,
        review_context="Pinned source: example-source\nPinned tree: example-tree",
    )
    options = server.starts[0]
    assert options["sandbox"] == Sandbox.workspace_write
    assert (
        options["config"]["multi_agent_v2.subagent_developer_instructions"]
        == options["developer_instructions"]
    )
    assert "Pinned tree: example-tree" in options["developer_instructions"]
    assert runtime.completed_reviewers["review"] == {"api-reviewer"}
    assert options["config"]["sandbox_workspace_write.exclude_slash_tmp"]
    assert options["config"]["sandbox_workspace_write.writable_roots"] == []


def test_a_changed_request_cannot_reuse_an_old_result(store, server):
    runtime = Runtime(store)
    runtime.ask(store.root, "Inspect the first design.", Presentation, label="design")
    runtime.ask(store.root, "Inspect the revised design.", Presentation, label="design")
    assert len(server.starts) == 2
    assert server.resumes == []


def test_workers_disable_inherited_remote_tools(store, tmp_path):
    executable = tmp_path / "codex"
    executable.write_text(
        "#!" + sys.executable + "\nimport json, sys\n"
        "assert sys.argv[1:] == ['mcp', 'list', '--json']\n"
        "print(json.dumps([{'name': 'other_tools'}]))\n"
    )
    executable.chmod(0o755)
    store.update(codex_bin=str(executable))

    config = Runtime(store).config(Path(store.state.worktree))

    assert 'mcp_servers={"other_tools"={enabled=false}}' in config.config_overrides
    assert "features.apps=false" in config.config_overrides
    assert "features.plugins=false" in config.config_overrides


@pytest.mark.skipif(
    os.environ.get("CODEX_UC_LIVE_TESTS") != "1", reason="Needs the installed Codex model service"
)
def test_live_model_accepts_the_design_schema(store, design):
    result = Runtime(store).ask(
        Path(store.state.worktree),
        "Return this design unchanged. Do not use tools.\n" + design.model_dump_json(),
        Design,
        label="live-design-schema",
    )
    assert result == design


@pytest.mark.skipif(
    os.environ.get("CODEX_UC_LIVE_TESTS") != "1", reason="Needs the installed Codex sandbox"
)
@pytest.mark.parametrize("network_access", [False, True])
def test_live_commands_cannot_write_controller_state(store, network_access):
    worktree = store.root / "worktree"
    original = (store.root / "state.json").read_bytes()
    script = """
import errno
import json
import socket
import tempfile
from pathlib import Path
Path('command-output.txt').write_text('allowed')
with tempfile.TemporaryDirectory() as directory:
    Path(directory, 'temporary.txt').write_text('allowed')
try:
    Path(%r).write_text('changed')
except OSError as error:
    if error.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
        raise
    protected = True
else:
    protected = False
try:
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
except OSError as error:
    if error.errno not in {errno.EACCES, errno.EPERM}:
        raise
    network = False
else:
    network = True
print(json.dumps({'protected': protected, 'network': network}))
""" % str(store.root / "state.json")
    check = Check(
        name="sandbox check",
        argv=[sys.executable, "-c", script],
        cwd=".",
        purpose="format",
        junit_glob=None,
        timeout_seconds=20,
        network_access=network_access,
    )
    result = Runtime(store).execute(worktree, check)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"protected": True, "network": network_access}
    assert (worktree / "command-output.txt").read_text() == "allowed"
    assert (store.root / "state.json").read_bytes() == original

    if not network_access:
        timeout = check.model_copy(
            update={
                "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                "timeout_seconds": 1,
            }
        )
        assert Runtime(store).execute(worktree, timeout).exit_code == 124
