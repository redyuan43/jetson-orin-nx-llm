#!/usr/bin/env python3
"""Build patch 0002 as an isolated libllama using the existing nx5 Ninja build."""
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
    parser.add_argument('--output', type=Path, required=True, help='New directory; never overwritten')
    args = parser.parse_args()
    engine = args.engine.resolve(strict=True)
    build = engine / 'build-jetson'
    patch = Path(__file__).resolve().parents[2] / 'patches/0002-les-window-slot-metadata.patch'
    commands = subprocess.check_output(['ninja', '-t', 'commands', 'llama'], cwd=build, text=True).splitlines()
    compile_cmd = next(shlex.split(s) for s in commands if 'llama-moe-slot.cpp.o -c ' in s)
    link_cmd = next(shlex.split(s) for s in commands if ' -shared ' in s and ' -o bin/libllama.so.' in s)
    if link_cmd[:2] != [':', '&&'] or link_cmd[-2:] != ['&&', ':']:
        raise RuntimeError('Ninja link format changed; inspect it before building')
    link_cmd = link_cmd[2:-2]
    original_library = Path(link_cmd[link_cmd.index('-o') + 1])
    if original_library.name != 'libllama.so.0.0.1':
        raise RuntimeError('Library ABI changed; this helper targets the audited nx5 build')

    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / 'src').mkdir()
    original_source = engine / 'src/llama-moe-slot.cpp'
    source = out / 'src/llama-moe-slot.cpp'
    shutil.copyfile(original_source, source)
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)], cwd=out, check=True)

    # Reuse all original flags; emit neither object nor depfile into the engine.
    cleaned = []
    i = 0
    while i < len(compile_cmd):
        if compile_cmd[i] in ('-MF', '-MT'):
            i += 2
            continue
        if compile_cmd[i] != '-MD':
            cleaned.append(compile_cmd[i])
        i += 1
    cleaned[cleaned.index('-o') + 1] = str(out / 'moe-slot.o')
    cleaned[cleaned.index('-c') + 1] = str(source)
    link_cmd[link_cmd.index('-o') + 1] = str(out / original_library.name)
    link_cmd[link_cmd.index('src/CMakeFiles/llama.dir/llama-moe-slot.cpp.o')] = str(out / 'moe-slot.o')
    subprocess.run(cleaned, cwd=build, check=True)
    subprocess.run(link_cmd, cwd=build, check=True)
    for name in ('libllama.so.0', 'libllama.so'):
        (out / name).symlink_to(original_library.name)
    manifest = {
        'engine': str(engine),
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=engine, text=True).strip(),
        'original_source_sha256': hashlib.sha256(original_source.read_bytes()).hexdigest(),
        'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
        'library_sha256': hashlib.sha256((out / original_library.name).read_bytes()).hexdigest(),
        'compile': cleaned, 'link': link_cmd,
    }
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Isolated library: {out}')


if __name__ == '__main__':
    main()
