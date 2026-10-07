"""Configure an isolated Arena dev deployment; never read production secrets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex


def write(path, content, mode=0o600):
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as stream:
        stream.write(content)
    path.chmod(mode)


def private_directory(path):
    if path.is_symlink():
        raise ValueError(f"Refusing directory symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("Dev directories must belong to the deploying user")
    path.chmod(0o700)


def configure(root, release, production_config, host, state_dir):
    for path in (root, state_dir.parent, state_dir):
        if path.is_symlink():
            raise ValueError(f"Refusing directory symlink: {path}")
    root, release, state_dir = root.resolve(), release.resolve(), state_dir.resolve()
    private_directory(root)
    for name in ("secrets", "config", "bin", "logs", "shared", "artifacts", "transfers", "backups", "hf-home"):
        private_directory(root / name)
    private_directory(state_dir.parent)
    private_directory(state_dir)
    if not (release / "source/src/posttrain_infra/controller.py").is_file():
        raise ValueError("Prepared source release is missing")
    credentials = {}
    for name in ("api-token", "delegate-token", "bridge-key", "gateway-key", "trackio-ingest-token"):
        path = root / "secrets" / name
        if not path.exists():
            write(path, secrets.token_urlsafe(48) + "\n")
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError("Dev credentials must be private files owned by the deploying user")
        credentials[name] = path.read_text().strip()
        if len(credentials[name]) < 32 or not credentials[name].isascii() or any(c.isspace() for c in credentials[name]):
            raise ValueError("Invalid dev credential file")

    # Explicit allowlist: production token hashes, credentials and state paths are never copied.
    original = json.loads(production_config.read_text())
    allowed = ("approved_models", "submission_run", "environment_limits", "model_cache", "model_loading_s",
               "artifact_max_bytes", "artifact_transfer_timeout_s", "cleanup_stall_s")
    controller = {key: original[key] for key in allowed}
    controller.update(provider="slurm", submission_mode="image", deployment="dev",
        dev_cpu_dummy={"steps": 8, "step_seconds": 1.0}, submissions_enabled=True,
        controller_url=f"http://{host}:8081", artifact_root=str(root / "artifacts"),
        transfer_directory=str(root / "transfers"), http_workers=16, artifact_transfer_workers=4,
        admission_concurrency=1, admission_episode_concurrency=1, admission_queue_s=3600,
        admission_wall_s=14400, max_concurrent_runs=1, run_queue_s=7200,
        submission_quota={"limit": 1, "window_s": 86400}, submission_quota_exempt_subjects=["space:burtenshaw"],
        client_tokens={hashlib.sha256(credentials["delegate-token"].encode()).hexdigest():
                       {"subject": "space", "roles": ["delegate"]}},
        trackio={"url": "https://openenvarena-training.hf.space", "token_file": str(root / "secrets/trackio-ingest-token")})
    slurm_allowed = ("partition", "gpu_type", "timeout_s", "lookback_days", "trainer_cpus", "trainer_memory_mb",
                     "trainer_generator", "max_cpus", "max_memory_mb", "max_wall_s")
    controller["slurm"] = {key: original["slurm"][key] for key in slurm_allowed}
    controller["slurm"].update(shared_directory=str(root / "shared"), python=str(root / "bin/trainer-python"), max_gpus=1)
    accounting_keys = ("cpu_vcpus", "memory_gib", "storage_gib", "episode_wall_s", "operation_wall_s", "startup_wall_s")
    accounting = {key: original["hf_environment"]["remote_accounting"][key] for key in accounting_keys}
    accounting["max_concurrent_envs"] = 6
    controller["hf_environment"] = {"url": f"http://{host}:8001", "flavor": "cpu-basic",
        "shared_secret_file": str(root / "secrets/gateway-key"), "remote_accounting": accounting}
    gateway = {"mode": "native", "deployment": "dev", "namespace": "openenvarena", "flavor": "cpu-basic",
        **{key: accounting[key] for key in ("max_concurrent_envs", "startup_wall_s", "operation_wall_s", "episode_wall_s")},
        "management": {"ledger_path": str(state_dir / "gateway-ledger.db"),
                       "shared_secret_file": str(root / "secrets/gateway-key")},
        "execution_identity": {"permissions": ["job.write", "org.read", "org.billing.read"]}}
    bridge = {"front_end": "wss://openenvarena-arena.hf.space/api/openenv/bridge-dev",
        "controller": f"http://{host}:8081", "key_file": str(root / "secrets/bridge-key"),
        "delegate_token_file": str(root / "secrets/delegate-token"), "max_inflight": 8, "request_timeout_s": 300}
    for name, value in (("controller", controller), ("gateway", gateway), ("bridge", bridge)):
        write(root / "config" / f"{name}.json", json.dumps(value, indent=2) + "\n")
    write(root / "bin/trainer-python", "#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(release / "source/src")) +
          '\nexec /data/arena/venvs/training-vllm/bin/python "$@"\n', 0o700)
    service = Path(__file__).with_name("service.py").resolve()
    socket = state_dir.parent / "supervisor.sock"
    conf = f"""[unix_http_server]
file={socket}
chmod=0700
[supervisord]
logfile={root}/logs/supervisord.log
pidfile={state_dir.parent}/supervisord.pid
childlogdir={root}/logs
umask=077
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{socket}
"""
    for role, priority in (("gateway", 10), ("controller", 20), ("bridge", 25), ("backup", 30)):
        command = shlex.join(["/data/arena/venvs/qualification/bin/python", str(service), role,
                              "--root", str(root), "--release", str(release), "--state-dir", str(state_dir), "--host", host])
        conf += f"""
[program:arena-dev-{role}]
command={command}
directory={root}
priority={priority}
autostart={'false' if role == 'gateway' else 'true'}
autorestart=true
startsecs=5
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile={root}/logs/{role}.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
"""
    write(root / "supervisord.conf", conf)
    return {"root": str(root), "release": str(release), "submissions_enabled": True, "training_mode": "cpu-dummy",
            "bridge_key_sha256": hashlib.sha256(credentials["bridge-key"].encode()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/arena-dev"))
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--production-config", type=Path, default=Path("/data/arena/beta/config/controller.json"))
    parser.add_argument("--host", default="10.0.8.102")
    parser.add_argument("--state-dir", type=Path, default=Path("/tmp/arena-dev/sql"))
    args = parser.parse_args()
    print(json.dumps(configure(args.root, args.release, args.production_config, args.host, args.state_dir)))
