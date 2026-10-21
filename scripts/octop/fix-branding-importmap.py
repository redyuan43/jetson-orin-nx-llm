#!/usr/bin/env python3
"""Keep branded entry and lazy chunks on one module instance; no container restart."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--html', type=Path, required=True)
p.add_argument('--expected-sha256', required=True)
p.add_argument('--original-entry', required=True)
p.add_argument('--branded-entry', required=True)
p.add_argument('--backup-dir', type=Path, required=True)
p.add_argument('--apply', action='store_true')
a = p.parse_args()
raw = a.html.read_bytes()
before = hashlib.sha256(raw).hexdigest()
assert before == a.expected_sha256, 'HTML baseline changed'
for entry in (a.original_entry, a.branded_entry):
    assert re.fullmatch(r'/assets/[A-Za-z0-9_.-]+\.js', entry)
text = raw.decode()
marker = '<script id="octop-brand-importmap" type="importmap">'
assert marker not in text, 'Import map already installed'
anchor = '<script type="module" crossorigin src="' + a.branded_entry + '"></script>'
assert text.count(anchor) == 1
mapping = marker + json.dumps({'imports': {a.original_entry: a.branded_entry}}) + '</script>\n    '
updated = text.replace(anchor, mapping + anchor).encode()
proof = {'before_sha256': before, 'after_sha256': hashlib.sha256(updated).hexdigest(),
         'original_entry': a.original_entry, 'branded_entry': a.branded_entry, 'applied': a.apply}
if a.apply:
    a.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = a.backup_dir / ('index-before-importmap-' + str(time.time_ns()) + '.html')
    shutil.copy2(a.html, backup)
    # Preserve the inode: Docker bind-mounts this individual file.
    with a.html.open('wb') as output:
        output.write(updated)
        output.flush()
        os.fsync(output.fileno())
    proof['backup'] = str(backup)
print(json.dumps(proof))
