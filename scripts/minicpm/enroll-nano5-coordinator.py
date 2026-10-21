#!/usr/bin/env python3
"""Prepare/apply a narrow nano5 enrollment on the existing AI coordinator.

Run on AI. Credentials arrive only over stdin. Existing leases, reservations,
other groups, and the immutable coordinator image are preserved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/home/ai/services/siyuan-gateways')
GROUP = 'nano5-igpu-group'
RESOURCE = 'nano5-igpu'
RELEASE = 'nano5-minicpm-20261021-r1'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private_write(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    os.replace(tmp, path)


def nano_idle(db):
    row = db.execute('SELECT config,transition_operation FROM resource_groups WHERE id=?', (GROUP,)).fetchone()
    if not row or row[1]:
        raise RuntimeError('nano5_transition_active')
    held = db.execute("SELECT count(*) FROM allocations a JOIN leases l ON a.lease_id=l.id WHERE a.resource=? AND l.status='held'", (RESOURCE,)).fetchone()[0]
    for raw, in db.execute("SELECT request FROM reservations WHERE status IN ('waiting','preparing')"):
        if any(r['resource_id'] == RESOURCE for c in json.loads(raw)['candidates'] for r in c['resources']):
            raise RuntimeError('nano5_reservation_active')
    if held:
        raise RuntimeError('nano5_lease_active')
    return row[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    a = parser.parse_args()
    folder = ROOT / 'coordinator' / RELEASE
    config = ROOT / 'coordinator/config/console.json'
    env = ROOT / 'coordinator/secrets.env'
    database = ROOT / 'coordinator/state/leases.sqlite3'
    compose = ['docker', 'compose', '-f', str(ROOT/'compose.json')]
    if not a.apply:
        token = sys.stdin.read().strip()
        if len(token) < 32 or '\n' in token:
            raise ValueError('invalid_private_token')
        folder.mkdir(mode=0o700, exist_ok=False)
        before = json.loads(config.read_text())
        with sqlite3.connect('file:'+str(database)+'?mode=ro', uri=True) as db:
            group_before = nano_idle(db)
        if json.loads(group_before) != before['resource_groups'][GROUP]:
            raise RuntimeError('config_database_mismatch')
        after = json.loads(json.dumps(before))
        group = after['resource_groups'][GROUP]
        assert group == {'resources': [RESOURCE], 'modes': ['comfy', 'off']}
        group['modes'].append('minicpm')
        assert 'nano5-minicpm' not in after['executors']
        after['executors']['nano5-minicpm'] = {'token_env': 'NANO5_MINICPM_EXECUTOR_TOKEN', 'resources': [RESOURCE], 'groups': []}
        env_before = env.read_text()
        assert 'NANO5_MINICPM_EXECUTOR_TOKEN=' not in env_before
        for path in (config, env):
            shutil.copy2(path, folder/(path.name+'.before'))
        private_write(folder/'console.json', (json.dumps(after, indent=2)+'\n').encode())
        private_write(folder/'secrets.env', (env_before.rstrip()+'\nNANO5_MINICPM_EXECUTOR_TOKEN='+token+'\n').encode())
        manifest = {'release': RELEASE, 'config_before': sha(config), 'env_before': sha(env),
                    'config_after': sha(folder/'console.json'), 'env_after': sha(folder/'secrets.env'),
                    'group_before': group_before, 'group_after': json.dumps(group, sort_keys=True)}
        private_write(folder/'manifest.json', json.dumps(manifest, indent=2).encode())
        print(json.dumps({'prepared': True, 'release': RELEASE, 'config_before': manifest['config_before'], 'config_after': manifest['config_after']}))
        return
    m = json.loads((folder/'manifest.json').read_text())
    if sha(config) != m['config_before'] or sha(env) != m['env_before']:
        raise RuntimeError('baseline_changed')
    if sha(folder/'console.json') != m['config_after'] or sha(folder/'secrets.env') != m['env_after']:
        raise RuntimeError('candidate_changed')
    with sqlite3.connect('file:'+str(database)+'?mode=ro', uri=True) as db:
        if nano_idle(db) != m['group_before']:
            raise RuntimeError('group_changed')
    # This stops only the resource API, not any GPU executor or model service.
    # Unknown holds on unrelated devices remain durable and are never released.
    subprocess.run(compose+['stop', 'coordinator'], check=True)
    changed = False
    try:
        with sqlite3.connect(database) as db:
            old_group = nano_idle(db)
            if old_group != m['group_before']:
                raise RuntimeError('group_changed_after_drain')
            held = list(db.execute("SELECT id,status FROM leases WHERE status='held'"))
            backup = sqlite3.connect(folder/'leases-backup.sqlite3')
            db.backup(backup)
            backup.close()
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE resource_groups SET config=?,mode=?,state=?,active=?,observed=NULL WHERE id=?',
                       (m['group_after'], 'off', 'unknown', 1, GROUP))
            db.commit()
            changed = True
        private_write(config, (folder/'console.json').read_bytes())
        private_write(env, (folder/'secrets.env').read_bytes())
        subprocess.run(compose+['up', '-d', '--no-deps', '--force-recreate', 'coordinator'], check=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(30):
            try:
                with opener.open('http://127.0.0.1:18292/healthz', timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError('coordinator_health_failed')
        with sqlite3.connect(database) as db:
            assert all(db.execute('SELECT status FROM leases WHERE id=?', (identifier,)).fetchone()[0] == status for identifier, status in held)
        receipt = {'release': RELEASE, 'active': True, 'unrelated_held_leases_preserved': len(held), 'config_sha256': sha(config)}
        private_write(folder/'activated.json', json.dumps(receipt, indent=2).encode())
        print(json.dumps(receipt))
    except BaseException:
        # No nano5 client is started until this publisher returns successfully.
        # Restore only the enrollment config; never restore an old lease database.
        if changed:
            with sqlite3.connect(database) as db:
                nano_idle(db)
                db.execute('UPDATE resource_groups SET config=? WHERE id=?', (m['group_before'], GROUP))
            private_write(config, (folder/'console.json.before').read_bytes())
            private_write(env, (folder/'secrets.env.before').read_bytes())
        subprocess.run(compose+['up', '-d', '--no-deps', '--force-recreate', 'coordinator'], check=True)
        raise


if __name__ == '__main__':
    main()
