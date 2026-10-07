"""Check the reviewed server-source baseline; optionally apply the leaderboard patch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Backend source checkout or a copy for testing')
    parser.add_argument('--apply', action='store_true', help='Apply after all baseline checks pass')
    args = parser.parse_args()
    root = args.source.resolve()
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / 'backend-baseline.json').read_text())
    mismatches = []
    for name, expected in manifest['files'].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            mismatches.append(name)
        elif expected is None:
            if path.exists():
                mismatches.append(name)
        elif not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatches.append(name)
    if mismatches:
        parser.exit(1, 'Source differs from the reviewed baseline; reconcile before applying:\n'
                    + '\n'.join(mismatches) + '\n')
    patch = here / 'backend.patch'
    environment = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(root.parent)}
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=root, env=environment, check=True)
    if args.apply:
        subprocess.run(['git', 'apply', str(patch)], cwd=root, env=environment, check=True)
        print('Leaderboard patch applied.')
    else:
        print('Baseline matches and patch applies. No files changed.')


if __name__ == '__main__':
    main()
