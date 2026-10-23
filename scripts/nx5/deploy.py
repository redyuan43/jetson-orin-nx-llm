#!/usr/bin/env python3
"""Publish an immutable committed nx5 service bundle; preflight unless --apply."""
import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess


def upgrade_config(existing, defaults, *, managed_comfy=False, llm_profile_version=None,
                   flint_profile_version=None):
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
    if result.get('managed_comfy') is True and flint_profile_version is not None:
        profiles = result.get('policy_profiles')
        if isinstance(profiles, dict):
            profile = profiles.get('flint')
            if profile is None:
                profiles['flint'] = {'profile_id': 'nx5-flint', 'version': flint_profile_version}
            elif (not isinstance(profile, dict) or set(profile) != {'profile_id', 'version'}
                  or profile.get('profile_id') != 'nx5-flint'):
                raise RuntimeError('flint_policy_profile_invalid')
            else:
                profile['version'] = flint_profile_version
    return result


def check_config_baseline(raw, expected, *, required=False):
    if raw is not None and required and not expected:
        raise RuntimeError('config_baseline_required')
    actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if expected is not None and actual != expected:
        raise RuntimeError('config_baseline_changed')


def validate_flint_recovery_snapshot(snapshot, operation_id, current_source_sha, profile_version):
    if (not re.fullmatch(r'op_[a-f0-9]{32}', operation_id)
            or not re.fullmatch(r'[a-f0-9]{40}', current_source_sha)
            or not re.fullmatch(r'[a-f0-9]{64}', profile_version)):
        raise RuntimeError('flint_recovery_identity_invalid')
    if snapshot.get('host') != 'nx5' or snapshot.get('tailnet_ipv4') != '100.118.44.27':
        raise RuntimeError('wrong_target_identity')
    if snapshot.get('current_source_sha') != current_source_sha:
        raise RuntimeError('current_release_changed')
    owner = snapshot.get('owner_health') or {}
    if (owner.get('source_commit') != current_source_sha or owner.get('state') != 'ready'
            or owner.get('mode') != 'off' or owner.get('active') != 0
            or owner.get('model_loaded') is not False or owner.get('resident_pid') is not None
            or owner.get('lease_status') != 'released' or owner.get('resident_error') is not None):
        raise RuntimeError('owner_not_fresh_off')
    if snapshot.get('owner_unit', {}).get('active') != 'active':
        raise RuntimeError('owner_service_not_active')
    if snapshot.get('native_unit', {}).get('active') not in {'inactive', 'failed'}:
        raise RuntimeError('flint_native_not_off')
    if snapshot.get('comfy_unit', {}).get('active') not in {'inactive', 'failed'}:
        raise RuntimeError('comfy_not_off')
    gate = snapshot.get('gate_activity') or {}
    if (gate.get('profile_id') != 'nx5-flint' or gate.get('profile_digest') != profile_version
            or gate.get('mode') != 'flint' or gate.get('enabled') is not True
            or gate.get('public_enabled') is not False or gate.get('qualification_only') is not True
            or gate.get('owner_admission') is not False or gate.get('active') != 0
            or gate.get('unknown') != 0 or gate.get('waiting') != 1
            or gate.get('ingress_closed') is not True):
        raise RuntimeError('flint_gate_not_single_qualification_waiting')
    if snapshot.get('models') != []:
        raise RuntimeError('flint_gate_models_not_closed')
    request = snapshot.get('gate_request') or {}
    if request.get('operation_id') != operation_id or request.get('state') not in {'preparing', 'reservation_unknown'}:
        raise RuntimeError('original_flint_request_not_pending')
    group = snapshot.get('central_activity') or {}
    transition = group.get('transition') or {}
    if (group.get('group_id') != 'nx5-igpu-group' or group.get('resources') != ['nx5-igpu']
            or group.get('fresh') is not True or group.get('leases') != []
            or transition.get('executor_id') != 'nx5-text'
            or transition.get('operation_id') != operation_id or transition.get('target_mode') != 'flint'):
        raise RuntimeError('original_flint_transition_changed')
    pending = [row for row in group.get('pending', []) if isinstance(row, dict)]
    if (len(pending) != 1 or pending[0].get('executor_id') != 'nx5-text'
            or pending[0].get('operation_id') != operation_id
            or pending[0].get('status') not in {'waiting', 'preparing'}
            or pending[0].get('resources') != [{'resource_id': 'nx5-igpu', 'units': 1}]):
        raise RuntimeError('original_flint_pending_changed')
    return True


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
                       llm_profile_version=p.get('llm_profile_version'),
                       flint_profile_version=p.get('flint_profile_version'))
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


REMOTE_FLINT_RECOVERY = r'''
import base64,fcntl,hashlib,json,os,pathlib,shlex,subprocess,tempfile,time,urllib.request,urllib.error
p=PAYLOAD;home=pathlib.Path.home();base=home/'.local/share/nx5-ssd-llm';private=home/'.config/nx5-ssd-llm';state=home/'.local/state/nx5-ssd-llm';units=home/'.config/systemd/user'
def need(value,code):
 if not value:raise RuntimeError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def unit(name):
 out=subprocess.check_output(['systemctl','--user','show',name,'--property=ActiveState','--property=UnitFileState','--property=MainPID'],text=True,timeout=8)
 values=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
 return {'active':values.get('ActiveState'),'enabled':values.get('UnitFileState'),'pid':int(values.get('MainPID','0') or 0)}
def auth(path):return pathlib.Path(path).read_text().strip()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(url,token):
 request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token})
 with opener.open(request,timeout=8) as response:return json.load(response)
def snapshot(owner_required=True):
 host=subprocess.check_output(['hostname','-s'],text=True).strip();ips=subprocess.check_output(['tailscale','ip','-4'],text=True).split()
 current=base/'current';need(current.is_symlink(),'current_not_symlink');target=os.readlink(current);release_manifest=json.loads((current/'release.json').read_text())
 service_cfg=private/'service.json';unit_file=units/'nx5-ssd-llm.service'
 gate_private=home/'.config/siyuan-nx5-flint';gate_token=auth(gate_private/'controller.key')
 gate=get('http://127.0.0.1:18085/internal/activity',gate_token)
 models=get('http://127.0.0.1:18085/v1/models',auth(home/'.config/nx5-flint/api.key'))
 gate_request=get('http://127.0.0.1:18085/internal/requests/'+p['operation_id'],gate_token)
 owner=get('http://100.118.44.27:18347/healthz',auth(private/'client.key')) if owner_required else None
 text_private=home/'.config/siyuan-text-ingress';central_cfg=json.loads((text_private/'ingress.json').read_text())['central'];env={}
 for line in (text_private/'credentials.env').read_text().splitlines():
  for word in shlex.split(line,comments=True):
   if '=' in word:key,value=word.split('=',1);env[key]=value
 group=get(central_cfg['base_url'].rstrip('/')+'/internal/v1/resource-groups/nx5-igpu-group/activity',env[central_cfg['auth']['token_env']])
 return {'host':host,'tailnet_ipv4':'100.118.44.27' if '100.118.44.27' in ips else None,
  'current_target':target,'current_source_sha':release_manifest.get('source_commit'),
  'service_config_sha256':sha(service_cfg.read_bytes()),'service_unit_sha256':sha(unit_file.read_bytes()),
  'owner_unit':unit('nx5-ssd-llm.service'),'native_unit':unit('nx5-flint.service'),
  'comfy_unit':unit('siyuan-image-comfy.service'),
  'owner_health':{k:owner.get(k) for k in ('source_commit','state','mode','active','model_loaded','resident_pid','lease_status','resident_error')} if owner else {},
  'gate_activity':{k:gate.get(k) for k in ('profile_id','profile_digest','mode','enabled','public_enabled','qualification_only','owner_admission','active','unknown','waiting','ingress_closed')},
  'models':models.get('data'),'gate_request':{k:gate_request.get(k) for k in ('operation_id','state','reason','request_digest','response_digest')},
  'central_activity':{k:group.get(k) for k in ('group_id','resources','fresh','mode','state','active','leases','pending','transition')}}
def atomic(path,raw,mode=0o600):
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(prefix=path.name+'.pending-',dir=path.parent)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
 os.chmod(tmp,mode);os.replace(tmp,path)
def journal(path,record,phase,**extra):
 record.update(phase=phase,**extra);atomic(path,(json.dumps(record,sort_keys=True)+'\n').encode())
need(subprocess.check_output(['hostname','-s'],text=True).strip()=='nx5','wrong_target_identity')
current=snapshot();validate_flint_recovery_snapshot(current,p['operation_id'],p['expected_current_source_sha'],p['flint_profile_version'])
need(current['service_config_sha256']==p['expected_config_sha256'],'service_config_cas_conflict')
need(current['service_unit_sha256']==p['expected_unit_sha256'],'service_unit_cas_conflict')
need(current['owner_unit']['pid']>1,'owner_pid_unknown')
release=base/'releases'/p['manifest']['source_commit'];need(not release.exists(),'immutable_release_already_exists')
need(current['current_target']==p['expected_current_target'],'current_link_cas_conflict')
state.mkdir(mode=0o700,parents=True,exist_ok=True);lock=os.open(state/'.flint-transition-recovery.lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
journal_path=state/('flint-transition-recovery-'+p['operation_id']+'-'+p['manifest']['source_commit']+'.json')
need(not journal_path.exists(),'original_flint_recovery_query_only')
backup=state/('flint-transition-backup-'+p['operation_id']+'-'+p['manifest']['source_commit']);backup.mkdir(mode=0o700,parents=True,exist_ok=False)
atomic(backup/'service.json',(private/'service.json').read_bytes());atomic(backup/'nx5-ssd-llm.service',(units/'nx5-ssd-llm.service').read_bytes())
record={'operation_id':p['operation_id'],'source_commit':p['manifest']['source_commit'],'previous_source_sha':current['current_source_sha'],
 'previous_target':current['current_target'],'config_sha256':current['service_config_sha256'],'unit_sha256':current['service_unit_sha256'],
 'profile_version':p['flint_profile_version'],'phase':'prepared'}
atomic(backup/'manifest.json',(json.dumps(record,sort_keys=True)+'\n').encode());journal(journal_path,record,'prepared')
release.mkdir(parents=True,exist_ok=False)
for name,item in p['files'].items():
 raw=base64.b64decode(item['body'],validate=True);need(sha(raw)==item['sha256'],'package_hash_mismatch')
 path=release/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
atomic(release/'release.json',(json.dumps(p['manifest'],sort_keys=True,indent=2)+'\n').encode(),0o644)
need(unit('nx5-ssd-llm.service')['pid']==current['owner_unit']['pid'],'owner_pid_changed')
journal(journal_path,record,'stopping_owner')
try:
 subprocess.run(['systemctl','--user','stop','nx5-ssd-llm.service'],check=True,timeout=60)
 need(unit('nx5-ssd-llm.service')['active'] in ('inactive','failed'),'owner_stop_unconfirmed')
 need(sha((private/'service.json').read_bytes())==p['expected_config_sha256'],'service_config_cas_conflict')
 need(sha((units/'nx5-ssd-llm.service').read_bytes())==p['expected_unit_sha256'],'service_unit_cas_conflict')
 after=snapshot(owner_required=False)
 need(after['native_unit']['active'] in ('inactive','failed') and after['comfy_unit']['active'] in ('inactive','failed'),'exclusive_device_changed')
 need(after['gate_request']['operation_id']==p['operation_id'] and after['gate_request']['state'] in ('preparing','reservation_unknown'),'original_gate_request_changed')
 need(after['central_activity']['transition']==current['central_activity']['transition'] and after['central_activity']['leases']==[],'original_central_transition_changed')
 link=base/'current';need(os.readlink(link)==p['expected_current_target'],'current_link_cas_conflict')
 pending=base/('current.pending-'+p['manifest']['source_commit']);pending.symlink_to(release);os.replace(pending,link)
 journal(journal_path,record,'new_release_selected')
 subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True,timeout=60)
 journal(journal_path,record,'owner_restarted')
 latest=None;terminal=None
 for _ in range(300):
  try:
   latest=get('http://100.118.44.27:18347/healthz',auth(private/'client.key'))
   if latest.get('source_commit')==p['manifest']['source_commit']:
    status=get('http://127.0.0.1:18085/internal/requests/'+p['operation_id'],auth(home/'.config/siyuan-nx5-flint/controller.key'))
    if status.get('state') in ('succeeded','failed','cancelled','unknown','release_unknown'):
     terminal={k:status.get(k) for k in ('operation_id','state','request_digest','backend_id','reason','response_digest')};break
  except Exception:pass
  time.sleep(1)
 journal(journal_path,record,'owner_recovery_finished' if terminal else 'owner_recovery_pending',
  owner={k:latest.get(k) for k in ('source_commit','state','mode','active','model_loaded','resident_pid','lease_status','resident_error')} if latest else None,
  terminal_operation=terminal)
 print(json.dumps({'phase':record['phase'],'source_commit':p['manifest']['source_commit'],'operation_id':p['operation_id'],
  'terminal_operation':terminal,'owner_pid':unit('nx5-ssd-llm.service')['pid'],'journal':str(journal_path)},sort_keys=True))
except Exception as exc:
 current_link=os.readlink(base/'current') if (base/'current').is_symlink() else None
 if current_link==p['expected_current_target'] and unit('nx5-ssd-llm.service')['active'] in ('inactive','failed'):
  try:subprocess.run(['systemctl','--user','start','nx5-ssd-llm.service'],check=True,timeout=60);journal(journal_path,record,'old_owner_restarted',failure_type=type(exc).__name__)
  except Exception as restart_exc:journal(journal_path,record,'owner_recovery_unknown',failure_type=type(exc).__name__,restart_failure_type=type(restart_exc).__name__)
 elif current_link!=p['expected_current_target']:
  journal(journal_path,record,'owner_recovery_unknown',failure_type=type(exc).__name__,selected_target=current_link)
 else:journal(journal_path,record,'owner_recovery_unknown',failure_type=type(exc).__name__)
 raise
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--managed-comfy', action='store_true', help='enable the reviewed NX5 managed mode owner')
    parser.add_argument('--expected-config-sha256', help='required CAS baseline when enabling an existing configuration')
    parser.add_argument('--expected-unit-sha256', help='exact installed owner unit baseline for scoped Flint recovery')
    parser.add_argument('--flint-profile-version', help='bind the managed Flint policy profile to its fixed runtime identity')
    parser.add_argument('--recover-flint-operation', help='recover this exact pending Flint request after publishing the owner fix')
    parser.add_argument('--expected-current-source-sha', help='exact installed owner source commit baseline for Flint recovery')
    parser.add_argument('--expected-current-target', help='exact current release symlink target for Flint recovery')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', 'rev-parse', args.commit + '^{commit}'], cwd=root, text=True).strip()
    contents = {name: subprocess.check_output(['git', 'show', revision + ':' + name], cwd=root) for name in FILES}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = {'release': 'nx5-ssd-llm-' + revision[:12], 'source_commit': revision,
                'commit': fingerprint, 'files': hashes}
    llm_profile_version = json.loads(contents['configs/nx5-minicpm-service.json'])['revision']
    flint_profile_version = args.flint_profile_version
    if args.recover_flint_operation:
        required = (args.apply, args.expected_config_sha256, args.expected_unit_sha256,
                    args.expected_current_source_sha, args.expected_current_target,
                    flint_profile_version)
        if not all(required):
            parser.error('--recover-flint-operation requires --apply and exact source, symlink, config, unit, and profile baselines')
        payload = {'operation_id': args.recover_flint_operation,
                   'expected_current_source_sha': args.expected_current_source_sha,
                   'expected_current_target': args.expected_current_target,
                   'expected_config_sha256': args.expected_config_sha256,
                   'expected_unit_sha256': args.expected_unit_sha256,
                   'flint_profile_version': flint_profile_version,
                   'manifest': manifest,
                   'files': {name: {'body': base64.b64encode(data).decode(), 'sha256': hashes[name]}
                             for name, data in contents.items()}}
        helpers='\n'.join(inspect.getsource(f) for f in (validate_flint_recovery_snapshot,))+'\n'
        subprocess.run(remote_ssh_command(['python3', '-']),
                       input=helpers+REMOTE_FLINT_RECOVERY.replace('PAYLOAD', repr(payload), 1),
                       text=True, check=True)
        return
    payload = {'apply': args.apply, 'manifest': manifest, 'managed_comfy':args.managed_comfy,
               'expected_config_sha256':args.expected_config_sha256,
               'llm_profile_version':llm_profile_version,
               'flint_profile_version':flint_profile_version,
               'files': {name: base64.b64encode(data).decode() for name, data in contents.items()}}
    helpers='\n'.join(inspect.getsource(f) for f in (upgrade_config,check_config_baseline))+'\n'
    subprocess.run(remote_ssh_command(['python3', '-']),
                   input=helpers+REMOTE.replace('PAYLOAD', repr(payload), 1), text=True, check=True)


if __name__ == '__main__':
    main()
