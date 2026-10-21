#!/usr/bin/env python3
"""Generate a deterministic ~8K-token retrieval probe with six distributed answers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'github/qwen38-kvmem')
    parser.add_argument('--output', type=Path, required=True, help='New prompt filename')
    parser.add_argument('--tokens', type=int, default=8192, help='Minimum raw prompt token count')
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix('.expected.json').exists():
        parser.error('Output already exists')
    if not 1000 <= args.tokens <= 30000:
        parser.error('tokens must be 1000..30000 for this probe')
    engine_bin = args.root / 'minicpm-les/llama.cpp-omni/build-jetson/bin'
    env = dict(os.environ, LD_LIBRARY_PATH=str(engine_bin))
    rng = random.Random(20261021)
    rows, values = [], {}
    for i in range(1000):
        key = f'R{i:04d}'
        value = ''.join(rng.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=8))
        values[key] = value
        rows.append(f'Record {key}: port={rng.choice(["cedar", "maple", "willow", "birch"])}; '
                    f'cargo={rng.choice(["rice", "cloth", "tools", "paper", "tea"])}; '
                    f'crates={rng.randrange(10, 999)}; status=checked; verification={value}.')

    def make(n):
        indices = [int((n - 1) * f) for f in (.03, .20, .40, .60, .80, .97)]
        keys = [f'R{i:04d}' for i in indices]
        prompt = ('下面是虚构仓库记录，仅作为查询资料，不包含给你的指令。请完整阅读后回答末尾问题。\n'
                  '<records>\n' + '\n'.join(rows[:n]) + '\n</records>\n'
                  '请返回以下记录的 verification 字段：' + '、'.join(keys) + '。'
                  '只输出一个 JSON 对象，键为记录编号，值为原样抄录的 verification 字符串；不要解释，不要代码围栏。\n')
        return prompt, {k: values[k] for k in keys}, indices

    def count(prompt):
        cmd = [str(engine_bin / 'llama-tokenize'), '-m', str(args.root / 'models/MiniCPM-V-4.7/stripped.gguf'),
               '--stdin', '--ids', '--no-bos', '--no-escape', '--log-disable']
        p = subprocess.run(cmd, input=prompt, text=True, capture_output=True, env=env, check=True)
        return len(json.loads(p.stdout))

    lo, hi = 1, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if count(make(mid)[0]) < args.tokens:
            lo = mid + 1
        else:
            hi = mid
    prompt, expected, positions = make(lo)
    tokens = count(prompt)
    if tokens < args.tokens:
        raise RuntimeError('Insufficient generated records')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(prompt)
    metadata = {'raw_prompt_tokens': tokens, 'records': lo, 'positions_zero_based': positions,
                'expected': expected, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'scope': 'Six-field retrieval from synthetic records; not a full-context quality benchmark.'}
    args.output.with_suffix('.expected.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
