"""Summarize the archived 128K capacity, retrieval, and unchanged-prompt SVG runs."""
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT.parent / '2026-10-21-qwen36-nx5'


def read_run(tag):
    prefix = ROOT / 'validation' / tag
    run = json.loads(prefix.with_suffix('.json').read_text())
    raw = prefix.with_suffix('.out').read_text()
    lease = json.loads(prefix.with_suffix('.lease.json').read_text())
    text = '\n'.join(run['metrics'])
    generation = re.search(r'\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens.*?([\d.]+) tokens per second', text)
    prompt = re.search(r'prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens', text)
    draft = re.search(r'draft acceptance =\s+([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated', text)
    assert generation and prompt
    assert run['rc'] == 0 and run['stop_reason'] is None
    assert lease['lease_after'] == 'released'
    assert run['vmstat_after']['oom_kill'] == run['vmstat_before']['oom_kill']
    assert '[Start thinking]' not in raw
    assert run['args']['ctx'] == 131072
    assert run['args']['window'] == 128 and run['args']['draft'] == 2
    parts = re.split(r'(?:[|/\\ \-]\x08){2,}', raw)
    assert len(parts) >= 2, 'Missing CLI answer boundary'
    answer = re.sub(r'[^\n]\x08', '', parts[-1]).split('[ Prompt:')[0].strip()
    result = {
        'tag': tag, 'input_tokens': int(prompt[2]), 'output_tokens': int(generation[2]),
        'prefill_s': float(prompt[1]) / 1000, 'generation_s': float(generation[1]) / 1000,
        'decode_tps': float(generation[3]), 'wall_s': run['wall_s'],
        'min_available_mib': run['min_available_kib'] / 1024,
        'peak_rss_mib': run['peak_rss_kib'] / 1024,
        'physical_read_gib': run['physical_read_bytes'] / 1024 ** 3,
        'oom_kill_delta': 0, 'rc': run['rc'], 'stop_reason': run['stop_reason'],
        'lease_status': lease['lease_after'],
        'draft_acceptance': float(draft[1]) if draft else None,
        'draft_accepted': int(draft[2]) if draft else None,
        'draft_generated': int(draft[3]) if draft else None,
        'output_limit_reached': int(generation[2]) >= run['args']['tokens'],
        'reasoning': run['args']['reasoning'],
    }
    assert not result['output_limit_reached']
    return result, answer


capacity, answer = read_run('qwen36-128k-w128-capacity')
prior_qa = json.loads((BASELINE / 'qa-comparison.json').read_text())['runs'][0]['answer']
capacity.update(answer=answer, identical_to_8k_greedy_answer=answer == prior_qa)
assert capacity['identical_to_8k_greedy_answer']

retrieval, answer = read_run('qwen36-128k-w128-long8k')
expected = json.loads((ROOT / 'context-probe-8k.expected.json').read_text())
assert hashlib.sha256((ROOT / 'context-probe-8k.txt').read_bytes()).hexdigest() == expected['prompt_sha256']
actual = json.loads(answer)
retrieval.update(expected=expected['expected'], actual=actual,
                 exact_matches=sum(actual.get(k) == v for k, v in expected['expected'].items()),
                 exact_json_match=actual == expected['expected'])
assert retrieval['exact_json_match'] and retrieval['input_tokens'] >= 8192

pelican, answer = read_run('qwen36-128k-w128-pelican')
match = re.search(r'<svg\b[\s\S]*?</svg>', answer)
assert match, 'No complete original SVG'
svg = match[0].encode()
ET.fromstring(svg)
folder = ROOT / 'pelican'
folder.mkdir(exist_ok=True)
(folder / 'pelican.svg').write_bytes(svg)
baseline = (BASELINE / 'pelican/pelican.svg').read_bytes()
pelican.update(svg_complete=True, svg_xml_valid=True, svg_sha256=hashlib.sha256(svg).hexdigest(),
               svg_bytes=len(svg), identical_to_8k_svg=svg == baseline,
               baseline_report='../2026-10-21-qwen36-nx5/pelican-result.json')

result = {
    'model': 'Qwen3.6-35B-A3B MTP UD-IQ4_XS', 'host': 'nx5',
    'ctx': 131072, 'native_ctx': 262144,
    'capacity': capacity, 'retrieval': retrieval, 'pelican': pelican,
    'full_128k_input_tested': False,
    'longest_actual_input_tokens': retrieval['input_tokens'],
    'scope': '128K allocation, one approximately 8K retrieval input, and a complete short-prompt SVG generation; no full 128K input or persistent multi-turn validation.',
    'memory_mib': {'expert_buffers': 7739.5, 'target_q8_kv': 1360, 'mtp_f16_kv': 256,
                   'main_recurrent_buffer': 188.44},
}
(ROOT / 'assessment.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
