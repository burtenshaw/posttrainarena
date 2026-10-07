"""Launch dev services without credentials in arguments or shared environment files."""
import argparse
import os
from pathlib import Path
import sqlite3
import time


def copy_database(source, destination):
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("controller", "gateway", "bridge", "backup"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.role == "backup":
        while True:
            for name in ("arena.db", "gateway-ledger.db"):
                source = args.state_dir / name
                if source.exists():
                    staged = args.root / "backups" / (name + ".tmp")
                    copy_database(source, staged)
                    staged.replace(args.root / "backups" / name)
            time.sleep(60)
    env = {**os.environ, "PYTHONPATH": str(args.release / "source/src"), "PYTHONUNBUFFERED": "1"}
    # A service receives only the secrets that it needs.
    for key in ("HF_TOKEN", "HF_API_KEY", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH", "ARENA_API_TOKEN"):
        env.pop(key, None)
    env["HF_HOME"] = str(args.root / "hf-home")
    env["HF_TOKEN_PATH"] = str(args.root / "hf-home/token")
    config = str(args.root / "config" / (args.role + ".json"))
    if args.role in ("controller", "gateway"):
        name = "arena.db" if args.role == "controller" else "gateway-ledger.db"
        saved = args.root / "backups" / name
        if not (args.state_dir / name).exists() and saved.is_file():
            copy_database(saved, args.state_dir / name)
    if args.role == "controller":
        env["ARENA_API_TOKEN"] = (args.root / "secrets/api-token").read_text().strip()
        command = ["serve", "--config", config, "--database", str(args.state_dir / "arena.db"),
                   "--host", args.host, "--port", "8081"]
    elif args.role == "gateway":
        # Provision separately: fine-grained Jobs permission on openenvarena only.
        env["HF_API_KEY"] = (args.root / "secrets/hf-execution-token").read_text().strip()
        command = ["worker", "--role", "hf-env", "--config", config, "--host", args.host, "--port", "8001"]
    else:
        command = ["bridge", "--config", config]
    python = "/data/arena/venvs/qualification/bin/python"
    os.execve(python, [python, "-m", "posttrain_infra", *command], env)


if __name__ == "__main__":
    main()
