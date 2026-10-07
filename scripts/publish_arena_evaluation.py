#!/usr/bin/env python3
"""Publish complete heldout-v1 pass counts from private evaluator artifacts.

Reads main's OpenCode evaluation JSON or PR #49's eval_task_outcomes.json.
Only domain counts leave this process; run ownership and dataset provenance are
resolved by the controller. This script does not run training or evaluation.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


BENCHMARK = "heldout-v1"
DOMAINS = (
    "software-engineering",
    "industrial-physical-systems",
    "natural-science",
    "office-white-collar",
    "finance-economics",
    "mathematics-or-formal-reasoning",
    "cybersecurity",
    "media-content-production",
)
TASKS_PER_DOMAIN = 5
TASK_COUNT = len(DOMAINS) * TASKS_PER_DOMAIN


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("JSON contains a non-finite number")


def read_json(path: Path):
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Private task IDs, paths and file contents must not enter public logs.
        raise ValueError("Could not read a valid JSON input file") from exc


def domain_map(document: dict) -> dict[str, str]:
    if not isinstance(document, dict) or set(document) != {"benchmark", "tasks"}:
        raise ValueError("Domain map must contain benchmark and tasks")
    if document["benchmark"] != BENCHMARK:
        raise ValueError("Unsupported benchmark in domain map")
    tasks = document["tasks"]
    if not isinstance(tasks, dict) or len(tasks) != TASK_COUNT:
        raise ValueError("Domain map must contain exactly 40 tasks")
    counts = {domain: 0 for domain in DOMAINS}
    for task_id, domain in tasks.items():
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("Domain map contains an invalid task identifier")
        if not isinstance(domain, str) or domain not in counts:
            raise ValueError("Domain map contains an unknown domain")
        counts[domain] += 1
    if any(count != TASKS_PER_DOMAIN for count in counts.values()):
        raise ValueError("Domain map must contain five tasks in every domain")
    return dict(tasks)


def _reward(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Every outcome must contain a numeric reward")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("Every outcome must contain a finite reward")
    return value


def _main_outcomes(document: dict) -> dict[str, bool]:
    # opencode.evaluate() writes mode, task_ids, task_count and health.rows.
    task_ids = document.get("task_ids")
    if (
        not isinstance(task_ids, list)
        or any(not isinstance(task, str) or not task.strip() for task in task_ids)
        or len(set(task_ids)) != len(task_ids)
        or type(document.get("task_count")) is not int
        or document["task_count"] != len(task_ids)
    ):
        raise ValueError("Evaluation task manifest is invalid")
    health = document.get("health")
    rows = health.get("rows") if isinstance(health, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Evaluation must contain health.rows")
    outcomes = {}
    for row in rows:
        if not isinstance(row, dict) or not {
            "task_id", "reward", "scored", "error", "verifier_error",
            "valid_llm_trajectory",
        }.issubset(row):
            raise ValueError("Evaluation contains an invalid health row")
        task_id = row["task_id"]
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("Evaluation contains an invalid task identifier")
        if task_id in outcomes:
            raise ValueError("Evaluation contains repeated task attempts")
        if (
            row["scored"] is not True
            or row["error"] is not None
            or row["verifier_error"] is not None
            or row["valid_llm_trajectory"] is not True
            or row.get("infra_error", False) is not False
        ):
            raise ValueError("Evaluation contains an unscored or unhealthy attempt")
        outcomes[task_id] = _reward(row["reward"]) >= 1.0
    if set(outcomes) != set(task_ids):
        raise ValueError("Evaluation rows do not match its task manifest")
    return outcomes


def _v2_outcomes(document: dict) -> dict[str, bool]:
    # PR #49 pipeline._heldout_report(): schema_version=1, arms.final.cells.
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported task-outcomes schema")
    if _reward(document.get("pass_threshold")) != 1.0:
        raise ValueError("Task-outcomes pass threshold must be 1.0")
    arms = document.get("arms")
    final = arms.get("final") if isinstance(arms, dict) else None
    if not isinstance(final, dict) or final.get("stage") not in {"posttrain_eval", "sft_eval"}:
        raise ValueError("Task outcomes must contain a final evaluation arm")
    cells = final.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("Final evaluation arm must contain cells")
    outcomes, suites = {}, set()
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("Final evaluation contains an invalid cell")
        suite = cell.get("suite")
        if not isinstance(suite, str) or not suite.strip() or suite in suites:
            raise ValueError("Final evaluation suite is invalid or repeated")
        suites.add(suite)
        if type(cell.get("trial")) is not int or cell["trial"] != 1:
            raise ValueError("heldout-v1 requires exactly one trial per task")
        if cell.get("stage") not in {final["stage"], f"{final['stage']}.{suite}.t01"}:
            raise ValueError("Evaluation cell stage does not match the final arm")
        rows = cell.get("outcomes")
        if not isinstance(rows, dict) or not rows:
            raise ValueError("Evaluation cell must contain task outcomes")
        for task_id, row in rows.items():
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("Evaluation contains an invalid task identifier")
            if task_id in outcomes:
                raise ValueError("Evaluation contains repeated task attempts")
            if not isinstance(row, dict) or row.get("infra_error") is not False:
                raise ValueError("Evaluation contains an infrastructure error")
            passed = _reward(row.get("reward")) >= 1.0
            if type(row.get("passed")) is not bool or row["passed"] != passed:
                raise ValueError("Evaluation pass flag disagrees with its reward")
            outcomes[task_id] = passed
    return outcomes


def aggregate_evaluation(document: dict, domains: dict) -> dict:
    """Validate 40 single attempts and return only the eight public counts."""
    tasks = domain_map(domains)
    if not isinstance(document, dict):
        raise ValueError("Evaluation must be a JSON object")
    if document.get("mode") == "eval" and "arms" not in document:
        outcomes = _main_outcomes(document)
    elif "arms" in document and "mode" not in document:
        outcomes = _v2_outcomes(document)
    else:
        raise ValueError("Unsupported evaluation artifact format")
    if set(outcomes) != set(tasks):
        raise ValueError("Evaluation must cover exactly the 40 mapped tasks")
    scores = {domain: {"solved": 0, "total": TASKS_PER_DOMAIN} for domain in DOMAINS}
    for task_id, passed in outcomes.items():
        scores[tasks[task_id]]["solved"] += int(passed)
    return {"benchmark": BENCHMARK, "scores": scores}


def evaluation_url(controller: str, run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("Invalid run ID")
    if not controller.isprintable() or any(character.isspace() for character in controller):
        raise ValueError("Invalid controller URL")
    try:
        parsed = urllib.parse.urlsplit(controller)
        hostname = parsed.hostname
        parsed.port  # Reject malformed ports before reading any credential.
    except ValueError as exc:
        raise ValueError("Invalid controller URL") from exc
    if (
        not hostname or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise ValueError("Controller URL must not contain credentials, query or fragment")
    loopback = hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Controller requires HTTPS, except for a loopback HTTP tunnel")
    return controller.rstrip("/") + f"/runs/{run_id}/evaluation"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish_evaluation(url: str, payload: dict, token: str) -> None:
    if (
        not token or not token.isascii() or not token.isprintable()
        or any(character.isspace() for character in token)
    ):
        raise ValueError("Controller admin token is missing or invalid")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        # In particular, an HTTP loopback tunnel must never go through an
        # environment-configured remote proxy with the admin credential.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Controller did not accept evaluation")
            # Never print the private controller's response or credential.
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Controller rejected evaluation (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise ValueError("Could not connect to the controller") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", help="Private controller URL; HTTPS or a loopback tunnel")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--outcomes", required=True, type=Path)
    parser.add_argument("--domains", required=True, type=Path, help="Private heldout-v1 task-to-domain JSON")
    parser.add_argument("--token-file", type=Path, help="Read admin token from a private file, otherwise ARENA_CONTROLLER_TOKEN")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print aggregate counts without contacting the controller")
    args = parser.parse_args(argv)
    try:
        payload = aggregate_evaluation(read_json(args.outcomes), read_json(args.domains))
        if args.dry_run:
            print(json.dumps(payload, indent=2))
            return 0
        if not args.controller:
            raise ValueError("--controller is required unless --dry-run is set")
        url = evaluation_url(args.controller, args.run_id)
        try:
            token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else os.environ.get("ARENA_CONTROLLER_TOKEN", "").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Could not read controller admin token file") from exc
        publish_evaluation(url, payload, token)
    except ValueError as exc:
        print(f"Evaluation not published: {exc}", file=sys.stderr)
        return 1
    print("Evaluation published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
