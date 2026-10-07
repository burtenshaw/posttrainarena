# Trackio error alerts

Training and sandbox cleanup errors are saved with the run and delivered to
Trackio from a durable SQLite queue. Dashboard outages do not block training;
event IDs survive delivery retries and controller restarts. The submission API
exposes `run.alert = {level: "ERROR", message, url}`. The URL selects the run's
Trackio Reports page after ingestion acknowledges the alert; pending delivery
still leaves the message visible in the frontend.

This patch adds error reporting only. Existing training retry, cleanup, deadline,
evaluation, and publication behavior is unchanged. No production deployment is
performed by merging this PR.

## Dependencies

- Backend metrics integration: [#63](https://github.com/benchflow-ai/posttrainarena/pull/63).
- [Trackio alerts](https://huggingface.co/spaces/openenvarena/training/discussions/2)
  adds alert ingestion; deploy it before enabling the backend sender.
- [Frontend error links](https://huggingface.co/spaces/openenvarena/arena/discussions/10)
  consumes the additive `run.alert` field.

`baseline.json` describes the backend source after applying #63.
`backend.patch` adds only alert reporting, storage, delivery, and focused tests.
It does not include the broader recovery changes prepared during investigation.

## Prepare

Use the release preparer from #63 against its already-prepared source tree:

```sh
python3 deploy/trackio/prepare.py \
  --source /path/to/prepared-metrics/source \
  --github-commit REVIEWED_FULL_COMMIT_SHA \
  --release-root /path/to/new/alert/releases \
  --manifest deploy/alerts/baseline.json \
  --patch deploy/alerts/backend.patch
```

All baseline hashes must match. The preparer copies the source into a new release
and never overwrites the deployment. Run its tests from the resulting source:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Use the dedicated ingestion credential and controller/trainer migration procedure
from `deploy/trackio/README.md`. Deploy the dashboard endpoint before the backend,
and load the reviewed backend source in both the controller and newly staged
trainers. CPU tests do not establish live GPU execution or successful deployment.
