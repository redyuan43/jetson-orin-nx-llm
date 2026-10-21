#!/usr/bin/env python3
"""Run a measured MiniCPM request with the validated nx5 8K CUDA/DSpark preset."""
import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt', type=Path, help='UTF-8 prompt file')
    parser.add_argument('--tag', default='fast-8k-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    parser.add_argument('--root', type=Path, default=Path.home() / 'github/qwen38-kvmem')
    parser.add_argument('--library-dir', type=Path, default=repo / 'optimization/cuda-mapped-v2')
    parser.add_argument('--output', type=Path, default=repo / 'logs/fast-8k')
    parser.add_argument('--tokens', type=int, default=6144)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    templates = [repo / 'reports/2026-10-21-nx5-quality/official-chat_template.jinja',
                 repo / 'quality/official-chat_template.jinja']
    template = next((p for p in templates if p.is_file()), None)
    if template is None:
        parser.error('Official chat template missing; restore the audited template before running')
    cmd = [sys.executable, str(repo / 'scripts/minicpm/bench-audit.py'), args.tag,
           '--root', str(args.root), '--prompt', str(args.prompt), '--output', str(args.output),
           '--ctx', '8192', '--window', '200', '--bank', '64', '--rows', '8',
           '--tokens', str(args.tokens), '--seed', str(args.seed), '--draft', '7',
           '--mapped-experts', '--library-dir', str(args.library_dir),
           '--sampling', 'instruct', '--reasoning', 'off', '--chat-template-file', str(template)]
    print(f'8K CUDA/DSpark request; output prefix: {args.output / args.tag}', flush=True)
    return subprocess.call(cmd)


if __name__ == '__main__':
    raise SystemExit(main())
