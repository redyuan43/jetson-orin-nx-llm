#!/usr/bin/env python3
"""Run the pinned nx5 64K profile, checking the tested libraries and chat template."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt', type=Path, help='UTF-8 prompt file')
    parser.add_argument('--tag', default='64k-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    parser.add_argument('--root', type=Path, default=Path.home() / 'github/qwen38-kvmem')
    parser.add_argument('--output', type=Path, default=repo / 'logs/64k')
    parser.add_argument('--tokens', type=int, default=6144)
    parser.add_argument('--seed', type=int, help='Override the profile seed; all other sampling parameters stay pinned')
    args = parser.parse_args()
    profile_path = repo / 'configs/minicpm-v47-64k.json'
    profile = json.loads(profile_path.read_text())
    library_dir = repo / profile['library_dir']
    template = repo / profile['chat_template']
    checks = {library_dir / name: digest for name, digest in profile['library_sha256'].items()}
    checks[template] = profile['chat_template_sha256']
    for path, expected in checks.items():
        if not path.is_file() or sha256(path) != expected:
            parser.error(f'Tested runtime/template missing or changed: {path}')
    parameters = dict(profile['parameters'])
    if args.seed is not None:
        parameters['seed'] = args.seed
    cmd = [sys.executable, str(repo / 'scripts/minicpm/bench-audit.py'), args.tag,
           '--root', str(args.root), '--prompt', str(args.prompt), '--output', str(args.output),
           '--tokens', str(args.tokens), '--library-dir', str(library_dir),
           '--chat-template-file', str(template)]
    for name, value in parameters.items():
        option = '--' + name.replace('_', '-')
        if isinstance(value, bool):
            if value:
                cmd.append(option)
        else:
            cmd.extend([option, str(value)])
    print(f'Profile: {profile_path} ({sha256(profile_path)})', flush=True)
    print(f'Output prefix: {args.output / args.tag}', flush=True)
    return subprocess.call(cmd)


if __name__ == '__main__':
    raise SystemExit(main())
