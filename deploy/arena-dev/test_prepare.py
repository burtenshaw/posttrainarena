import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "prepare", Path(__file__).with_name("prepare.py")
)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "snapshot"
        (self.source / "src").mkdir(parents=True)
        self.original = b"answer = 1\n"
        (self.source / "src/app.py").write_bytes(self.original)
        # This file is deliberately absent from the explicit source manifest.
        (self.source / ".env").write_text("PRIVATE_SETTING=fixture\n")
        self.manifest = self.root / "baseline.json"
        self.write_manifest({"src/app.py": hashlib.sha256(self.original).hexdigest()})
        self.patch = self.root / "backend.patch"
        self.patch.write_text(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"
        )
        self.releases = self.root / "releases"
        self.commit = "a" * 40

    def write_manifest(self, files):
        self.manifest.write_text(json.dumps({"files": files}))

    def build(self):
        return prepare.prepare(
            self.source, self.releases, self.commit, self.manifest, self.patch
        )

    def test_release_changes_only_explicit_copies_and_records_provenance(self):
        release = self.build()
        self.assertEqual((release / "source/src/app.py").read_text(), "answer = 2\n")
        self.assertEqual((self.source / "src/app.py").read_bytes(), self.original)
        self.assertFalse((release / "source/.env").exists())
        metadata = json.loads((release / "release.json").read_text())
        self.assertEqual(metadata["github_commit"], self.commit)
        self.assertEqual(metadata["patch_sha256"], prepare.sha256(self.patch))
        self.assertEqual(
            metadata["files"],
            {"src/app.py": hashlib.sha256(b"answer = 2\n").hexdigest()},
        )

    def test_baseline_mismatch_does_not_create_release(self):
        (self.source / "src/app.py").write_text("answer = 3\n")
        with self.assertRaisesRegex(ValueError, "baseline mismatch"):
            self.build()
        self.assertFalse(self.releases.exists())

    def test_existing_release_is_not_overwritten(self):
        release = self.build()
        metadata = (release / "release.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual((release / "release.json").read_bytes(), metadata)

    def test_manifest_cannot_escape_snapshot_or_follow_symlinks(self):
        digest = hashlib.sha256(self.original).hexdigest()
        for name in ("../snapshot/src/app.py", "/src/app.py", "src/../src/app.py"):
            self.write_manifest({name: digest})
            with self.assertRaisesRegex(ValueError, "invalid manifest path"):
                self.build()
        (self.source / "link.py").symlink_to(self.source / "src/app.py")
        self.write_manifest({"link.py": digest})
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build()

    def test_bad_patch_leaves_snapshot_and_releases_unchanged(self):
        self.patch.write_text(
            self.patch.read_text().replace("answer = 1", "answer = 9")
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertEqual((self.source / "src/app.py").read_bytes(), self.original)
        self.assertEqual(list(self.releases.iterdir()), [])

    def test_patch_cannot_write_outside_release(self):
        self.patch.write_text(
            "diff --git a/../escaped.py b/../escaped.py\n"
            "new file mode 100644\n--- /dev/null\n+++ b/../escaped.py\n"
            "@@ -0,0 +1 @@\n+escaped = True\n"
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertFalse((self.releases / "escaped.py").exists())
        self.assertEqual(list(self.releases.iterdir()), [])

    def test_patch_does_not_use_surrounding_git_checkout(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        release = self.build()
        self.assertEqual((release / "source/src/app.py").read_text(), "answer = 2\n")


if __name__ == "__main__":
    unittest.main()
