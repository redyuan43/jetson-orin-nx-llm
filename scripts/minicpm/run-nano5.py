#!/usr/bin/env python3
"""Run a measured MiniCPM profile on the 8GB nano5 deployment."""
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
    parser.add_argument('--profile', choices=('fast-8k', 'long-64k', 'vision-8k'), default='long-64k')
    parser.add_argument('--image', type=Path, help='One local image with the vision-8k profile')
    parser.add_argument('--root', type=Path, default=Path.home() / 'minicpm-v47')
    parser.add_argument('--output', type=Path, default=repo / 'logs/nano5')
    parser.add_argument('--tag', default='nano5-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    parser.add_argument('--tokens', type=int, default=6144)
    parser.add_argument('--seed', type=int, help='Override the pinned sampling seed')
    args = parser.parse_args()
    if bool(args.image) != (args.profile == 'vision-8k'):
        parser.error('--image and --profile vision-8k must be used together')
    profile_path = repo / 'configs/minicpm-v47-nano5.json'
    config = json.loads(profile_path.read_text())
    library_dir = args.root / config['library_dir']
    template = repo / config['chat_template']
    checks = {library_dir / name: digest for name, digest in config['library_sha256'].items()}
    checks[template] = config['chat_template_sha256']
    checks[args.root / config['cli']] = config['cli_sha256']
    for path, expected in checks.items():
        if not path.is_file() or sha256(path) != expected:
            parser.error(f'Tested runtime/template missing or changed: {path}')
    parameters = {**config['common_parameters'], **config['profiles'][args.profile]['parameters']}
    if args.seed is not None:
        parameters['seed'] = args.seed
    cmd = [sys.executable, str(repo / 'scripts/minicpm/bench-audit.py'), args.tag,
           '--root', str(args.root), '--prompt', str(args.prompt), '--output', str(args.output),
           '--tokens', str(args.tokens), '--library-dir', str(library_dir),
           '--chat-template-file', str(template)]
    if args.image:
        projector = args.root / config['vision_projector']['path']
        if not projector.is_file() or projector.stat().st_size != config['vision_projector']['size']:
            parser.error('Verified vision projector missing or has the wrong size')
        cmd += ['--mmproj', str(projector), '--image', str(args.image)]
    for name, value in parameters.items():
        option = '--' + name.replace('_', '-')
        if isinstance(value, bool):
            if value:
                cmd.append(option)
        else:
            cmd.extend([option, str(value)])
    print(f'Profile: {args.profile} ({sha256(profile_path)})', flush=True)
    print(f'Output prefix: {args.output / args.tag}', flush=True)
    return subprocess.call(cmd)


if __name__ == '__main__':
    raise SystemExit(main())
