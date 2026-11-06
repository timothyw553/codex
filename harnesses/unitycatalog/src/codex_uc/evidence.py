"""Run approved checks and inspect fresh test reports for the claimed behavior."""

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .contracts import Check, Reproduction
from .storage import inside, save


def report_files(worktree: Path, check: Check) -> dict[str, tuple]:
    if not check.junit_glob:
        return {}
    cwd = inside(worktree, check.cwd)
    result = {}
    for path in cwd.glob(check.junit_glob):
        resolved = inside(worktree, str(path.relative_to(worktree)))
        if resolved.is_file():
            result[str(path.relative_to(worktree))] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return result


def run_check(worktree: Path, check: Check, directory: Path, execute) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    before = report_files(worktree, check)
    started = time.time()
    process = execute(worktree, check)
    (directory / "command.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    cases = []
    report_errors = []
    for index, (name, signature) in enumerate(report_files(worktree, check).items()):
        if before.get(name) == signature:
            continue
        xml = (worktree / name).read_bytes()
        (directory / f"report-{index}.xml").write_bytes(xml)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as error:
            report_errors.append(f"{name}: {error}")
            continue
        if any(int(suite.get("errors", "0")) for suite in root.iter("testsuite")):
            report_errors.append(f"{name}: the test suite reported errors")
        for case in root.iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            cases.append(
                {
                    "class": case.get("classname", ""),
                    "name": case.get("name", ""),
                    "skipped": case.find("skipped") is not None,
                    "error": error is not None,
                    "failed": failure is not None,
                    "failure_type": failure.get("type", "") if failure is not None else "",
                    "failure": ET.tostring(failure, encoding="unicode")
                    if failure is not None
                    else "",
                }
            )
    result = {
        "command": check.model_dump(),
        "started_at": started,
        "exit_code": process.exit_code,
        # The app server reserves 124 for command timeouts. Either interpretation
        # of a process returning this code leaves its test evidence unverified.
        "timed_out": process.exit_code == 124,
        "cases": cases,
        "report_errors": report_errors,
    }
    save(directory / "result.json", result)
    return result


def proves_failure(result: dict, reproduction: Reproduction) -> bool:
    matches = [
        case
        for case in result["cases"]
        if (case["class"], case["name"]) == (reproduction.test_class, reproduction.test_name)
    ]
    return (
        result["exit_code"] not in (None, 0)
        and not result["timed_out"]
        and not result["report_errors"]
        and not any(case["error"] or case["skipped"] for case in result["cases"])
        and len(matches) == 1
        and matches[0]["failed"]
        and (
            bool(re.search(r"assert|testfailed|^failed$", matches[0]["failure_type"], re.I))
            or (
                not matches[0]["failure_type"]
                and bool(
                    re.search(
                        r"(?:^|\n)[^\n]*\bAssertionError(?::[^\n]*)?\s*</failure>$",
                        matches[0]["failure"],
                    )
                )
            )
        )
        and bool(reproduction.failure_text.strip())
        and reproduction.failure_text in matches[0]["failure"]
    )


def proves_success(result: dict, reproduction: Reproduction | None = None) -> bool:
    if result["exit_code"] != 0 or result["timed_out"] or result["report_errors"]:
        return False
    cases = result["cases"]
    if any(case["failed"] or case["error"] or case["skipped"] for case in cases):
        return False
    if reproduction:
        cases = [
            case
            for case in cases
            if (case["class"], case["name"]) == (reproduction.test_class, reproduction.test_name)
        ]
        if len(cases) != 1:
            return False
    elif result["command"]["purpose"] == "test" and not cases:
        return False
    return not any(case["failed"] or case["error"] or case["skipped"] for case in cases)
