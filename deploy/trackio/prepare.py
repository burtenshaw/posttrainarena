#!/usr/bin/env python3
"""Build an isolated Arena release from an explicitly verified source snapshot."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_file(source, name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or any(part in ("..", ".git") for part in path.parts)
        or str(path) != name
    ):
        raise ValueError(f"invalid manifest path: {name!r}")
    current = source
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"manifest path contains a symlink: {name!r}")
    if not current.is_file():
        raise ValueError(f"missing source file: {name}")
    return current


def prepare(source, release_root, github_commit, manifest_path, patch_path):
    if not re.fullmatch(r"[0-9a-f]{40}", github_commit):
        raise ValueError("github_commit must be a full lowercase Git commit SHA")
    source = source.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    patch_path = patch_path.resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    patch_bytes = patch_path.read_bytes()
    files = json.loads(manifest_bytes)["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("baseline manifest must contain a nonempty files mapping")
    for name, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"invalid baseline digest: {name}")
        if sha256(source_file(source, name)) != expected:
            raise ValueError(f"baseline mismatch: {name}")

    release_root.mkdir(parents=True, exist_ok=True)
    destination = release_root.resolve() / github_commit
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"release already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=release_root) as temporary:
        staging = Path(temporary)
        prepared_source = staging / "source"
        prepared_source.mkdir()
        for name, expected in files.items():
            target = prepared_source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file(source, name), target)
            if sha256(target) != expected:
                raise ValueError(f"source changed while copying: {name}")

        # Prevent Git from finding a surrounding checkout, even when the caller
        # stages releases inside one. The patch only touches the copied files.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env["GIT_CEILING_DIRECTORIES"] = str(staging.resolve())
        for options in (["--check"], []):
            subprocess.run(
                ["git", "apply", *options, "--", "-"],
                cwd=prepared_source,
                env=env,
                check=True,
                capture_output=True,
                input=patch_bytes,
            )

        prepared_files = {}
        for path in sorted(prepared_source.rglob("*")):
            if path.is_symlink():
                raise ValueError("release patches must not create symlinks")
            if path.is_file():
                prepared_files[path.relative_to(prepared_source).as_posix()] = sha256(
                    path
                )
        metadata = {
            "github_commit": github_commit,
            "baseline_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
            "files": prepared_files,
        }
        (staging / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # Exclusive creation prevents replacing an existing release, including
        # another prepare process winning a race. release.json is installed last.
        destination.mkdir()
        try:
            prepared_source.rename(destination / "source")
            (staging / "release.json").rename(destination / "release.json")
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination


def main():
    directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--github-commit", required=True)
    parser.add_argument("--manifest", type=Path, default=directory / "baseline.json")
    parser.add_argument("--patch", type=Path, default=directory / "backend.patch")
    args = parser.parse_args()
    try:
        release = prepare(
            args.source,
            args.release_root,
            args.github_commit,
            args.manifest,
            args.patch,
        )
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"release preparation failed: {error}\n")
    print(release)


if __name__ == "__main__":
    main()
