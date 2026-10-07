# User leaderboard

The backend patch stores complete evaluated runs and ranks each Hugging Face
user by the average of their eight independently best domain scores. Every
winning cell retains its run, submission, and pinned dataset revision. Lower
scores never replace a best; ties keep the first recorded winner. All scored
runs remain in SQLite. No domain declaration is added to submissions.

`POST /runs/{run_id}/evaluation` is administrator-only. It requires a completed,
admitted, real training run with a committed checkpoint and pinned dataset.
Identity and dataset provenance come from the stored run/submission, never the
posted result. Identical retries are idempotent; conflicting results return 409.
`GET /leaderboard` is readable through the public Space bridge; score writes
remain outside that bridge. Results travel in bounded pages of 50 users and 50
runs with a stable snapshot cursor. The Space assembles the complete snapshot
before displaying it, so newly published results cannot shift in-flight pages.

The companion frontend belongs to the
[Arena Space](https://huggingface.co/spaces/openenvarena/arena). It shows 10 users
per page, HF avatars, dataset links per domain, and a pinned signed-in user row
when their rank exceeds 10 and they are outside the selected page. The model
baseline is an unranked reference. The chart continues to show individual runs.

## Backend deployment patch

The production runtime is not imported into this GitHub repository.
`backend.patch` targets the server source at
`/data/arena/src/posttrain-infra`; `backend-baseline.json` records the exact
reviewed hashes. New files must be absent. The helper refuses a changed baseline
so another deployment's changes cannot be silently overwritten.

Check against a staged source copy, then apply explicitly:

```sh
python3 deploy/leaderboard/apply_backend.py /path/to/staged/posttrain-infra
python3 deploy/leaderboard/apply_backend.py /path/to/staged/posttrain-infra --apply
cd /path/to/staged/posttrain-infra
PYTHONPATH=src python3 -m pytest -q tests/test_leaderboard.py
```

The schema addition creates a separate evaluations table and preserves existing
submissions, runs, and events. Deploy the controller and outbound bridge together,
then the companion frontend. Its live `/api/leaderboard` needs the new read route;
an unavailable backend is displayed as unavailable rather than an empty ranking.
This patch does not change the trainer or schedule paid evaluation jobs.

## Evaluation publisher

`scripts/publish_arena_evaluation.py` converts private evaluator outcomes into
the eight public `heldout-v1` domain counts and sends them to the private
controller's administrator endpoint. It uses the standard library only.

The publisher does **not** run the private evaluation, schedule it after native
OpenEnv training, or establish that an artifact belongs to a training run. The
operator must obtain the trained checkpoint's actual final evaluation artifact,
verify that it belongs to `--run-id`, and supply the organizer's private task map.
The controller derives the authenticated submitting user and the pinned training
dataset from that run; neither can be supplied by this script.

## Supported evaluator artifacts

The accepted formats come from existing evaluator code, not a new trial schema:

1. Main's `opencode.evaluate()` JSON, normally
   `results/posttrain_eval.json` (or `results/sft_eval.json` for an SFT run).
   Required fields are `mode: "eval"`, `task_ids`, `task_count`, and `health.rows`.
   Each health row must contain `task_id`, finite numeric `reward`, `scored: true`,
   `error: null`, `verifier_error: null`, and `valid_llm_trajectory: true`.
   The aggregate `score` is not used: it is not necessarily a binary pass rate.
   [Existing implementation](https://github.com/benchflow-ai/posttrainarena/blob/4fffddff36d8c321bce5cab0d3b4a8d270f277e9/pipelines/benchflow-task-posttrain/src/posttrainarena/benchflow_pipeline/opencode.py#L315-L338).
2. Draft PR #49's `reports/eval_task_outcomes.json`, with `schema_version: 1`,
   `pass_threshold: 1.0`, and `arms.final.cells`. Only the final arm with stage
   `posttrain_eval` or `sft_eval` is consumed. Every cell must have a unique
   `suite`, `trial: 1`, the matching stage, and an `outcomes` object keyed by task
   ID. Each outcome contains finite `reward`, matching boolean `passed`, and
   `infra_error: false`. PR #49's default multi-trial recipe is incompatible with
   this benchmark's single-attempt rule and is rejected.
   [Existing artifact writer](https://github.com/benchflow-ai/posttrainarena/blob/dcdf73a74c7c15b6a792e18dad0a6b21770c6bab/pipelines/benchflow-task-posttrain/src/posttrainarena/benchflow_pipeline/pipeline.py#L1614-L1647).

Bare `results.jsonl`, health-only JSON, `score.json`, and summary pass rates are
not accepted. Main does not need PR #49 to use the publisher. Main's strict
health-row policy rejects agent errors, including timeout errors; PR #49's
normalized final outcomes may already classify a scored timeout as a healthy
failure. The publisher preserves that evaluator classification and never turns
an infrastructure error or absent result into a failed task.

## Private domain map and scoring

The organizer supplies a JSON object with exactly two fields:

```json
{
  "benchmark": "heldout-v1",
  "tasks": {
    "PRIVATE_TASK_ID": "software-engineering"
  }
}
```

This is a shape example, not a valid map. The real map must contain exactly 40
unique task IDs, five in each of these domains:

- `software-engineering`
- `industrial-physical-systems`
- `natural-science`
- `office-white-collar`
- `finance-economics`
- `mathematics-or-formal-reasoning`
- `cybersecurity`
- `media-content-production`

The map belongs to the organizer's versioned, private benchmark. Neither the
submission nor its author declares a domain. Evaluator suites such as `tb2` and
`lhtb` are not automatically treated as leaderboard domains.

For every task, `reward >= 1.0` is a solved attempt, matching PR #49's
[`PASS_THRESHOLD`](https://github.com/benchflow-ai/posttrainarena/blob/dcdf73a74c7c15b6a792e18dad0a6b21770c6bab/pipelines/benchflow-task-posttrain/src/posttrainarena/benchflow_pipeline/heldout.py#L77-L104).
Fractional rewards are not partial solves. Publication requires exactly one
healthy outcome for every mapped task. Missing/unknown tasks, duplicate attempts,
duplicate JSON keys, non-finite rewards, infrastructure errors, unknown domains,
incorrect counts, and inconsistent pass flags stop publication before a network
request. This adapter does not choose a best attempt or average repeated trials.

## Validate and publish

Validate without credentials or network access:

```sh
python3 scripts/publish_arena_evaluation.py \
  --run-id RUN_ID \
  --outcomes /private/final-evaluation.json \
  --domains /private/heldout-v1-domains.json \
  --dry-run
```

After the administrator has verified the artifact's run and checkpoint:

```sh
python3 scripts/publish_arena_evaluation.py \
  --controller https://PRIVATE_CONTROLLER \
  --run-id RUN_ID \
  --outcomes /private/final-evaluation.json \
  --domains /private/heldout-v1-domains.json \
  --token-file /private/controller-admin-token
```

An existing `ARENA_CONTROLLER_TOKEN` environment variable can replace
`--token-file`. Do not put the credential in a command argument or URL. HTTP is
allowed only for `localhost` or a loopback IP, for an SSH tunnel to the private
controller. HTTPS redirects are also refused so a credential cannot follow a
redirect to another endpoint. This is not the public Space's submission API.
Environment proxy settings are ignored; controller connections are direct so an
HTTP loopback tunnel cannot forward the administrator credential through a proxy.

The authenticated request is `POST /runs/RUN_ID/evaluation` with a JSON body
containing only `benchmark: "heldout-v1"` and `scores`, whose eight domain values
are `{ "solved": INTEGER, "total": 5 }`. No private task identifiers,
instructions, local paths, model metadata, user identity, or dataset values are
sent. Dry-run output contains these same public counts. Controller response
bodies and credentials are never printed.

## Integration boundary

The existing GitHub training pipeline invokes evaluation after training, and its
HF Jobs wrapper optionally invokes a benchmark matrix. This is separate from the
deployed native `posttrain_infra` trainer. A production hook still needs the real
private benchmark runner, its checkpoint handoff, and the native run ID. No such
command or handoff is invented here. Once that integration supplies the validated
artifact and map, it can call this publisher. The trusted controller endpoint
must be deployed before posting; it verifies run eligibility and records user
and dataset provenance independently.

Offline synthetic coverage (no private tasks or remote requests):

```sh
python3 scripts/test_publish_arena_evaluation.py
```
