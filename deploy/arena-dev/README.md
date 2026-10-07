# Arena development backend

This directory builds an independent development release from an explicit server
source snapshot. The production runtime is not imported into this repository.
`baseline.json` lists every source file to copy and its SHA-256; `backend.patch`
contains the reviewed changes. Unlisted files, including credentials and runtime
state, are not copied.

Requires Python 3.10+ and Git. From the checked-out GitHub commit, run:

```sh
python3 deploy/arena-dev/prepare.py \
  --source /path/to/server-source-snapshot \
  --github-commit "$(git rev-parse HEAD)" \
  --release-root /data/arena-dev/releases
```

The result is `/data/arena-dev/releases/<GitHubSHA>/source` and `release.json`.
Metadata records the GitHub commit, manifest/patch hashes, and resulting file
hashes. Preparation verifies every baseline file, applies the patch only to its
copy, and refuses to replace an existing release. A release is complete when
`release.json` exists. Preparation does not start processes or configure secrets.

Keep development state separate: `/tmp/arena-dev/sql` for SQLite on local storage,
backups under `/data/arena-dev`, and independent secret files with mode `0600`.
Use the development bridge `/api/openenv/bridge-dev`, controller port `8081` and
gateway port `8001` on `10.0.8.102`. The separate Supervisor configuration is
`/data/arena-dev/supervisord.conf`, with socket `/tmp/arena-dev/supervisor.sock`.

The existing CPU interpreter `/data/arena/venvs/qualification/bin/python`, GPU
interpreter `/data/arena/venvs/training-vllm/bin/python`, and Supervisor executable
`/data/arena/venvs/ops/bin/supervisord` can be shared read-only. A GPU launcher must
set `PYTHONPATH` to the release's source before executing the training interpreter:
Slurm jobs use `--export=NONE`, so the submitting process's environment does not
carry across. The default dev configuration enables submissions with
`deployment=dev` and `dev_cpu_dummy={"steps": 8, "step_seconds": 1.0}`.
These submissions run a short CPU simulation and log synthetic metrics to Trackio.
They do not pull images, read datasets, load models, launch sandboxes, or use GPUs.
The run metadata and UI identify them as CPU tests; they are not scored results.
Use `cpu-test-submission.json` as a request to the existing Space submission API,
changing `submission_id` for each test. The image address is a placeholder that
is never fetched. Existing valid submission forms also work in CPU test mode.

Configure the prepared release, then start its own Supervisor:

```sh
python3 deploy/arena-dev/configure.py \
  --release "/data/arena-dev/releases/$(git rev-parse HEAD)"
/data/arena/venvs/ops/bin/supervisord -c /data/arena-dev/supervisord.conf
/data/arena/venvs/ops/bin/supervisorctl -c /data/arena-dev/supervisord.conf status
```

`configure.py` copies only an explicit allowlist of nonsecret model and runtime
settings from the production controller configuration. It creates independent
credentials, configs, a GPU interpreter wrapper, and process definitions. It
enables CPU test submissions and leaves the gateway stopped. Running it again keeps
the existing dev credentials and restores CPU test mode.
`service.py` reads each process's own secret files into memory; secrets never
appear in arguments. A background process backs up the two dev SQLite databases
once a minute and restores the latest backups if local state is missing.

Configure the frontend Space with `ARENA_BACKEND=prod` and
`ARENA_DEV_USERS=burtenshaw`; the backend remains production for other users.
Set its `ARENA_BRIDGE_DEV_KEY_SHA256` secret to the SHA-256 digest emitted by
`configure.py`. The dev bridge uses `/api/openenv/bridge-dev`; the production
bridge and its key remain separate. Set `ARENA_BACKEND=dev` only when all users
should use dev. Clear `ARENA_DEV_USERS` and set `ARENA_BACKEND=prod` to route
everyone back to production. A disconnected dev bridge never falls back to prod.

On the existing `openenvarena/training` dashboard, set `TRACKIO_INGEST_TOKEN` to
the value in `/data/arena-dev/secrets/trackio-ingest-token`, using a secret-aware
API client without printing it. The dashboard needs its `/ingest` code deployed
and continues using its existing bucket mount. Workers receive only this
append-only credential, never an HF repository token.

Before switching to real training, pause submissions with `submissions_enabled=false`.
Provision a dedicated fine-grained HF
token in `/data/arena-dev/secrets/hf-execution-token` (mode `0600`) with only
`job.write`, `org.read`, and `org.billing.read` on `openenvarena`. Obtain authorized
Slurm GPU access for the deploying account; do not bypass the current reservation
or set `allow_broad_token`. Enable the gateway's `autostart` and start it, then
check its authenticated `/health` and the account's allocation permissions.
Only after those checks, remove `dev_cpu_dummy`, set `submissions_enabled=true`
in the dev controller config, and restart `arena-dev-controller`. The gateway uses
`posttrain-role=native-openenv-dev` to keep cleanup separate from production.

To stop this deployment, use its own Supervisor config:

```sh
/data/arena/venvs/ops/bin/supervisorctl -c /data/arena-dev/supervisord.conf shutdown
```

Run the preparation checks without external Python packages:

```sh
python3 -m unittest discover -s deploy/arena-dev -p 'test_*.py'
```
