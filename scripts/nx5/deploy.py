#!/usr/bin/env python3
"""Publish an immutable committed nx5 service bundle; preflight unless --apply."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess

FILES = ['scripts/nx5/compat_api.py', 'configs/nx5-chat-runtime.json', 'scripts/nx5/chat-runner.cpp', 'scripts/nx5/service.py', 'scripts/nx5/run-model.py', 'scripts/minicpm/bench-audit.py',
         'configs/qwen36-iq4xs-128k.json', 'configs/nx5-minicpm-service.json',
         'configs/minicpm-v47-instruct.jinja', 'docs/nx5-service/API.md',
         'docs/nx5-service/catalog.json', 'docs/nx5-service/openapi.json',
         'deploy/nx5/nx5-ssd-llm.service']

REMOTE = r'''
import base64,hashlib,json,os,secrets,shlex,subprocess,time,urllib.request
from pathlib import Path
p=PAYLOAD
home=Path.home(); base=home/'.local/share/nx5-ssd-llm'; private=home/'.config/nx5-ssd-llm'
state=home/'.local/state/nx5-ssd-llm'; unit_dir=home/'.config/systemd/user'
legacy=['siyuan-fleet-control.service','siyuan-text-ingress.service','siyuan-text-compat-bridge.service',
        'siyuan-image-comfy.service','nx5-ornith-35b-a3b-iq2s.service','qwen38-kvmem.service']
def state_of(unit,prop):
    return subprocess.check_output(['systemctl','--user','show',unit,'--property='+prop,'--value'],text=True).strip()
for unit in legacy:
    if state_of(unit,'ActiveState') not in ('inactive','failed'):raise RuntimeError('legacy_active: '+unit)
if state_of('nx5-ssd-llm.service','ActiveState') not in ('inactive','failed'):
    raise RuntimeError('existing_service_active: drain and stop it before upgrading')
for f in (state/'jobs').glob('op_*.json'):
    if json.loads(f.read_text())['status'] not in ('succeeded','failed'):raise RuntimeError('unresolved_job')
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():continue
    try:
        if (entry/'exe').resolve(strict=True).name.startswith('llama-'):raise RuntimeError('inference_active')
    except (FileNotFoundError,PermissionError,ProcessLookupError):pass
credentials=home/'.config/siyuan-text-ingress'
cfg=json.loads((credentials/'wrapper.json').read_text())['central']; env={}
for line in (credentials/'credentials.env').read_text().splitlines():
    for word in shlex.split(line,comments=True):
        if '=' in word:
            key,value=word.split('=',1);env[key]=value
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
req=urllib.request.Request(cfg['base_url']+'/internal/v1/resource-groups/nx5-igpu-group/activity',
    headers={'Authorization':'Bearer '+env[cfg['auth']['token_env']]})
with opener.open(req,timeout=15) as r:activity=json.load(r)
if activity['leases'] or activity['pending'] or activity['transition']:raise RuntimeError('resource_busy')
for name,data in p['files'].items():
    if hashlib.sha256(base64.b64decode(data)).hexdigest()!=p['manifest']['files'][name]:raise RuntimeError('bundle_changed')
runtime=json.loads(base64.b64decode(p['files']['configs/nx5-chat-runtime.json']))
model_root=home/'github/qwen38-kvmem'
for name,expected in {runtime['binary']:runtime['binary_sha256'],**runtime['libraries']}.items():
    if hashlib.sha256((model_root/name).read_bytes()).hexdigest()!=expected:raise RuntimeError('chat_runtime_changed: '+name)
if not p['apply']:
    print(json.dumps({'preflight':'passed','modified':False,'source_commit':p['manifest']['source_commit']}))
    raise SystemExit(0)
base.mkdir(parents=True,exist_ok=True);private.mkdir(parents=True,exist_ok=True,mode=0o700)
state.mkdir(parents=True,exist_ok=True,mode=0o700);unit_dir.mkdir(parents=True,exist_ok=True)
release=base/'releases'/p['manifest']['source_commit']
release.mkdir(parents=True,exist_ok=False)
for name,data in p['files'].items():
    target=release/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(base64.b64decode(data))
(release/'release.json').write_text(json.dumps(p['manifest'],indent=2)+'\n')
current=base/'current'; old=os.readlink(current) if current.is_symlink() else None
backup=state/('deployment-'+p['manifest']['source_commit']+'.json')
old_unit=unit_dir/'nx5-ssd-llm.service'
record={'source_commit':p['manifest']['source_commit'],'previous_release':old,
        'legacy_enabled_before':{u:state_of(u,'UnitFileState') for u in legacy},
        'previous_unit':old_unit.read_text() if old_unit.exists() else None}
backup.write_text(json.dumps(record,indent=2));backup.chmod(0o600)
token=private/'client.key'
if not token.exists():token.write_text(secrets.token_urlsafe(48)+'\n');token.chmod(0o600)
config=private/'service.json'
desired={'root':str(home/'github/qwen38-kvmem'),'state':str(state/'jobs'),
         'client_token_file':str(token),'bind':'100.118.44.27','port':18347}
if config.exists() and json.loads(config.read_text())!=desired:raise RuntimeError('service_config_changed')
config.write_text(json.dumps(desired,indent=2)+'\n');config.chmod(0o600)
# They were checked idle. Disable their boot activation, preserving all files/data.
subprocess.run(['systemctl','--user','disable','--now',*legacy],check=True)
old_unit.write_bytes((release/'deploy/nx5/nx5-ssd-llm.service').read_bytes())
temp=base/'current.new';temp.symlink_to(release);os.replace(temp,current)
try:
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','nx5-ssd-llm.service'],check=True)
    subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True)
    for _ in range(20):
        try:
            with opener.open('http://100.118.44.27:18347/healthz',timeout=2) as response:health=json.load(response)
            if health.get('commit')==p['manifest']['commit'] and health.get('state')=='ready':break
        except Exception:pass
        time.sleep(1)
    else:raise RuntimeError('health_failed')
except Exception:
    subprocess.run(['systemctl','--user','stop','nx5-ssd-llm.service'],check=True)
    if old:
        temp.symlink_to(old);os.replace(temp,current)
        if record['previous_unit']:old_unit.write_text(record['previous_unit'])
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True)
    # Never restore conflicting auto-starts on a failed first install.
    raise
record.update(health=health,legacy_disabled=True,activated_at=time.time())
backup.write_text(json.dumps(record,indent=2))
print(json.dumps({'deployed':True,'release':str(release),'health':health,'legacy_auto_start_disabled':True}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', 'rev-parse', args.commit + '^{commit}'], cwd=root, text=True).strip()
    contents = {name: subprocess.check_output(['git', 'show', revision + ':' + name], cwd=root) for name in FILES}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = {'release': 'nx5-ssd-llm-' + revision[:12], 'source_commit': revision,
                'commit': fingerprint, 'files': hashes}
    payload = {'apply': args.apply, 'manifest': manifest,
               'files': {name: base64.b64encode(data).decode() for name, data in contents.items()}}
    subprocess.run(['ssh', '-o', 'Hostname=192.168.100.154', '-o', 'BatchMode=yes', 'nx5', 'python3', '-'],
                   input=REMOTE.replace('PAYLOAD', repr(payload), 1), text=True, check=True)


if __name__ == '__main__':
    main()
