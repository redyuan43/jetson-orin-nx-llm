#!/usr/bin/env python3
"""Download the pinned UD-IQ4_XS file with bounded RAM, resume, and SHA256 verification."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

NAME = 'Qwen3.6-35B-A3B-UD-IQ4_XS.gguf'
CHUNK = 128 * 1024 * 1024


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path(__file__).resolve().parents[2] / 'configs/qwen36-iq4xs-8k.json')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    ROOT = Path.home() / 'github/qwen38-kvmem' / config['model_dir']
    REVISION = config['revision']
    URL = f"{config['source']}/resolve/{REVISION}/{NAME}"
    SIZE = config['source_bytes']
    SHA256 = config['source_sha256']
    ROOT.mkdir(parents=True, exist_ok=True)
    final = ROOT / NAME
    if final.exists():
        assert final.stat().st_size == SIZE and sha256(final) == SHA256
        print('Pinned model already verified', flush=True)
        return
    partial = ROOT / (NAME + '.part')
    receipt = ROOT / 'download-progress.json'
    state = json.loads(receipt.read_text()) if receipt.exists() else {
        'url': URL, 'size': SIZE, 'sha256': SHA256, 'chunk_bytes': CHUNK, 'complete_chunks': []}
    assert (state['url'], state['size'], state['sha256'], state['chunk_bytes']) == (URL, SIZE, SHA256, CHUNK)
    if state['complete_chunks']:
        assert partial.exists() and partial.stat().st_size == SIZE
    fd = os.open(partial, os.O_CREAT | os.O_RDWR, 0o644)
    os.ftruncate(fd, SIZE)
    lock = threading.Lock()
    count = (SIZE + CHUNK - 1) // CHUNK
    started = time.monotonic()

    def download(index):
        start, end = index * CHUNK, min(SIZE, (index + 1) * CHUNK) - 1
        position = start
        for attempt in range(12):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                    'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'}))
                request = urllib.request.Request(URL + f'?download=true&chunk={index}&attempt={attempt}',
                                                 headers={'Range': f'bytes={position}-{end}'})
                with opener.open(request, timeout=45) as response:
                    assert response.status == 206
                    assert response.headers['Content-Range'] == f'bytes {position}-{end}/{SIZE}'
                    response_started = time.monotonic()
                    while position <= end:
                        if time.monotonic() - response_started > 90:
                            raise TimeoutError('Slow response; resume from the current byte')
                        data = response.read1(min(1024 * 1024, end - position + 1))
                        if not data:
                            raise IOError('Unexpected EOF')
                        view = memoryview(data)
                        while view:
                            written = os.pwrite(fd, view, position)
                            if written <= 0:
                                raise IOError('Write failed')
                            position += written
                            view = view[written:]
                with lock:
                    os.fdatasync(fd)
                    state['complete_chunks'].append(index)
                    tmp = receipt.with_suffix('.tmp')
                    tmp.write_text(json.dumps(state, indent=2) + '\n')
                    tmp.replace(receipt)
                    print(json.dumps({'complete_chunks': len(state['complete_chunks']),
                                      'total_chunks': count, 'elapsed_s': round(time.monotonic() - started, 1)}), flush=True)
                return
            except Exception as error:
                print(json.dumps({'chunk': index, 'attempt': attempt + 1,
                                  'received_bytes': position - start,
                                  'error': type(error).__name__,
                                  'detail': str(error).split('?')[0][:120]}), flush=True)
                if attempt == 11:
                    raise
                time.sleep(2 * (attempt + 1))

    try:
        pending = [i for i in range(count) if i not in state['complete_chunks']]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(download, i) for i in pending]
            errors = []
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    errors.append(type(error).__name__)
            if errors:
                raise RuntimeError(f'{len(errors)} chunks unfinished; resume using this receipt')
    finally:
        os.close(fd)
    print('Checking complete model SHA256', flush=True)
    actual = sha256(partial)
    assert actual == SHA256, actual
    partial.replace(final)
    state.update(verified=True, finished_at=time.time(), actual_sha256=actual)
    receipt.write_text(json.dumps(state, indent=2) + '\n')
    print(json.dumps({'verified': True, 'file': str(final), 'sha256': actual}), flush=True)


if __name__ == '__main__':
    main()
