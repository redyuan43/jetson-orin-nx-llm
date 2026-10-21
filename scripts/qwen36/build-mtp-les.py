#!/usr/bin/env python3
"""Build patches 0002 and 0004 in an isolated library; preserve the engine checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path,
                        default=Path.home() / 'github/qwen38-kvmem/minicpm-les/llama.cpp-omni')
    parser.add_argument('--cuda-library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    engine, cuda = args.engine.resolve(strict=True), args.cuda_library.resolve(strict=True)
    build, out = engine / 'build-jetson', args.output.resolve()
    commands = subprocess.check_output(['ninja', '-t', 'commands', 'llama'], cwd=build, text=True).splitlines()
    link = next(shlex.split(s) for s in commands if ' -shared ' in s and ' -o bin/libllama.so.' in s)
    assert link[:2] == [':', '&&'] and link[-2:] == ['&&', ':']
    link = link[2:-2]
    library = Path(link[link.index('-o') + 1]).name
    assert library == 'libllama.so.0.0.1'
    out.mkdir(parents=True, exist_ok=False)
    entries = [('llama-moe-slot.cpp', '0002-les-window-slot-metadata.patch'),
               ('models/qwen35moe.cpp', '0004-qwen-mtp-les-window.patch')]
    evidence = []
    for filename, patch_name in entries:
        original = engine / 'src' / filename
        source = out / 'src' / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, source)
        patch = Path(__file__).resolve().parents[2] / 'patches' / patch_name
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)], cwd=out, check=True)
        command = next(shlex.split(s) for s in commands if f'{filename}.o -c ' in s)
        cleaned, i = [], 0
        while i < len(command):
            if command[i] in ('-MF', '-MT'):
                i += 2
                continue
            if command[i] != '-MD':
                cleaned.append(command[i])
            i += 1
        obj = out / (Path(filename).stem + '.o')
        cleaned[cleaned.index('-o') + 1] = str(obj)
        cleaned[cleaned.index('-c') + 1] = str(source)
        cleaned.insert(1, '-I' + str(original.parent))
        subprocess.run(cleaned, cwd=build, check=True)
        link[link.index('src/CMakeFiles/llama.dir/' + filename + '.o')] = str(obj)
        evidence.append({'source': str(original), 'original_sha256': sha(original),
                         'patched_sha256': sha(source), 'patch': patch_name,
                         'patch_sha256': sha(patch), 'compile': cleaned, 'object_sha256': sha(obj)})
    link[link.index('-o') + 1] = str(out / library)
    subprocess.run(link, cwd=build, check=True)
    for name in ('libllama.so.0', 'libllama.so'):
        (out / name).symlink_to(library)
    (out / 'libggml-cuda.so.0').symlink_to(cuda)
    manifest = {'engine': str(engine), 'source_files': evidence, 'link': link,
                'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=engine, text=True).strip(),
                'library_sha256': sha(out / library), 'cuda_sha256': sha(cuda)}
    (out / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'output': str(out), 'library_sha256': manifest['library_sha256']}))


if __name__ == '__main__':
    main()
