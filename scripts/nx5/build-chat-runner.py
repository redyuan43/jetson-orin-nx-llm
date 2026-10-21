#!/usr/bin/env python3
"""Build the standalone JSONL adapter against the already deployed nx5 engine."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--engine', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
source = Path(__file__).with_name('chat-runner.cpp').resolve()
a.output.mkdir(parents=True, exist_ok=False)
engine = a.engine.resolve()
lib = engine / 'build-jetson/bin'
command = ['c++', '-std=c++17', '-O2', '-pthread', '-DGGML_BACKEND_SHARED', '-DGGML_SHARED',
           '-DGGML_USE_CPU', '-DGGML_USE_CUDA', '-DLLAMA_SHARED']
for name in ('common', 'vendor', 'include', 'ggml/include', 'tools/server', 'tools/mtmd'):
    command += ['-I', str(engine / name)]
command += [str(source), '-L', str(lib), '-Wl,-rpath,' + str(lib),
            '-lllama-cli-impl', '-lllama-common', '-lllama', '-lmtmd', '-lggml-base',
            '-o', str(a.output / 'llama-nx5-chat')]
subprocess.run(command, check=True)
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
receipt = {'binary_sha256': sha(a.output / 'llama-nx5-chat'), 'source_sha256': sha(source),
           'engine_commit': subprocess.check_output(['git', '-C', str(engine), 'rev-parse', 'HEAD'], text=True).strip(),
           'command': command, 'engine_headers_sha256': {
               str(f.relative_to(engine)): sha(f) for directory in ('common', 'vendor', 'include', 'ggml/include', 'tools/server')
               for f in (engine / directory).rglob('*.h')},
           'linked_cli_impl_sha256': sha(lib / 'libllama-cli-impl.so')}
(a.output / 'build.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({k: receipt[k] for k in ('binary_sha256', 'source_sha256', 'linked_cli_impl_sha256')}))
