# Production Trackio integration

This patch updates the backend running at `/data/arena/src/posttrain-infra` on
`204.12.170.253`. That source snapshot is not tracked in this repository; the
repository's `benchflow_pipeline` is a different runtime. Merging this PR does
not deploy the live backend.

The production changes are limited to the metrics sender, controller run
identity, and TRL callback. New runs log into the authenticated participant's
project with their full Arena run ID. The sender uses the dashboard's append-only
`/ingest` endpoint, forwards numeric TRL metrics, and adds optimizer-step wall
time including failed rollout retries. It preserves separate logging events at
the same step while deduplicating HTTP retries. Metrics failures do not stop
training. No CPU simulation, quota exemption, development routing, or sandbox
changes are included.

## Prepare a release

Run as the account that operates the production backend (currently `carrie`).
Python 3.10+ and Git
are required to prepare a release; use the existing backend interpreters to run
it. From this repository at the reviewed commit:

```sh
python3 deploy/trackio/prepare.py \
  --source /data/arena/src/posttrain-infra \
  --github-commit "$(git rev-parse HEAD)" \
  --release-root /data/arena/releases/trackio
```

The result is `/data/arena/releases/trackio/<GitHubSHA>/source` with a
`release.json` that records the commit and file hashes. Every source file must
match `baseline.json` before copying. A mismatch means the live source changed:
rebase and review the patch against that source rather than bypassing the check.
Only explicitly listed source files are copied; credentials, database files and
runtime state are excluded. Existing releases and the original source are never
overwritten. Preparation does not start services or change configuration.

Run the backend regression tests against the prepared source:

```sh
cd /data/arena/releases/trackio/<GitHubSHA>/source
PYTHONPATH=src /data/arena/venvs/qualification/bin/python \
  -m unittest discover -s tests -p 'test_trackio.py' -v
```

## Production configuration

The dashboard changes are already deployed at
[`openenvarena/training`](https://huggingface.co/spaces/openenvarena/training).
Its `TRACKIO_INGEST_TOKEN` is an application credential, not an HF access token.
Provision the matching value through the operator's secret mechanism into a
production-owned file readable only by the production account (`0600`), under a
private directory (`0700`). Do not
reuse the old HF write-token file, print the credential, or put it in Git or
command arguments. Workers receive only a private copy of the ingestion token.

Update only `trackio` in the existing production controller configuration:

```json
{
  "trackio": {
    "url": "https://openenvarena-training.hf.space",
    "project": "arena",
    "token_file": "/data/arena/beta/secrets/trackio-ingest-token"
  }
}
```

`project: "arena"` is retained only for historical links. New projects come from
the verified username. Keep production state, artifacts, model configuration,
Slurm allocation, sandbox credentials, controller port and bridge unchanged.
No additional HF Jobs credential or GPU allocation is needed for metrics.

## Roll out and verify

1. Stop the production bridge using the existing Supervisor configuration at
   `/data/arena/beta/supervisord.conf`. Keep the controller and gateway running
   while queued and active work drains; the participant API is unavailable
   during this maintenance. The baseline has no `submissions_enabled` gate.
   Check the production database as its owner as well as the Slurm queue:
   an empty Slurm queue alone does not prove admission and run queues are empty.
   Back up the production database and service configuration.
   Old staged jobs carry the old HF token and run naming;
   they must finish with the old sender and its existing native Trackio API.
2. Point both the production controller and its Slurm trainer launcher at the
   prepared release. Slurm uses `--export=NONE`: setting the controller's
   `PYTHONPATH` alone does not update GPU workers. The executable selected by
   `slurm.python` must explicitly export the release's `source/src` before
   executing the existing training interpreter. Keep the gateway and bridge on
   their existing configuration.
3. Restart the production controller and bridge as their owning account. Verify
   the controller health and production bridge connection. Do not run the
   `arena-dev` configurator against production.
4. Validate one real GPU training run: its submission must link to
   `?project=<username>&runs=<full-run-id>`, and the dashboard must show reward
   mean/std, loss, gradient norm, completion lengths, `step/wall_seconds` and
   `step/retries`. The CPU test confirms ingestion and UI behavior; it does not
   validate execution of the GPU training stack.

The frontend link changes are already deployed in
[`openenvarena/arena`](https://huggingface.co/spaces/openenvarena/arena).
Keep `ARENA_BACKEND=prod`. Clear `ARENA_DEV_USERS` when `burtenshaw` should return
to production; currently that account is routed to the CPU test backend.
The dashboard keeps its existing storage bucket and `TRACKIO_DIR=/data/trackio`.
Participants need no new settings or tokens.

Completed historical runs keep their original links. If an old run is explicitly
resumed with this release, subsequent metrics use its new per-user identity;
older curves stay in the original project and are not migrated.

To roll back after draining new work, restore both old code paths and the saved
controller configuration (including its old HF-token path), then restart the
controller and bridge. Keep the new dashboard ingestion endpoint and bucket in
place; its native API remains available for old jobs. Do not roll back the
database or delete recorded metrics.

Preparation checks need only Python and Git:

```sh
python3 -m unittest discover -s deploy/trackio -p 'test_prepare.py' -v
```
