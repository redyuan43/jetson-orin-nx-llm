#!/usr/bin/env python3
"""Publish an immutable committed nx5 service bundle; preflight unless --apply."""
import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import subprocess


def upgrade_config(existing, defaults, *, managed_comfy=False, llm_profile_version=None):
    import copy

    if existing is not None and any(existing.get(k) != v for k, v in defaults.items()):
        raise RuntimeError('service_config_changed')
    result = copy.deepcopy(defaults if existing is None else existing)
    if managed_comfy:
        result['managed_comfy'] = True
    if result.get('managed_comfy') is True and llm_profile_version is not None:
        profiles = result.get('policy_profiles')
        if isinstance(profiles, dict) and 'llm' in profiles:
            profile = profiles['llm']
            if not isinstance(profile, dict) or set(profile) != {'profile_id', 'version'}:
                raise RuntimeError('llm_policy_profile_invalid')
            profile['version'] = llm_profile_version
    return result


def check_config_baseline(raw, expected, *, required=False):
    if raw is not None and required and not expected:
        raise RuntimeError('config_baseline_required')
    actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if expected is not None and actual != expected:
        raise RuntimeError('config_baseline_changed')


def remote_ssh_command(command):
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'nx5', *command]


FILES = ['scripts/nx5/resident.py', 'scripts/nx5/compat_api.py', 'configs/nx5-chat-runtime.json', 'scripts/nx5/chat-runner.cpp', 'scripts/nx5/service.py', 'scripts/nx5/run-model.py', 'scripts/minicpm/bench-audit.py',
         'configs/qwen36-iq4xs-128k.json', 'configs/nx5-minicpm-service.json',
         'configs/minicpm-v47-instruct.jinja', 'docs/nx5-service/API.md',
         'docs/nx5-service/catalog.json', 'docs/nx5-service/openapi.json',
         'deploy/nx5/nx5-ssd-llm.service']

REMOTE = r'''
import base64,hashlib,json,os,secrets,shlex,subprocess,time,urllib.request,tempfile,uuid
from pathlib import Path
host_identity=subprocess.check_output(['hostname','-s'],text=True).strip()
tailnet_addresses=subprocess.check_output(['tailscale','ip','-4'],text=True).split()
if host_identity!='nx5' or '100.118.44.27' not in tailnet_addresses:
    raise RuntimeError('wrong_target_identity')
p=PAYLOAD
home=Path.home(); base=home/'.local/share/nx5-ssd-llm'; private=home/'.config/nx5-ssd-llm'
state=home/'.local/state/nx5-ssd-llm'; unit_dir=home/'.config/systemd/user'
config=private/'service.json'
config_raw=config.read_bytes() if config.exists() else None
check_config_baseline(config_raw, p.get('expected_config_sha256'), required=p.get('managed_comfy',False))
defaults={'root':str(home/'github/qwen38-kvmem'),'state':str(state/'jobs'),
          'client_token_file':str(private/'client.key'),'bind':'100.118.44.27','port':18347}
desired=upgrade_config(json.loads(config_raw) if config_raw is not None else None, defaults,
                       managed_comfy=p.get('managed_comfy',False),
                       llm_profile_version=p.get('llm_profile_version'))
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
unit_raw=old_unit.read_bytes() if old_unit.exists() else None
if current.exists() and not current.is_symlink():raise RuntimeError('current_not_symlink')
record={'source_commit':p['manifest']['source_commit'],'previous_release':old,
        'legacy_enabled_before':{u:state_of(u,'UnitFileState') for u in legacy},
        'previous_unit':unit_raw.decode() if unit_raw is not None else None,
        'previous_config':base64.b64encode(config_raw).decode() if config_raw is not None else None,
        'phase':'prepared','owned':[]}
os.umask(0o077)
def atomic_bytes(path,raw):
    fd,name=tempfile.mkstemp(prefix=path.name+'.pending-',dir=path.parent)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(name,path)
def journal(phase,**fields):
    record.update(phase=phase,**fields)
    atomic_bytes(backup,(json.dumps(record,indent=2)+'\n').encode())
def link_value():
    if current.is_symlink():return os.readlink(current)
    if current.exists():raise RuntimeError('rollback_ownership_lost')
    return None
original={'config':config_raw,'unit':unit_raw,'link':old}
expected=dict(original)
owned=set()
def check_ownership():
    actual={'config':config.read_bytes() if config.exists() else None,
            'unit':old_unit.read_bytes() if old_unit.exists() else None,'link':link_value()}
    if actual!=expected:raise RuntimeError('rollback_ownership_lost')
def replace_control(name,raw):
    check_ownership()
    journal('writing_'+name)
    if name=='link':
        pending=base/('current.pending-'+uuid.uuid4().hex)
        pending.symlink_to(raw);os.replace(pending,current)
    else:
        atomic_bytes(config if name=='config' else old_unit,raw)
    expected[name]=raw;owned.add(name)
    journal('installed_'+name,owned=sorted(owned))
def unresolved_jobs():
    result=[]
    for path in (state/'jobs').glob('op_*.json'):
        try:terminal=json.loads(path.read_text()).get('status') in ('succeeded','failed')
        except (ValueError,OSError):terminal=False
        if not terminal:result.append(path.stem)
    return result
journal('prepared')
token=private/'client.key'
if not token.exists():token.write_text(secrets.token_urlsafe(48)+'\n');token.chmod(0o600)
desired_raw=(json.dumps(desired,indent=2)+'\n').encode()
candidate_start_attempted=False
try:
    # Every owned mutable control is protected, including failures before start.
    replace_control('config',desired_raw)
    subprocess.run(['systemctl','--user','disable','--now',*legacy],check=True)
    replace_control('unit',(release/'deploy/nx5/nx5-ssd-llm.service').read_bytes())
    replace_control('link',str(release))
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','nx5-ssd-llm.service'],check=True)
    check_ownership()
    candidate_start_attempted=True
    journal('starting_candidate')
    subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True)
    for _ in range(20):
        try:
            with opener.open('http://100.118.44.27:18347/healthz',timeout=2) as response:health=json.load(response)
            if health.get('commit')==p['manifest']['commit'] and health.get('state') in ('ready','busy'):break
        except Exception:pass
        time.sleep(1)
    else:raise RuntimeError('health_failed')
    check_ownership()
except Exception as failure:
    journal('activation_failed',failure_type=type(failure).__name__)
    try:
        # A change to any control aborts all rollback writes and lifecycle work.
        check_ownership()
    except Exception:
        journal('ownership_lost')
        raise RuntimeError('rollback_ownership_lost') from failure
    pending=unresolved_jobs()
    if pending:
        journal('reconciliation_required',pending_operations=pending)
        raise RuntimeError('rollback_pending_jobs') from failure
    try:
        if candidate_start_attempted:
            check_ownership()
            subprocess.run(['systemctl','--user','stop','nx5-ssd-llm.service'],check=True)
        check_ownership()
        pending=unresolved_jobs()
        if pending:
            journal('reconciliation_required',pending_operations=pending)
            raise RuntimeError('rollback_pending_jobs')
        if old:
            # All three controls were checked together before restoring any.
            for name in ('config','unit','link'):
                if name not in owned:continue
                if original[name] is None:raise RuntimeError('rollback_original_control_missing')
                replace_control(name,original[name])
            subprocess.run(['systemctl','--user','daemon-reload'],check=True)
            subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True)
            journal('rolled_back')
        else:
            journal('first_install_failed')
    except Exception as rollback_failure:
        if record['phase']!='reconciliation_required':
            journal('ownership_lost' if str(rollback_failure)=='rollback_ownership_lost' else 'rollback_failed',
                    rollback_failure_type=type(rollback_failure).__name__)
        raise
    # Business state is never copied back from a backup or replayed.
    raise
record.update(health=health,legacy_disabled=True,activated_at=time.time())
journal('activated')
print(json.dumps({'deployed':True,'release':str(release),'health':health,'legacy_auto_start_disabled':True}))

'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--managed-comfy', action='store_true', help='enable the reviewed NX5 managed mode owner')
    parser.add_argument('--expected-config-sha256', help='required CAS baseline when enabling an existing configuration')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', 'rev-parse', args.commit + '^{commit}'], cwd=root, text=True).strip()
    contents = {name: subprocess.check_output(['git', 'show', revision + ':' + name], cwd=root) for name in FILES}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = {'release': 'nx5-ssd-llm-' + revision[:12], 'source_commit': revision,
                'commit': fingerprint, 'files': hashes}
    llm_profile_version = json.loads(contents['configs/nx5-minicpm-service.json'])['revision']
    payload = {'apply': args.apply, 'manifest': manifest, 'managed_comfy':args.managed_comfy,
               'expected_config_sha256':args.expected_config_sha256,
               'llm_profile_version':llm_profile_version,
               'files': {name: base64.b64encode(data).decode() for name, data in contents.items()}}
    helpers='\n'.join(inspect.getsource(f) for f in (upgrade_config,check_config_baseline))+'\n'
    subprocess.run(remote_ssh_command(['python3', '-']),
                   input=helpers+REMOTE.replace('PAYLOAD', repr(payload), 1), text=True, check=True)


if __name__ == '__main__':
    main()
