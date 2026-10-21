#!/usr/bin/env python3
"""Build the opt-in LES mapped CUDA allocator without changing the engine checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path,
                        default=Path.home() / 'github/qwen38-kvmem/minicpm-les/llama.cpp-omni')
    parser.add_argument('--base-library-dir', type=Path, required=True,
                        help='Directory containing the validated window-fix libllama')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    engine = args.engine.resolve(strict=True)
    build = engine / 'build-jetson'
    baseline = (args.base_library_dir / 'libllama.so.0').resolve(strict=True)
    patch = Path(__file__).resolve().parents[2] / 'patches/0003-cuda-les-mapped-experts.patch'
    commands = subprocess.check_output(['ninja', '-t', 'commands', 'ggml-cuda'], cwd=build, text=True).splitlines()
    compile_cmd = next(shlex.split(s) for s in commands if ' -c ' in s and s.endswith('/ggml-cuda.cu.o'))
    link_cmd = next(shlex.split(s) for s in commands if ' -shared ' in s and ' -o bin/libggml-cuda.so.' in s)
    if link_cmd[:2] != [':', '&&'] or link_cmd[-2:] != ['&&', ':']:
        raise RuntimeError('Unexpected Ninja link command')
    link_cmd = link_cmd[2:-2]
    soname = Path(link_cmd[link_cmd.index('-o') + 1]).name
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    original = engine / 'ggml/src/ggml-cuda/ggml-cuda.cu'
    source = out / 'ggml/src/ggml-cuda/ggml-cuda.cu'
    source.parent.mkdir(parents=True)
    shutil.copyfile(original, source)
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)], cwd=out, check=True)
    cleaned = []
    i = 0
    while i < len(compile_cmd):
        if compile_cmd[i] in ('-MF', '-MT'):
            i += 2
            continue
        if compile_cmd[i] != '-MD':
            cleaned.append(compile_cmd[i])
        i += 1
    cleaned += ['-I' + str(original.parent)]
    cleaned[cleaned.index('-o') + 1] = str(out / 'ggml-cuda.cu.o')
    cleaned[cleaned.index('-c') + 1] = str(source)
    link_cmd[link_cmd.index('-o') + 1] = str(out / soname)
    link_cmd[link_cmd.index('ggml/src/ggml-cuda/CMakeFiles/ggml-cuda.dir/ggml-cuda.cu.o')] = str(out / 'ggml-cuda.cu.o')
    subprocess.run(cleaned, cwd=build, check=True)
    subprocess.run(link_cmd, cwd=build, check=True)
    for name in ('libggml-cuda.so.0', 'libggml-cuda.so'):
        (out / name).symlink_to(soname)
    (out / 'libllama.so.0').symlink_to(baseline)
    manifest = {
        'engine': str(engine), 'baseline_llama': str(baseline),
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=engine, text=True).strip(),
        'source_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
        'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
        'library_sha256': hashlib.sha256((out / soname).read_bytes()).hexdigest(),
        'compile': cleaned, 'link': link_cmd,
    }
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(out)


if __name__ == '__main__':
    main()
