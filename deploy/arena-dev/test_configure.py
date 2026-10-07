import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


configure = load("configure")
service = load("service")


class ExecCaptured(Exception):
    pass


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "dev"
        self.release = self.directory / "release with spaces"
        self.source = self.release / "source/src/posttrain_infra/controller.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("# immutable prepared source\n")
        self.state = self.directory / "runtime/sql"
        self.production_config = self.directory / "production/config/controller.json"
        self.production_config.parent.mkdir(parents=True)
        self.production_database = self.production_config.parent.parent / "arena.db"
        self.database(self.production_database, "production")
        self.original = {
            "approved_models": [{"model_id": "test/model", "revision": "a" * 40}],
            "submission_run": {"model_id": "test/model", "num_updates": 2},
            "environment_limits": {"max_tasks": 50},
            "model_cache": "/shared/read-only/model-cache",
            "model_loading_s": 600,
            "artifact_max_bytes": 1000000,
            "artifact_transfer_timeout_s": 300,
            "cleanup_stall_s": 300,
            "provider": "production-provider",
            "submissions_enabled": True,
            "database": str(self.production_database),
            "client_tokens": {"production-token-hash": {"roles": ["admin"]}},
            "api_token": "production-api-secret",
            "trackio": {"token_file": "/production/secrets/dashboard"},
            "slurm": {
                "partition": "gpu", "gpu_type": "h200", "timeout_s": 30,
                "lookback_days": 7, "trainer_cpus": 4, "trainer_memory_mb": 32000,
                "trainer_generator": "vllm", "max_cpus": 32,
                "max_memory_mb": 256000, "max_wall_s": 14400,
                "max_gpus": 8, "shared_directory": "/production/shared",
                "python": "/production/bin/trainer-python", "secret": "production-slurm-secret",
            },
            "hf_environment": {
                "url": "http://production:8000",
                "shared_secret_file": "/production/secrets/gateway",
                "token": "production-hf-secret",
                "remote_accounting": {
                    "cpu_vcpus": 2, "memory_gib": 16, "storage_gib": 50,
                    "max_concurrent_envs": 24, "startup_wall_s": 120,
                    "operation_wall_s": 120, "episode_wall_s": 3600,
                },
            },
        }
        self.write_original()
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)

    def write_original(self):
        self.production_config.write_text(json.dumps(self.original))

    def build(self):
        return configure.configure(self.root, self.release, self.production_config, "10.0.8.102", self.state)

    def config(self, role):
        return json.loads((self.root / "config" / (role + ".json")).read_text())

    @staticmethod
    def database(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES (?)", (value,))

    @staticmethod
    def value(path):
        with sqlite3.connect(path) as connection:
            return connection.execute("SELECT value FROM marker").fetchone()[0]

    def invoke(self, role, **environment):
        args = ["service.py", role, "--root", str(self.root), "--release", str(self.release),
                "--state-dir", str(self.state), "--host", "10.0.8.102"]
        with patch("sys.argv", args), patch.dict(os.environ, environment), \
             patch.object(service.os, "execve", side_effect=ExecCaptured) as execute:
            with self.assertRaises(ExecCaptured):
                service.main()
        return execute.call_args.args

    def test_configuration_copies_only_allowed_settings_and_keeps_production_untouched(self):
        before_config = self.production_config.read_bytes()
        before_source = self.source.read_bytes()
        before_db = self.production_database.read_bytes()
        self.build()
        controller = self.config("controller")
        self.assertEqual(controller["approved_models"], self.original["approved_models"])
        self.assertEqual(controller["model_cache"], self.original["model_cache"])
        self.assertEqual(controller["submission_run"], self.original["submission_run"])
        output = "\n".join(path.read_text() for path in (self.root / "config").glob("*.json"))
        for forbidden in ("production-token-hash", "production-api-secret", "production-hf-secret",
                          "production-slurm-secret", "/production/", str(self.production_database)):
            self.assertNotIn(forbidden, output)
        self.assertEqual(self.production_config.read_bytes(), before_config)
        self.assertEqual(self.production_database.read_bytes(), before_db)
        self.assertEqual(self.source.read_bytes(), before_source)
        self.assertFalse((self.state / "arena.db").exists())

    def test_nested_remote_accounting_is_allowlisted(self):
        self.original["hf_environment"]["remote_accounting"]["secret_file"] = "/production/secrets/account"
        self.write_original()
        self.build()
        accounting = self.config("controller")["hf_environment"]["remote_accounting"]
        self.assertNotIn("secret_file", accounting)
        self.assertEqual(accounting["max_concurrent_envs"], 6)

    def test_credentials_are_private_unique_and_stable_on_reconfiguration(self):
        result = self.build()
        paths = list((self.root / "secrets").iterdir())
        self.assertEqual({path.name for path in paths}, {"api-token", "delegate-token", "bridge-key", "gateway-key", "trackio-ingest-token"})
        credentials = {path.name: path.read_text().strip() for path in paths}
        self.assertEqual(len(set(credentials.values())), len(credentials))
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(credentials[path.name], json.dumps(result))
        self.assertEqual(result["bridge_key_sha256"], hashlib.sha256(credentials["bridge-key"].encode()).hexdigest())
        self.build()
        self.assertEqual({path.name: path.read_text().strip() for path in paths}, credentials)
        for path in (self.root / "config").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_runtime_directories_are_private(self):
        for path in (self.root, self.root / "secrets", self.root / "config", self.state):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)
        self.build()
        for path in (self.root, self.root / "secrets", self.root / "config", self.state):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))

    def test_symlinked_secrets_or_state_cannot_redirect_into_another_deployment(self):
        external = self.directory / "external"
        external.mkdir()
        self.root.mkdir()
        (self.root / "secrets").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(list(external.iterdir()), [])
        (self.root / "secrets").unlink()
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(list(external.iterdir()), [])

    def test_initial_services_use_independent_ports_paths_and_cpu_simulation(self):
        result = self.build()
        controller, gateway, bridge = (self.config(role) for role in ("controller", "gateway", "bridge"))
        self.assertTrue(result["submissions_enabled"])
        self.assertEqual(result["training_mode"], "cpu-dummy")
        self.assertTrue(controller["submissions_enabled"])
        self.assertEqual(controller["deployment"], "dev")
        self.assertEqual(controller["dev_cpu_dummy"], {"steps": 8, "step_seconds": 1.0})
        self.assertEqual(controller["submission_quota_exempt_subjects"], ["space:burtenshaw"])
        self.assertEqual(controller["controller_url"], "http://10.0.8.102:8081")
        self.assertEqual(controller["hf_environment"]["url"], "http://10.0.8.102:8001")
        self.assertEqual(controller["slurm"]["shared_directory"], str(self.root / "shared"))
        self.assertEqual(controller["slurm"]["max_gpus"], 1)
        self.assertEqual(bridge["front_end"], "wss://openenvarena-arena.hf.space/api/openenv/bridge-dev")
        self.assertEqual(gateway["management"]["ledger_path"], str(self.state / "gateway-ledger.db"))
        wrapper = self.root / "bin/trainer-python"
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
        self.assertEqual(shlex.split(wrapper.read_text().splitlines()[1]), ["export", "PYTHONPATH=" + str(self.release / "source/src")])
        self.assertIn('exec /data/arena/venvs/training-vllm/bin/python "$@"', wrapper.read_text())
        supervisor = configparser.ConfigParser(interpolation=None)
        supervisor.read(self.root / "supervisord.conf")
        self.assertFalse(supervisor.getboolean("program:arena-dev-gateway", "autostart"))
        self.assertEqual(supervisor["unix_http_server"]["file"], str(self.state.parent / "supervisor.sock"))
        for role in ("controller", "gateway", "bridge", "backup"):
            command = shlex.split(supervisor["program:arena-dev-" + role]["command"])
            self.assertEqual(command[0], "/data/arena/venvs/qualification/bin/python")
            self.assertIn(str(self.release), command)

    def test_service_roles_receive_only_their_own_credentials(self):
        self.build()
        (self.root / "secrets/hf-execution-token").write_text("dev-execution-token")
        inherited = {"HF_TOKEN": "production-token", "HF_API_KEY": "production-api-key",
                     "ARENA_API_TOKEN": "production-admin", "HUGGING_FACE_HUB_TOKEN": "production-legacy-token",
                     "HF_TOKEN_PATH": "/production/token", "HF_HOME": "/production/hf-home"}
        for role in ("controller", "gateway", "bridge"):
            executable, arguments, env = self.invoke(role, **inherited)
            self.assertEqual(executable, "/data/arena/venvs/qualification/bin/python")
            self.assertEqual(arguments[1:3], ["-m", "posttrain_infra"])
            self.assertEqual(env["PYTHONPATH"], str(self.release / "source/src"))
            self.assertNotIn("HF_TOKEN", env)
            self.assertNotIn("HUGGING_FACE_HUB_TOKEN", env)
            self.assertNotEqual(env.get("HF_TOKEN_PATH"), "/production/token")
            self.assertNotEqual(env.get("HF_HOME"), "/production/hf-home")
            self.assertTrue(Path(env["HF_HOME"]).is_relative_to(self.root))
            if role == "controller":
                self.assertEqual(env["ARENA_API_TOKEN"], (self.root / "secrets/api-token").read_text().strip())
                self.assertNotIn("HF_API_KEY", env)
                self.assertEqual(arguments[-2:], ["--port", "8081"])
            elif role == "gateway":
                self.assertEqual(env["HF_API_KEY"], "dev-execution-token")
                self.assertNotIn("ARENA_API_TOKEN", env)
                self.assertEqual(arguments[-2:], ["--port", "8001"])
            else:
                self.assertNotIn("HF_API_KEY", env)
                self.assertNotIn("ARENA_API_TOKEN", env)

    def test_services_restore_only_their_own_missing_database(self):
        self.build()
        (self.root / "secrets/hf-execution-token").write_text("dev-execution-token")
        for name, marker in (("arena.db", "dev-controller"), ("gateway-ledger.db", "dev-gateway"), ("unrelated.db", "unrelated")):
            self.database(self.root / "backups" / name, marker)
        self.invoke("bridge")
        self.assertEqual(list(self.state.iterdir()), [])
        self.invoke("controller")
        self.assertEqual(self.value(self.state / "arena.db"), "dev-controller")
        self.assertFalse((self.state / "gateway-ledger.db").exists())
        with sqlite3.connect(self.state / "arena.db") as connection:
            connection.execute("UPDATE marker SET value = 'newer-dev'")
        self.invoke("controller")
        self.assertEqual(self.value(self.state / "arena.db"), "newer-dev")
        self.invoke("gateway")
        self.assertEqual(self.value(self.state / "gateway-ledger.db"), "dev-gateway")
        self.assertFalse((self.state / "unrelated.db").exists())
        self.assertEqual(self.value(self.production_database), "production")

    def test_backup_copies_only_dev_databases(self):
        self.build()
        for name in ("arena.db", "gateway-ledger.db", "unrelated.db"):
            self.database(self.state / name, "dev-" + name)
        args = ["service.py", "backup", "--root", str(self.root), "--release", str(self.release),
                "--state-dir", str(self.state), "--host", "10.0.8.102"]
        with patch("sys.argv", args), patch.object(service.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                service.main()
        self.assertEqual({path.name for path in (self.root / "backups").iterdir()}, {"arena.db", "gateway-ledger.db"})
        for name in ("arena.db", "gateway-ledger.db"):
            self.assertEqual(self.value(self.root / "backups" / name), "dev-" + name)
            self.assertEqual(stat.S_IMODE((self.root / "backups" / name).stat().st_mode), 0o600)
        self.assertEqual(self.value(self.production_database), "production")


if __name__ == "__main__":
    unittest.main()
