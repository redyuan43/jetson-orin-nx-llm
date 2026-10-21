#!/usr/bin/env python3
"""Repack every routed expert without requantization, then verify every tensor byte."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path.home() / 'github/qwen38-kvmem'
ENGINE = ROOT / 'minicpm-les/llama.cpp-omni'


def same_ranges(first, a, second, b, size):
    first.seek(a)
    second.seek(b)
    remaining = size
    while remaining:
        n = min(1024 * 1024, remaining)
        x, y = first.read(n), second.read(n)
        assert len(x) == len(y) == n and x == y, (a, b, size, remaining)
        remaining -= n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, default=ROOT / 'models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS')
    args = parser.parse_args()
    MODEL = args.model_dir.resolve(strict=True)
    SOURCE = MODEL / 'Qwen3.6-35B-A3B-UD-IQ4_XS.gguf'
    EXPERTS = MODEL / 'experts'
    STRIPPED = MODEL / 'stripped.gguf'
    receipt = json.loads((MODEL / 'download-progress.json').read_text())
    assert receipt['verified'] and receipt['actual_sha256'] == receipt['sha256']
    assert SOURCE.stat().st_size == receipt['size']
    started = time.monotonic()
    if not (EXPERTS / 'manifest.json').exists():
        subprocess.run([sys.executable, str(ENGINE / 'tools/moe/experts.py'), 'extract',
                        '--model', str(SOURCE), '--out-dir', str(EXPERTS)], check=True)
    if not STRIPPED.exists():
        temporary = MODEL / 'stripped.building.gguf'
        assert not temporary.exists(), 'Inspect interrupted stripping before resuming'
        subprocess.run([sys.executable, str(ENGINE / 'tools/moe/strip_experts.py'),
                        '--model', str(SOURCE), '--experts', str(EXPERTS),
                        '--out', str(temporary)], check=True)
        temporary.replace(STRIPPED)
    sys.path.insert(0, str(ENGINE / 'gguf-py'))
    from gguf import GGUFReader
    original, stripped = GGUFReader(SOURCE), GGUFReader(STRIPPED)
    manifest = json.loads((EXPERTS / 'manifest.json').read_text())
    originals = {t.name: t for t in original.tensors}
    experts = {e['original_gguf_tensor_name']: e for e in manifest['entries']}
    remaining = {t.name: t for t in stripped.tensors}
    assert set(experts).isdisjoint(remaining) and set(originals) == set(experts) | set(remaining)
    assert set(experts) == {n for n in originals if n.endswith('_exps.weight')}
    expected_fields = {k: v.contents() for k, v in original.fields.items() if not k.startswith('GGUF.')}
    actual_fields = {k: v.contents() for k, v in stripped.fields.items() if not k.startswith('GGUF.')}
    assert expected_fields == actual_fields
    count = manifest['model']['expert_count']
    assert manifest['model']['arch'] == 'qwen35moe' and count == 256
    assert manifest['model']['expert_used_count'] == 8
    with SOURCE.open('rb') as src, STRIPPED.open('rb') as small:
        for name, entry in experts.items():
            tensor = originals[name]
            assert list(tensor.shape) == entry['shape']
            assert tensor.tensor_type.name == entry['quant_type']
            assert tensor.n_bytes == entry['exact_byte_length']
            with (EXPERTS / entry['repacked_file']).open('rb') as packed:
                for expert in range(count):
                    same_ranges(src, int(tensor.data_offset) + expert * entry['bytes_per_expert'],
                                packed, entry['repacked_offset'] + expert * entry['expert_stride'],
                                entry['bytes_per_expert'])
        for name, tensor in remaining.items():
            old = originals[name]
            assert list(old.shape) == list(tensor.shape) and old.tensor_type == tensor.tensor_type
            assert old.n_bytes == tensor.n_bytes
            same_ranges(src, int(old.data_offset), small, int(tensor.data_offset), int(tensor.n_bytes))
    model_fields = {k: v for k, v in expected_fields.items()
                    if k.startswith('qwen35moe.') or k == 'general.architecture'}
    (MODEL / 'chat-template.jinja').write_text(expected_fields['tokenizer.chat_template'])
    groups = {}
    for entry in experts.values():
        groups[(entry['tensor_family'], entry['quant_type'], entry['bytes_per_expert'])] = entry['bytes_per_expert']
    expert_bytes = sum(e['exact_byte_length'] for e in experts.values())
    report = {
        'source': str(SOURCE), 'source_sha256': receipt['sha256'], 'verified_at': time.time(),
        'duration_s': time.monotonic() - started, 'all_tensor_bytes_identical': True,
        'metadata_identical': True, 'expert_tensor_count': len(experts),
        'resident_tensor_count': len(remaining), 'experts_bytes': expert_bytes,
        'resident_gguf_bytes': STRIPPED.stat().st_size,
        'expert_types': dict(collections.Counter(e['quant_type'] for e in experts.values())),
        'metadata': model_fields,
        'expert_buffer_estimates_bytes': {
            str(window): int(expert_bytes // count * window + sum(groups.values()) * 64)
            for window in (56, 128, 144, 160)},
        'template_sha256': hashlib.sha256((MODEL / 'chat-template.jinja').read_bytes()).hexdigest(),
    }
    (MODEL / 'ssd-verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
