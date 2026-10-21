#!/usr/bin/env python3
"""Reuse matching base tensors, fetch changed MTP ranges, accept only the published full-file SHA256."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path.home() / 'github/qwen38-kvmem'
FOLDER = ROOT / 'models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS'
NAME = 'Qwen3.6-35B-A3B-UD-IQ4_XS.gguf'
SIZE = 18209036576
SHA = 'df27a780435b7b45c2597536112ea3cb091f8544c3d0c3318d9f4258b31f7adf'
REVISION = '5bc3e238d916f48a861bac2f8a1990a0e9b7e98d'
URL = f'https://huggingface.co/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/resolve/{REVISION}/{NAME}'
sys.path.insert(0, str(ROOT / 'minicpm-les/llama.cpp-omni/gguf-py'))
from gguf import GGUFReader


def main():
    source = ROOT / 'models/Qwen3.6-35B-A3B-UD-IQ4_XS' / NAME
    header = FOLDER / 'mtp-header.gguf'
    partial, final = FOLDER / (NAME + '.assembled.part'), FOLDER / NAME
    assert not final.exists() and not partial.exists(), 'Inspect existing materialization before rerunning'
    old, new = GGUFReader(source), GGUFReader(header)
    originals = {t.name: t for t in old.tensors}
    missing, reused = [], []
    with source.open('rb') as src, partial.open('w+b') as dst, header.open('rb') as head:
        dst.truncate(SIZE)
        dst.write(head.read(int(new.data_offset)))
        for tensor in new.tensors:
            previous = originals.get(tensor.name)
            if (previous is None or previous.tensor_type != tensor.tensor_type
                    or list(previous.shape) != list(tensor.shape)):
                missing.append({'name': tensor.name, 'offset': int(tensor.data_offset), 'bytes': int(tensor.n_bytes)})
                continue
            src.seek(int(previous.data_offset))
            dst.seek(int(tensor.data_offset))
            remaining = int(tensor.n_bytes)
            while remaining:
                block = src.read(min(4 * 1024 * 1024, remaining))
                assert block
                dst.write(block)
                remaining -= len(block)
            reused.append(tensor.name)
    print(json.dumps({'reused_tensors': len(reused), 'download_bytes': sum(t['bytes'] for t in missing)}), flush=True)
    fd = os.open(partial, os.O_RDWR)
    blocks = []
    for tensor in missing:
        for offset in range(tensor['offset'], tensor['offset'] + tensor['bytes'], 16 * 1024 * 1024):
            blocks.append((offset, min(offset + 16 * 1024 * 1024, tensor['offset'] + tensor['bytes']) - 1))

    def download(bounds):
        start, end = bounds
        position = start
        for attempt in range(12):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://127.0.0.1:10809'}))
                request = urllib.request.Request(URL + f'?mtp-range={start}&attempt={attempt}',
                                                 headers={'Range': f'bytes={position}-{end}'})
                with opener.open(request, timeout=30) as response:
                    assert response.status == 206 and response.headers['Content-Range'] == f'bytes {position}-{end}/{SIZE}'
                    deadline = time.monotonic() + 60
                    while position <= end:
                        if time.monotonic() > deadline:
                            raise TimeoutError('Slow range')
                        data = response.read1(min(1024 * 1024, end - position + 1))
                        if not data:
                            raise IOError('Unexpected EOF')
                        view = memoryview(data)
                        while view:
                            n = os.pwrite(fd, view, position)
                            assert n > 0
                            position += n
                            view = view[n:]
                return
            except Exception:
                if attempt == 11:
                    raise
                time.sleep(1)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for index, _ in enumerate(executor.map(download, blocks), 1):
                print(json.dumps({'downloaded_ranges': index, 'total_ranges': len(blocks)}), flush=True)
        os.fsync(fd)
    finally:
        os.close(fd)
    h = hashlib.sha256()
    with partial.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    actual = h.hexdigest()
    report = {'url': URL, 'size': SIZE, 'sha256': SHA, 'actual_sha256': actual,
              'verified': actual == SHA, 'reused_tensor_count': len(reused),
              'downloaded_tensors': missing, 'downloaded_bytes': sum(t['bytes'] for t in missing),
              'method': 'Reused candidate base tensor bytes; full artifact accepted only by official SHA256',
              'finished_at': time.time()}
    (FOLDER / 'download-progress.json').write_text(json.dumps(report, indent=2) + '\n')
    assert actual == SHA, f'Artifact differs from upstream; do not use it: {actual}'
    partial.replace(final)
    print(json.dumps({'verified': True, 'sha256': actual}), flush=True)


if __name__ == '__main__':
    main()
