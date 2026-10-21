"""Extract the original reference-protocol SVG and collect measured inference evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('tag')
args = p.parse_args()
root = Path(__file__).resolve().parent
folder = root / 'pelican'
folder.mkdir(exist_ok=True)
run = json.loads((root / 'validation' / (args.tag + '.json')).read_text())
raw = (root / 'validation' / (args.tag + '.out')).read_bytes()
assert b'[Start thinking]' not in raw, 'Reasoning appeared in the non-thinking reference run'
start = re.search(rb'<svg\b', raw, re.I)
end = raw.lower().rfind(b'</svg>')
assert start and end > start.start(), 'No complete SVG; do not repair or fill it in'
svg = raw[start.start():end + len(b'</svg>')]
document = ET.fromstring(svg)
assert document.tag.rsplit('}', 1)[-1] == 'svg'
(folder / 'pelican.svg').write_bytes(svg)
text = '\n'.join(run['metrics'])
m = re.search(r'\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens.*?([\d.]+) tokens per second', text)
prompt = re.search(r'prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens', text)
assert m and prompt, 'Missing timing metrics; inspect the original runtime log'
draft = re.search(r'draft acceptance =\s+([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated', text)
lease = json.loads((root / 'validation' / (args.tag + '.lease.json')).read_text())
assert lease['lease_after'] == 'released'
result = {
    'tag': args.tag, 'args': run['args'], 'wall_s': run['wall_s'],
    'decode_tps': float(m[3]), 'output_tokens': int(m[2]),
    'generation_s': float(m[1]) / 1000,
    'input_tokens': int(prompt[2]), 'prefill_s': float(prompt[1]) / 1000,
    'rc': run['rc'], 'stop_reason': run['stop_reason'],
    'min_available_mib': run['min_available_kib'] / 1024,
    'peak_rss_mib': run['peak_rss_kib'] / 1024,
    'physical_read_gib': run['physical_read_bytes'] / 1024 ** 3,
    'oom_kill_delta': run['vmstat_after']['oom_kill'] - run['vmstat_before']['oom_kill'],
    'thinking_started': b'[Start thinking]' in raw, 'reasoning_disabled': run['args']['reasoning'] == 'off',
    'output_limit_reached': int(m[2]) >= run['args']['tokens'],
    'svg_complete': True, 'svg_xml_valid': True, 'svg_sha256': hashlib.sha256(svg).hexdigest(),
    'svg_bytes': len(svg), 'svg_chars': len(svg.decode()),
    'draft_acceptance': float(draft[1]) if draft else None,
    'draft_accepted': int(draft[2]) if draft else None,
    'draft_generated': int(draft[3]) if draft else None,
    'lease_status': lease['lease_after'],
    'extraction': 'exact contiguous bytes from model stdout; no generated-code edits',
}
(root / 'pelican-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
