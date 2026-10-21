"""Extract the unmodified generated HTML and compare the measured two-device runs."""
import hashlib
import json
from pathlib import Path
import re

folder = Path(__file__).resolve().parent
nano_folder = folder.parent / '2026-10-21-nano5-thinking'
nx = json.loads((folder / 'nx5-pelican-thinking.json').read_text())
nano = json.loads((nano_folder / 'nano-pelican-thinking.json').read_text())
raw = (folder / 'nx5-pelican-thinking.out').read_bytes()
assert b'[Start thinking]' in raw and b'[End thinking]' in raw
match = re.search(rb'<!DOCTYPE\s+html\b[^>]*>.*?</html>',
                  raw[raw.index(b'[End thinking]'):], re.I | re.S)
assert match, 'No complete HTML found; preserve the original output for inspection'
html = match[0]
(folder / 'pelican.html').write_bytes(html)
extraction = {'method': 'exact byte substring from DOCTYPE through closing html; no code edits',
              'sha256': hashlib.sha256(html).hexdigest(),
              'html_chars': len(html.decode()), 'html_bytes': len(html),
              'thinking_started': True, 'thinking_finished': True}
(folder / 'extraction.json').write_text(json.dumps(extraction, indent=2) + '\n')


def metrics(result):
    text = '\n'.join(result['metrics'])
    m = re.search(r'\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens.*?([\d.]+) tokens per second', text)
    prompt = re.search(r'prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens', text)
    acceptance = re.search(r'draft acceptance = ([\d.]+)', text)
    return {'wall_s': result['wall_s'], 'output_tokens_including_reasoning': int(m[2]),
            'decode_tps': float(m[3]), 'decode_s': float(m[1]) / 1000,
            'prompt_s': float(prompt[1]) / 1000, 'prompt_tokens': int(prompt[2]),
            'draft_acceptance': float(acceptance[1]),
            'min_available_mib': result['min_available_kib'] / 1024,
            'physical_read_gib': result['physical_read_bytes'] / 1024 ** 3,
            'peak_rss_mib': result['peak_rss_kib'] / 1024,
            'oom_kill_delta': result['vmstat_after']['oom_kill'] - result['vmstat_before']['oom_kill'],
            'rc': result['rc'], 'stop_reason': result['stop_reason']}


path_keys = {'tag', 'root', 'prompt', 'output', 'chat_template_file', 'library_dir'}
differences = {k: [nano['args'].get(k), nx['args'].get(k)]
               for k in set(nano['args']) | set(nx['args'])
               if k not in path_keys and nano['args'].get(k) != nx['args'].get(k)}
assert not differences, differences
proof = json.loads((folder / 'lease-proof.json').read_text())
nano_proof = json.loads((nano_folder / 'lease-proof.json').read_text())
assert proof['request']['prompt_sha256'] == nano_proof['request']['prompt_sha256']
comparison = {
    'comparison': 'nx5 vs nano5; original prompt, same weights/runtime, reasoning on, same inference parameters',
    'argument_differences_excluding_paths': differences,
    'prompt_sha256': proof['request']['prompt_sha256'],
    'template_sha256': '49c37bcc86c7f7f20ed2ebf79e9f872be41ba2b5f9d0d51e6712cc038ef51d6c',
    'nano5': metrics(nano), 'nx5': metrics(nx),
    'html_identical_to_nano5': html == (nano_folder / 'pelican.html').read_bytes(),
    'nx5_html_sha256': extraction['sha256'],
    'lease_after': proof['lease_after'], 'production_defaults_changed': False,
    'scope_note': 'One sample per machine; different physical RAM and OS page-cache state are not controlled.'}
comparison['decode_speed_ratio'] = comparison['nx5']['decode_tps'] / comparison['nano5']['decode_tps']
if (folder / 'browser-check.json').exists():
    browser = json.loads((folder / 'browser-check.json').read_text())
    comparison['browser_errors'] = browser['page_errors']
    comparison['html_visual_passed'] = False
    comparison['findings'] = [
        'Wheel rims orbit away from the stationary hubs',
        'Head translation is overwritten by CSS rotation; head clips outside scene',
        'JS parse error prevents registering button handlers; pause click leaves animations running']
(folder / 'comparison.json').write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(comparison, ensure_ascii=False, indent=2))
