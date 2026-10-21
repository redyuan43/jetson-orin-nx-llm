#!/usr/bin/env python3
"""Run one exclusive, measured LES/DSpark trial on nx5; never kill other jobs."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


def memory():
    return {k: int(v.split()[0]) for k, v in
            (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}


def vmstat():
    return {key: int(value) for key, value in
            (line.split() for line in Path('/proc/vmstat').read_text().splitlines())
            if key.startswith(('allocstall_', 'pgscan_', 'pgsteal_', 'compact_'))
            or key in ('oom_kill', 'pgmajfault')}


def stop(child):
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag', help='Unique output basename')
    parser.add_argument('--root', type=Path, default=Path.home() / 'github/qwen38-kvmem')
    parser.add_argument('--prompt', type=Path, default=Path(__file__).resolve().parents[2] / 'samples/pelican-prompt.txt')
    parser.add_argument('--output', type=Path, default=Path('logs/minicpm-audit'))
    parser.add_argument('--ctx', type=int, default=8192)
    parser.add_argument('--window', type=int, default=192)
    parser.add_argument('--bank', type=int, default=400)
    parser.add_argument('--rows', type=int, default=8)
    parser.add_argument('--tokens', type=int, default=2048)
    parser.add_argument('--draft', type=int, choices=range(8), default=0,
                        help='0 disables DSpark; otherwise draft length, at most ubatch - 1')
    parser.add_argument('--prefetch', type=int, choices=range(17), default=0,
                        help='Lookahead depth; adds this many expert slots per layer')
    parser.add_argument('--matmul-split', type=int, choices=range(8), default=0,
                        help='Number of trailing expert positions with deferred reads')
    parser.add_argument('--io-threads', type=int, default=8)
    parser.add_argument('--no-cuda-graphs', action='store_true')
    parser.add_argument('--mapped-experts', action='store_true',
                        help='Use experimental patch 0003 in the supplied library directory')
    parser.add_argument('--offload-min-batch', type=int, default=32,
                        help='CUDA offload threshold for operations with host weights')
    parser.add_argument('--log-verbosity', type=int, default=3)
    parser.add_argument('--threads', type=int, choices=range(1, 9),
                        help='Explicit CPU compute threads; omitted uses engine default')
    parser.add_argument('--ctx-checkpoints', type=int,
                        help='Bound ordinary prompt checkpoints; independent of speculative rollback')
    parser.add_argument('--cache-ram-mib', type=int,
                        help='Prompt-cache RAM budget, 0 disables it; omitted uses engine default')
    parser.add_argument('--sampling', choices=['benchmark', 'instruct'], default='benchmark',
                        help='Greedy benchmark or documented MiniCPM-V 4.7 instruct sampling')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--reasoning', choices=['auto', 'on', 'off'], default='auto')
    parser.add_argument('--chat-template-file', type=Path)
    parser.add_argument('--library-dir', type=Path, help='Directory with an isolated patched libllama.so.0')
    parser.add_argument('--min-available-mib', type=int, default=384)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.tag) or args.tag in {'.', '..'}:
        parser.error('tag must be a plain basename')
    if not (1 <= args.rows <= 8 and 8 * args.rows <= args.window <= 256):
        parser.error('rows must be 1..8 and window must hold the worst-case expert union')
    if min(args.ctx, args.tokens, args.timeout, args.min_available_mib) <= 0:
        parser.error('sizes and limits must be positive')
    if args.bank < 64:
        parser.error('bank must hold 8 tokens x 8 experts for the fixed ubatch=8')
    if args.io_threads < 1 or args.io_threads > 32:
        parser.error('io-threads must be 1..32')
    if args.offload_min_batch < 1:
        parser.error('offload-min-batch must be positive')
    if any(v is not None and v < 0 for v in (args.ctx_checkpoints, args.cache_ram_mib)):
        parser.error('checkpoint count and cache RAM budget must be non-negative')
    if args.mapped_experts and (args.library_dir is None or not (args.library_dir / 'libggml-cuda.so.0').exists()):
        parser.error('mapped-experts requires a library directory with patch 0003')
    if args.bank < args.window + args.prefetch and args.library_dir is None:
        parser.error('bank < window crashes the original runtime; supply a library with patch 0002')

    args.output.mkdir(parents=True, exist_ok=True)
    prefix = args.output / args.tag
    for suffix in ('.out', '.err', '.json', '.memory.jsonl'):
        if Path(str(prefix) + suffix).exists():
            parser.error('output already exists; use a new tag')
    args.prompt = args.prompt.resolve(strict=True)
    base = args.root / 'minicpm-les/llama.cpp-omni'
    model = args.root / 'models/MiniCPM-V-4.7'
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('LLAMA_MOE_', 'LLAMA_DRAFT_', 'LLAMA_ARG_', 'GGML_CUDA_', 'GGML_OP_'))}
    env.update(LD_LIBRARY_PATH=str(base / 'build-jetson/bin'),
               LLAMA_MOE_PREWARM=str(args.root / 'minicpm/prewarm_top128.txt'),
               LLAMA_MOE_BANK_CAP=str(max(args.window, (args.bank + 39) // 40)), LLAMA_MOE_DECODE_WINDOW=str(args.window),
               LLAMA_MOE_IO_THREADS=str(args.io_threads), LLAMA_MOE_WINDOW_MAX_ROWS=str(args.rows), LLAMA_MOE_F_NOCACHE='0')
    if args.prefetch:
        env.update(LLAMA_MOE_LOOKAHEAD_PREFETCH='1',
                   LLAMA_MOE_LOOKAHEAD_PREFETCH_DEPTH=str(args.prefetch))
    if args.matmul_split:
        env['LLAMA_MOE_MATMUL_SPLIT'] = str(args.matmul_split)
    if args.no_cuda_graphs:
        env['GGML_CUDA_DISABLE_GRAPHS'] = '1'
    if args.mapped_experts:
        env['GGML_CUDA_LES_MAPPED'] = '1'
    env['GGML_OP_OFFLOAD_MIN_BATCH'] = str(args.offload_min_batch)
    if args.library_dir:
        args.library_dir = args.library_dir.resolve(strict=True)
        (args.library_dir / 'libllama.so.0').resolve(strict=True)
        env['LD_LIBRARY_PATH'] = str(args.library_dir) + ':' + env['LD_LIBRARY_PATH']
    cmd = [str(base / 'build-jetson/bin/llama-cli'), '-m', str(model / 'stripped.gguf'),
           '--moe-experts', str(model / 'experts'), '--moe-slot-bank', str(args.bank),
           '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '-ngl', '99', '-fa', 'on',
           '-c', str(args.ctx), '-b', '2048', '-ub', '8', '-n', str(args.tokens),
           '--seed', str(args.seed), '-fit', 'off', '--perf', '-lv', str(args.log_verbosity),
           '-f', str(args.prompt), '--no-display-prompt', '-st']
    if args.sampling == 'instruct':
        cmd += ['--temp', '0.7', '--top-p', '0.95', '--top-k', '100', '--min-p', '0',
                '--presence-penalty', '0', '--repeat-penalty', '1.03', '--repeat-last-n', '-1']
    else:
        cmd += ['--temp', '0']
    if args.threads:
        cmd += ['--threads', str(args.threads), '--threads-batch', str(args.threads)]
    if args.ctx_checkpoints is not None:
        cmd += ['--ctx-checkpoints', str(args.ctx_checkpoints)]
    if args.cache_ram_mib is not None:
        cmd += ['--cache-ram', str(args.cache_ram_mib)]
    if args.reasoning != 'auto':
        cmd += ['--reasoning', args.reasoning]
    if args.chat_template_file:
        args.chat_template_file = args.chat_template_file.resolve(strict=True)
        cmd += ['--chat-template-file', str(args.chat_template_file)]
    if args.draft:
        cmd += ['--spec-type', 'draft-dspark', '--spec-draft-model', str(model / 'dspark-Q4_K_M.gguf'),
                '--spec-draft-n-max', str(args.draft)]

    # Coordinate trials launched by this script and refuse existing inference.
    with open('/tmp/minicpm-bench-audit.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                exe = (entry / 'exe').resolve(strict=True).name
                if exe.startswith('llama-'):
                    raise RuntimeError(f'Inference/tool process already active: PID {entry.name} ({exe})')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue

        result = {'args': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                  'command': cmd, 'environment': {k: v for k, v in env.items() if k.startswith(('LLAMA_', 'GGML_CUDA_', 'GGML_OP_')) or k == 'LD_LIBRARY_PATH'},
                  'mem_before_kib': memory(), 'vmstat_before': vmstat()}
        start = time.monotonic()
        min_available, peak_rss, read_bytes, reason = memory()['MemAvailable'], 0, 0, None
        with open(str(prefix) + '.out', 'w') as stdout, open(str(prefix) + '.err', 'w') as stderr, \
                open(str(prefix) + '.memory.jsonl', 'w') as samples:
            child = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, start_new_session=True)
            try:
                while child.poll() is None:
                    m = memory()
                    min_available = min(min_available, m['MemAvailable'])
                    try:
                        status = Path(f'/proc/{child.pid}/status').read_text()
                        rss = re.search(r'VmRSS:\s+(\d+)', status)
                        peak_rss = max(peak_rss, int(rss[1]) if rss else 0)
                        io = Path(f'/proc/{child.pid}/io').read_text()
                        read_bytes = max(read_bytes, int(re.search(r'^read_bytes: (\d+)', io, re.M)[1]))
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        pass
                    elapsed = time.monotonic() - start
                    samples.write(json.dumps({'t': round(elapsed, 2), 'mem': m,
                                              'rss_peak_kib': peak_rss, 'read_bytes': read_bytes,
                                              'vmstat': vmstat()}) + '\n')
                    samples.flush()
                    if m['MemAvailable'] < args.min_available_mib * 1024 or elapsed > args.timeout:
                        reason = 'memory_guard' if m['MemAvailable'] < args.min_available_mib * 1024 else 'timeout'
                        stop(child)
                        break
                    time.sleep(0.5)
            finally:
                stop(child)
        result.update(rc=child.returncode, stop_reason=reason, wall_s=time.monotonic()-start,
                      min_available_kib=min_available, peak_rss_kib=peak_rss,
                      physical_read_bytes=read_bytes, mem_after_kib=memory(), vmstat_after=vmstat())
        txt = Path(str(prefix) + '.err').read_text(errors='replace')
        result['metrics'] = [line for line in txt.splitlines() if any(s in line for s in
                             ['eval time', 'total time', 'total[final]', 'draft acceptance',
                              'adding speculative', 'speculative decoding context', 'n_ctx_seq'])]
        Path(str(prefix) + '.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        return 0 if child.returncode == 0 and reason is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
