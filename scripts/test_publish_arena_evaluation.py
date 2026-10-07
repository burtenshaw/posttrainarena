"""Offline publisher tests; all task identifiers and credentials are synthetic.

Run directly with python3 scripts/test_publish_arena_evaluation.py or pytest.
"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish_arena_evaluation as P


def _domains():
    return {
        "benchmark": "heldout-v1",
        "tasks": {
            f"synthetic-{domain_index}-{task_index}": domain
            for domain_index, domain in enumerate(P.DOMAINS)
            for task_index in range(5)
        },
    }


def _main_artifact():
    tasks = list(_domains()["tasks"])
    return {
        "mode": "eval",
        "task_ids": tasks,
        "task_count": len(tasks),
        "model": "synthetic-trained-checkpoint",
        "score": 0.54,
        "health": {
            "rows": [
                {
                    "task_id": task,
                    "reward": (1.0, 1.0, 0.7, 0.0, 0.0)[index % 5],
                    "scored": True,
                    "error": None,
                    "verifier_error": None,
                    "valid_llm_trajectory": True,
                }
                for index, task in enumerate(tasks)
            ],
        },
    }


def _v2_artifact():
    rows = _main_artifact()["health"]["rows"]
    return {
        "schema_version": 1,
        "pass_threshold": 1.0,
        "arms": {
            "baseline": None,
            "final": {
                "stage": "posttrain_eval",
                "cells": [
                    {
                        "suite": suite,
                        "trial": 1,
                        "stage": "posttrain_eval" if index == 0 else f"posttrain_eval.{suite}.t01",
                        "jobs_dir": "/private/synthetic",
                        "score": 0.54,
                        "outcomes": {
                            row["task_id"]: {
                                "reward": row["reward"],
                                "passed": row["reward"] >= 1.0,
                                "infra_error": False,
                            }
                            for row in rows[index * 20:(index + 1) * 20]
                        },
                    }
                    for index, suite in enumerate(("synthetic-a", "synthetic-b"))
                ],
            },
        },
    }


def _reject(call):
    try:
        call()
    except ValueError as exc:
        assert "synthetic-" not in str(exc), "private task identifier leaked"
        return str(exc)
    raise AssertionError("invalid input was accepted")


def _run(*args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = P.main(list(args))
    return result, stdout.getvalue(), stderr.getvalue()


def test_main_and_v2_publish_raw_pass_counts_and_no_private_metadata():
    for artifact in (_main_artifact(), _v2_artifact()):
        result = P.aggregate_evaluation(artifact, _domains())
        assert result == {
            "benchmark": "heldout-v1",
            "scores": {domain: {"solved": 2, "total": 5} for domain in P.DOMAINS},
        }
        serialized = json.dumps(result)
        assert "synthetic" not in serialized and "model" not in serialized
        assert "dataset" not in serialized and "user" not in serialized


def test_pass_threshold_matches_evaluator_instead_of_averaging_rewards():
    artifact = _main_artifact()
    rows = artifact["health"]["rows"]
    for row, reward in zip(rows[:5], (0.9999, 1, 1.1, -0.1, 0)):
        row["reward"] = reward
    result = P.aggregate_evaluation(artifact, _domains())
    assert result["scores"][P.DOMAINS[0]] == {"solved": 2, "total": 5}


def test_domain_map_rejects_partial_unknown_and_unbalanced_domains():
    for modification in (
        lambda d: d.update(benchmark="other"),
        lambda d: d["tasks"].pop(next(iter(d["tasks"]))),
        lambda d: d["tasks"].update({next(iter(d["tasks"])): "other"}),
        lambda d: d["tasks"].update({next(iter(d["tasks"])): P.DOMAINS[1]}),
        lambda d: d.update(extra=True),
    ):
        domains = _domains()
        modification(domains)
        _reject(lambda: P.aggregate_evaluation(_main_artifact(), domains))


def test_main_rejects_missing_unknown_or_duplicate_attempts():
    for modification in (
        lambda a: a["health"]["rows"].pop(),
        lambda a: a["health"]["rows"].append(copy.deepcopy(a["health"]["rows"][0])),
        lambda a: a["health"]["rows"][0].update(task_id="synthetic-unknown"),
        lambda a: a["task_ids"].append(a["task_ids"][0]),
        lambda a: a.update(task_count=True),
        lambda a: a.update(task_count=39),
    ):
        artifact = _main_artifact()
        modification(artifact)
        _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_main_rejects_infrastructure_errors_and_incomplete_health():
    for patch in (
        {"scored": False}, {"scored": 1}, {"error": "private failure details"},
        {"verifier_error": "private verifier error"}, {"valid_llm_trajectory": False},
        {"valid_llm_trajectory": 1}, {"infra_error": True},
    ):
        artifact = _main_artifact()
        artifact["health"]["rows"][0].update(patch)
        _reject(lambda: P.aggregate_evaluation(artifact, _domains()))
    artifact = _main_artifact()
    del artifact["health"]["rows"][0]["error"]
    _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_rewards_must_be_finite_numbers_in_both_artifact_formats():
    for reward in (None, True, "1", float("nan"), float("inf"), -float("inf"), 10 ** 400):
        for artifact in (_main_artifact(), _v2_artifact()):
            if "mode" in artifact:
                artifact["health"]["rows"][0]["reward"] = reward
            else:
                rows = artifact["arms"]["final"]["cells"][0]["outcomes"]
                rows[next(iter(rows))]["reward"] = reward
            _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_v2_rejects_multi_trial_incomplete_and_inconsistent_outcomes():
    for modification in (
        lambda a: a.update(schema_version=2),
        lambda a: a.update(schema_version=True),
        lambda a: a.update(pass_threshold=0.5),
        lambda a: a["arms"].update(final=None),
        lambda a: a["arms"]["final"].update(stage="baseline_eval"),
        lambda a: a["arms"]["final"]["cells"][0].update(trial=2),
        lambda a: a["arms"]["final"]["cells"][0].update(trial=True),
        lambda a: a["arms"]["final"]["cells"][0].update(stage="baseline_eval"),
        lambda a: a["arms"]["final"]["cells"].append(copy.deepcopy(a["arms"]["final"]["cells"][0])),
    ):
        artifact = _v2_artifact()
        modification(artifact)
        _reject(lambda: P.aggregate_evaluation(artifact, _domains()))
    for patch in ({"infra_error": True}, {"infra_error": 0}, {"passed": False}, {"passed": 1}):
        artifact = _v2_artifact()
        rows = artifact["arms"]["final"]["cells"][0]["outcomes"]
        rows[next(iter(rows))].update(patch)
        _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_v2_rejects_task_repeated_across_different_suites():
    artifact = _v2_artifact()
    cells = artifact["arms"]["final"]["cells"]
    task_id, row = next(iter(cells[0]["outcomes"].items()))
    cells[1]["outcomes"][task_id] = copy.deepcopy(row)
    _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_wrong_artifact_shapes_are_rejected():
    for artifact in ([], {}, {"rows": []}, {"score_v2": {}}, {"mode": "eval", "arms": {}}):
        _reject(lambda: P.aggregate_evaluation(artifact, _domains()))


def test_json_reader_rejects_duplicate_private_keys_and_nonstandard_numbers():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "input.json"
        for content in ('{"synthetic-private": 1, "synthetic-private": 0}', '{"reward": NaN}', '{"reward": Infinity}', "broken"):
            source.write_text(content)
            _reject(lambda: P.read_json(source))


def test_controller_url_requires_https_or_loopback_and_no_embedded_credentials():
    for controller in ("https://controller.example", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"):
        assert P.evaluation_url(controller, "run-1") == controller + "/runs/run-1/evaluation"
    for controller in (
        "http://controller.example", "http://10.0.0.1", "http://localhost.evil.example",
        "https://user:secret@controller.example", "https://controller.example?token=secret",
        "https://controller.example#fragment", "file:///tmp/controller", "https://controller.example:bad",
        "https://controller.example\n", "https://controller.example/\x00",
    ):
        _reject(lambda: P.evaluation_url(controller, "run-1"))
    for run_id in ("../run", "run/a", "run?secret", "", "run#fragment"):
        _reject(lambda: P.evaluation_url("https://controller.example", run_id))


def test_post_contains_only_aggregate_payload_and_bearer_header():
    payload = P.aggregate_evaluation(_main_artifact(), _domains())
    response = mock.MagicMock()
    response.__enter__.return_value.status = 200
    opener = mock.Mock()
    opener.open.return_value = response
    with mock.patch.object(P.urllib.request, "build_opener", return_value=opener) as build:
        P.publish_evaluation("https://controller.example/runs/run-1/evaluation", payload, "synthetic-secret")
    request = opener.open.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer synthetic-secret"
    assert json.loads(request.data) == payload
    assert "synthetic-secret" not in request.full_url
    assert isinstance(build.call_args.args[0], P.urllib.request.ProxyHandler)
    assert build.call_args.args[0].proxies == {}
    assert isinstance(build.call_args.args[1], P._NoRedirect)
    assert build.call_args.args[1].redirect_request(None, None, 302, None, None, "https://elsewhere.example") is None


def test_missing_or_malformed_credentials_never_make_a_network_request():
    for token in ("", "synthetic secret", "synthetic\nsecret", "synthetic\x00secret", "synthetic-☃"):
        with mock.patch.object(P.urllib.request, "build_opener") as build:
            _reject(lambda: P.publish_evaluation("https://controller.example", {}, token))
        build.assert_not_called()


def test_cli_dry_run_is_offline_and_needs_no_credential():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outcomes, domains = root / "outcomes.json", root / "domains.json"
        outcomes.write_text(json.dumps(_main_artifact()))
        domains.write_text(json.dumps(_domains()))
        with mock.patch.object(P, "publish_evaluation") as publish:
            code, stdout, stderr = _run("--run-id", "run-1", "--outcomes", str(outcomes), "--domains", str(domains), "--dry-run")
        assert code == 0 and not stderr
        assert json.loads(stdout)["scores"][P.DOMAINS[0]] == {"solved": 2, "total": 5}
        assert "synthetic-" not in stdout
        publish.assert_not_called()


def test_cli_validates_before_reading_token_or_posting():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outcomes, domains = root / "outcomes.json", root / "domains.json"
        artifact = _main_artifact()
        artifact["health"]["rows"].pop()
        outcomes.write_text(json.dumps(artifact))
        domains.write_text(json.dumps(_domains()))
        with mock.patch.object(P, "publish_evaluation") as publish:
            code, stdout, stderr = _run("--run-id", "run-1", "--outcomes", str(outcomes), "--domains", str(domains), "--controller", "https://controller.example", "--token-file", str(root / "absent"))
        assert code == 1 and not stdout
        assert "Evaluation rows do not match" in stderr and "token" not in stderr
        publish.assert_not_called()


def test_cli_uses_token_file_or_environment_without_printing_credentials():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outcomes, domains, credential = root / "outcomes.json", root / "domains.json", root / "credential"
        outcomes.write_text(json.dumps(_main_artifact()))
        domains.write_text(json.dumps(_domains()))
        credential.write_text("synthetic-secret\n")
        common = ("--run-id", "run-1", "--outcomes", str(outcomes), "--domains", str(domains), "--controller", "https://controller.example")
        with mock.patch.object(P, "publish_evaluation") as publish:
            for extra in (("--token-file", str(credential)), ()):
                with mock.patch.dict(os.environ, {"ARENA_CONTROLLER_TOKEN": "synthetic-secret"}):
                    code, stdout, stderr = _run(*common, *extra)
                assert code == 0 and stdout == "Evaluation published.\n" and not stderr
                assert publish.call_args.args[2] == "synthetic-secret"


def test_http_errors_never_include_response_body_or_private_reason():
    opener = mock.Mock()
    opener.open.side_effect = urllib.error.HTTPError("https://controller.example", 403, "synthetic-secret", {}, io.BytesIO(b"private task contents"))
    with mock.patch.object(P.urllib.request, "build_opener", return_value=opener):
        message = _reject(lambda: P.publish_evaluation("https://controller.example", {}, "synthetic-secret"))
    assert message == "Controller rejected evaluation (HTTP 403)"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
