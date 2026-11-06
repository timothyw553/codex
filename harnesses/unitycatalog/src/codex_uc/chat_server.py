"""A local MCP stdio bridge from native chat to the existing controller."""

import argparse
import asyncio
import hmac
import json
import os
import secrets
import signal
import sys
from pathlib import Path

from pydantic import Field

from .contracts import Record
from .runtime import Runtime
from .storage import Store, load, save
from .workflow import Workflow


class Problem(Record):
    problem: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(max_length=20)


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
TOOLS = [
    {
        "name": "agree_problem",
        "description": "Confirm the problem, success criteria, and constraints with the user. Use after discussing them in chat. Does not start design or implementation.",
        "inputSchema": Problem.model_json_schema(),
    },
    {
        "name": "propose_design",
        "description": "Investigate or revise the design for the agreed problem. Include decisions from the chat. An empty discussion reuses the saved discussion. Requires problem confirmation. Source access is read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"discussion": {"type": "string", "default": ""}},
            "additionalProperties": False,
        },
    },
    {
        "name": "implement_design",
        "description": "Ask the user to confirm the saved design, then implement it through both reviews, conservative finding selection, regression evidence, and one repair batch. Resumes an already approved run without asking again.",
        "inputSchema": EMPTY,
    },
    {
        "name": "read_workflow",
        "description": "Read the agreed problem, current design, worktree, phase, and saved evidence. Does not start work or change a design.",
        "inputSchema": EMPTY,
        "annotations": {"readOnlyHint": True},
    },
]


def design_report(root: Path, expected_hash: str) -> str | None:
    path = root / "chat-design.json"
    if path.exists():
        report = load(path)
        if hmac.compare_digest(report["design_hash"], expected_hash):
            return report["text"]
    return None


def describe(root: Path) -> str:
    session = load(root / "chat.json")
    result = {"phase": "problem_discussion", "problem": session["problem"]}
    if session["problem"]:
        result["phase"] = "problem_agreed"
    if session["run"]:
        store = Store(Path(session["run"]))
        result.update(store.state.model_dump())
        result["run"] = str(store.root)
        if store.design_ready:
            result["design"] = store.design().model_dump()
            result["design_hash"] = store.approval_hash()
            text = design_report(store.root, result["design_hash"])
            if text:
                result["design_text"] = text
        if store.state.approved_design:
            artifacts = store.root / "revisions" / result["design_hash"]
            result["evidence"] = str(artifacts)
            if (artifacts / "final.json").exists():
                final = load(artifacts / "final.json")
                result["final_source"] = final["source"]
                result["checked_tree"] = final["tree"]
    return json.dumps(result, ensure_ascii=False, indent=2)


def work(root: Path, action: str, message: str = "", expected_hash: str = "") -> str:
    session = load(root / "chat.json")
    if not session["problem"]:
        raise ValueError("First discuss and agree on the problem.")
    if not session["run"]:
        if action != "design":
            raise ValueError("Discuss a design before implementation.")
        from .cli import start

        problem = Problem.model_validate(session["problem"])
        task = (
            problem.problem
            + "\n\nSuccess criteria:\n"
            + "\n".join("- " + item for item in problem.success_criteria)
            + "\n\nConstraints:\n"
            + "\n".join("- " + item for item in problem.constraints)
        )
        store = start(
            argparse.Namespace(
                repo=Path(session["repo"]),
                base=session["base"],
                kernel_review=session["kernel_review"],
                model=session["model"],
                codex_bin=session["codex_bin"],
                state_dir=Path(session["state_dir"]),
                task=task,
            )
        )
        session["run"] = str(store.root)
        save(root / "chat.json", session)
    store = Store(Path(session["run"]))
    with store.locked():
        workflow = Workflow(store, Runtime(store))
        if action == "design":
            text = workflow.propose(message)
            save(
                store.root / "chat-design.json",
                {"design_hash": store.approval_hash(), "text": text},
            )
            return text
        if action != "implement":
            raise ValueError("Unknown chat worker action.")
        store.require_design()
        if not hmac.compare_digest(expected_hash, store.approval_hash()):
            raise ValueError("The design changed. Discuss it again before approving it.")
        if store.state.approved_design != expected_hash:
            workflow.approve(expected_hash)
        return workflow.run()


class ChatServer:
    def __init__(self, root: Path):
        self.root = root
        self.elicitation = False
        self.pending = {}
        self.tasks = {}
        self.mutation = asyncio.Lock()

    def send(self, message):
        print(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False), flush=True)

    async def confirm(self, message: str) -> bool:
        if not self.elicitation:
            raise ValueError("This Codex client cannot confirm the agreement. Update Codex first.")
        identifier = "agreement-" + secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        self.send(
            {
                "id": identifier,
                "method": "elicitation/create",
                "params": {
                    "message": message,
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "approve": {"type": "boolean", "title": "Agree", "default": False}
                        },
                        "required": ["approve"],
                    },
                },
            }
        )
        try:
            result = await future
            content = result.get("content") or {}
            return result.get("action") == "accept" and content.get("approve") is True
        finally:
            self.pending.pop(identifier, None)

    async def run_worker(self, action, message="", expected_hash="", progress=None):
        log = self.root / f"{action}-{secrets.token_hex(4)}.log"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "codex_uc.chat_server",
            str(self.root),
            "--worker",
            action,
            "--message",
            message,
            "--expected-hash",
            expected_hash,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            count = 0
            with log.open("wb") as output:
                while chunk := await process.stdout.read(4096):
                    output.write(chunk)
                    output.flush()
                    if progress is not None:
                        count += 1
                        self.send(
                            {
                                "method": "notifications/progress",
                                "params": {
                                    "progressToken": progress,
                                    "progress": count,
                                    "message": f"Unity Catalog {action} work is running.",
                                },
                            }
                        )
            if await process.wait():
                raise ValueError(log.read_text(encoding="utf-8", errors="replace")[-4000:])
        finally:
            # Stop the worker and its app-server children when chat cancels the tool.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        return describe(self.root)

    async def call_tool(self, name, arguments, progress=None):
        if name == "read_workflow":
            if arguments:
                raise ValueError("read_workflow takes no arguments.")
            return describe(self.root)
        if self.mutation.locked():
            raise ValueError("Work is already running in this chat. Wait or cancel it first.")
        async with self.mutation:
            with Store(self.root).locked():
                return await self.change_tool(name, arguments, progress)

    async def change_tool(self, name, arguments, progress):
        session = load(self.root / "chat.json")
        if name == "agree_problem":
            problem = Problem.model_validate(arguments).model_dump()
            if problem == session["problem"]:
                return describe(self.root)
            text = "Confirm the problem for design work.\n\n" + problem["problem"]
            text += "\n\nSuccess criteria:\n" + "\n".join(problem["success_criteria"])
            text += "\n\nConstraints:\n" + "\n".join(problem["constraints"])
            if not await self.confirm(text):
                return "The problem was not confirmed. Continue the discussion.\n\n" + describe(
                    self.root
                )
            if session["run"]:
                session.setdefault("previous_runs", []).append(session["run"])
            session.update(problem=problem, run=None)
            save(self.root / "chat.json", session)
            return describe(self.root)
        if not session["problem"]:
            raise ValueError("First discuss and agree on the problem.")
        if name == "propose_design":
            if set(arguments) - {"discussion"}:
                raise ValueError("propose_design accepts only discussion.")
            return await self.run_worker(
                "design", arguments.get("discussion", ""), progress=progress
            )
        if name != "implement_design" or arguments:
            raise ValueError("Unknown tool or unexpected arguments.")
        if not session["run"]:
            raise ValueError("Discuss a design before implementation.")
        store = Store(Path(session["run"]))
        design = store.require_design()
        expected = store.approval_hash()
        if design.questions:
            raise ValueError("Resolve the design questions in chat before implementation.")
        if store.state.approved_design != expected:
            text = design_report(store.root, expected)
            if not text:
                raise ValueError(
                    "The design report is incomplete or stale. Discuss the design again."
                )
            if not await self.confirm("Approve this design for implementation?\n\n" + text):
                return "The design was not approved. Continue the discussion.\n\n" + describe(
                    self.root
                )
        return await self.run_worker("implement", expected_hash=expected, progress=progress)

    async def tool_request(self, message):
        identifier = message["id"]
        try:
            params = message.get("params", {})
            result = await self.call_tool(
                params["name"],
                params.get("arguments", {}),
                params.get("_meta", {}).get("progressToken"),
            )
            content = {"content": [{"type": "text", "text": result}], "isError": False}
        except asyncio.CancelledError:
            return
        except Exception as error:
            content = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        self.send({"id": identifier, "result": content})

    async def receive(self, message):
        identifier = message.get("id")
        method = message.get("method")
        if method is None:
            future = self.pending.get(identifier)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(ValueError("The client could not confirm the agreement."))
                else:
                    future.set_result(message.get("result", {}))
            return
        if method == "initialize":
            capabilities = message.get("params", {}).get("capabilities", {})
            self.elicitation = isinstance(capabilities.get("elicitation"), dict)
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unitycatalog", "version": "0.1.0"},
                "instructions": "Discuss and confirm the problem before proposing a design. Confirm the saved design before implementation. Use read_workflow to answer status questions without starting work.",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            task = asyncio.create_task(self.tool_request(message))
            self.tasks[identifier] = task
            task.add_done_callback(lambda done: self.tasks.pop(identifier, None))
            return
        elif method == "notifications/cancelled":
            task = self.tasks.get(message.get("params", {}).get("requestId"))
            if task:
                task.cancel()
            return
        elif identifier is None:
            return
        else:
            self.send({"id": identifier, "error": {"code": -32601, "message": "Unknown method."}})
            return
        self.send({"id": identifier, "result": result})

    async def serve(self):
        try:
            while line := await asyncio.to_thread(sys.stdin.readline):
                await self.receive(json.loads(line))
                await asyncio.sleep(0)
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path)
    parser.add_argument("--worker", choices=["design", "implement"])
    parser.add_argument("--message", default="")
    parser.add_argument("--expected-hash", default="")
    args = parser.parse_args()
    if args.worker:
        print(work(args.session, args.worker, args.message, args.expected_hash))
    else:
        asyncio.run(ChatServer(args.session).serve())


if __name__ == "__main__":
    main()
