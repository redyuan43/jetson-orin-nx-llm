#!/usr/bin/env python3
"""Build and audit the pinned NX5 load-only runtime bundle; never activates it."""
import argparse
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import time
import urllib.request

BASELINE = '984530f50d6c6abf6b3b0b5c9d682f0a0b1f5c58'
VENDOR_COMMIT = '00420e8ada805f35a91d31c796b11b2c9cfe4284'
GRAPH = 'configs/nx5-qwen21-weight-preload-graph.json'
LOCAL_PRELOAD_FILES = {
    'configs/nx5-qwen21-weight-preload-graph.json',
    'scripts/minicpm/vendor/fleet_control/__init__.py',
    'scripts/minicpm/vendor/fleet_control/comfy_preload.py',
    'scripts/minicpm/vendor/fleet_control/weight_preload_protocol.py',
    'scripts/minicpm/vendor/weight_preload_node/__init__.py',
}
ALLOWED_DELTA = LOCAL_PRELOAD_FILES | {
    'scripts/nx5/resident.py', 'scripts/nx5/deploy.py',
    'scripts/nx5/publish-preload.py', 'tests/test_nx5_resident.py',
    'tests/test_nx5_preload_release.py', 'scripts/minicpm/vendor/source-lock.json',
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)


def source_files(repo, revision):
    namespace = {'__name__': '_nx5_preload_deploy',
                 '__file__': str(Path(repo).resolve()/'scripts/nx5/deploy.py')}
    raw = git(repo, 'show', revision+':scripts/nx5/deploy.py')
    exec(compile(raw, 'scripts/nx5/deploy.py', 'exec'), namespace)
    return sorted(set(namespace['FILES']) | LOCAL_PRELOAD_FILES |
                  {'scripts/nx5/deploy.py', 'scripts/nx5/publish-preload.py'})


def next_phase(current, action):
    allowed = {'captured': {'prepare': 'prepared'}, 'prepared': {'apply': 'applying'},
               'applying': {}, 'unknown': {}, 'activated': {}, 'verified': {}}
    try:
        return allowed[current][action]
    except KeyError:
        raise ValueError('inspect_only:'+str(current)) from None


def validate_central_snapshot(snapshot, *, operation, revision, now):
    if (not isinstance(operation, str) or not re.fullmatch(
            r'nx5-(?:preload|weight-preload-maintenance|weight-preload-publish)-[A-Za-z0-9-]{1,48}', operation)
            or type(revision) is not int or revision < 0):
        raise ValueError('maintenance_identity_invalid')
    resource = snapshot.get('resource') or {}
    group = snapshot.get('group') or {}
    public = snapshot.get('public') or {}
    leases = snapshot.get('leases')
    if (resource.get('resource_id') != 'nx5-igpu' or resource.get('capacity') != 0
            or resource.get('occupied') != 0 or resource.get('revision') != revision):
        raise ValueError('maintenance_capacity_cas_mismatch')
    if (group.get('group_id') != 'nx5-igpu-group'
            or group.get('resources') not in (None, ['nx5-igpu'])
            or group.get('policy_demand') is not None or group.get('mode') != 'comfy'
            or group.get('state') != 'ready' or group.get('active') != 0
            or group.get('transition') is not None or group.get('pending') not in (None, [])):
        raise ValueError('central_group_not_idle_off')
    if not isinstance(leases, list) or leases:
        raise ValueError('central_leases_not_empty')
    observed = group.get('observed_at')
    public_observed = public.get('observed_at')
    if (not isinstance(observed, (int, float)) or not 0 <= now-observed <= 60
            or not isinstance(public_observed, (int, float)) or not 0 <= now-public_observed <= 60):
        raise ValueError('central_observation_stale')
    if (public.get('mode') != 'comfy' or public.get('state') != 'ready'
            or public.get('active') != 0 or public.get('fresh') is not True
            or public.get('transition_pending') is not False
            or public.get('policy_demand') is not None):
        raise ValueError('public_group_not_fresh_idle')
    return True


def central_baseline_matches(before, after):
    for section in ('resource', 'leases'):
        if before.get(section) != after.get(section):
            return False
    for section, volatile in (('group', {'observed_at'}), ('public', {'observed_at'})):
        old = {key: value for key, value in before.get(section, {}).items() if key not in volatile}
        new = {key: value for key, value in after.get(section, {}).items() if key not in volatile}
        if old != new:
            return False
    return True


RESTARTED_UNITS = {'nx5-ssd-llm.service', 'siyuan-image-comfy.service'}


def unaffected_processes(rows, excluded_pids=()):
    excluded_pids = {int(pid) for pid in excluded_pids}
    return [row for row in rows if row.get('pid') not in excluded_pids
            and row.get('unit') not in RESTARTED_UNITS]


def comfy_dropin(current, node_config):
    current = str(current)
    node_config = str(node_config)
    if not current.startswith('/home/nx/.local/share/nx5-ssd-llm/'):
        raise ValueError('release_path_invalid')
    if not node_config.startswith('/home/nx/.config/nx5-ssd-llm/'):
        raise ValueError('private_config_path_invalid')
    return ('[Service]\n'
            'SendSIGKILL=no\n'
            'TimeoutStopSec=infinity\n'
            'Environment="PYTHONPATH='+current+'/scripts/minicpm/vendor"\n'
            'Environment="PYTHONDONTWRITEBYTECODE=1"\n'
            'Environment="SIYUAN_WEIGHT_PRELOAD_CONFIG='+node_config+'"\n')


CENTRAL_URL = 'http://100.91.42.28:18292'
ADMIN_TOKEN_FILE = Path('/home/ai/.local/state/siyuan-gateway-releases/fleet-lifecycle-enrollment-20261023-001/nano1-registration/maintenance-admin.key')


def central_snapshot(token_file=ADMIN_TOKEN_FILE, *, now=None):
    token = Path(token_file).read_text().strip()
    if not token:
        raise ValueError('central_admin_token_unavailable')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        request = urllib.request.Request(CENTRAL_URL+path,
            headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=10) as response:
                raw = response.read(2*1024*1024+1)
        except Exception:
            raise ValueError('central_request_failed:'+path) from None
        if len(raw) > 2*1024*1024:
            raise ValueError('central_response_too_large')
        return json.loads(raw)

    resources = get('/console/admin/v1/resources')
    leases = get('/console/admin/v1/leases')
    public = get('/agent/device-policies.json')
    rows = [row for row in resources.get('resources', [])
            if isinstance(row, dict) and row.get('resource_id') == 'nx5-igpu']
    groups = [row for row in resources.get('groups', [])
              if isinstance(row, dict) and row.get('group_id') == 'nx5-igpu-group']
    public_groups = [row for row in public.get('groups', [])
                     if isinstance(row, dict) and row.get('group_id') == 'nx5-igpu-group']
    if len(rows) != 1 or len(groups) != 1 or len(public_groups) != 1:
        raise ValueError('central_target_identity_ambiguous')
    now = time.time() if now is None else now
    actual = public_groups[0].get('actual')
    if not isinstance(actual, dict):
        raise ValueError('central_public_actual_missing')
    target_leases = []
    for lease in leases.get('leases', []):
        if not isinstance(lease, dict):
            raise ValueError('central_lease_schema_invalid')
        resources_in_lease = []
        for field in ('resources', 'allocations'):
            entries = lease.get(field, [])
            if isinstance(entries, dict):
                entries = [entries] if 'resource_id' in entries else list(entries.values())
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and item.get('resource_id') == 'nx5-igpu':
                    resources_in_lease.append({'resource_id':'nx5-igpu',
                        'units':item.get('units')})
                elif item == 'nx5-igpu':
                    resources_in_lease.append({'resource_id':'nx5-igpu'})
        if resources_in_lease and lease.get('status') != 'released':
            target_leases.append({'status':lease.get('status'),
                                  'resources':resources_in_lease})
    snapshot = {'resource': {key: rows[0].get(key) for key in
                  ('resource_id', 'capacity', 'occupied', 'revision')},
        'leases': target_leases,
        'group': {key: groups[0].get(key) for key in
                  ('group_id', 'resources', 'mode', 'state', 'active', 'transition', 'pending', 'observed_at', 'policy_demand')},
        'public': {key: actual.get(key) for key in
                  ('mode', 'state', 'active', 'fresh', 'transition_pending', 'observed_at', 'policy_demand')}}
    return snapshot


REMOTE_PRELOAD = r'''import base64,hashlib,json,os,pathlib,secrets,shlex,stat,subprocess,sys,tempfile,time,urllib.request
P=PAYLOAD; action=P['action']; home=pathlib.Path.home(); base=home/'.local/share/nx5-ssd-llm'
private=home/'.config/nx5-ssd-llm'; state=home/'.local/state/nx5-ssd-llm'; units=home/'.config/systemd/user'
RESTARTED_UNITS={'nx5-ssd-llm.service','siyuan-image-comfy.service'}
def unaffected_processes(rows,excluded_pids=()):
 excluded_pids={int(pid) for pid in excluded_pids}
 return [row for row in rows if row.get('pid') not in excluded_pids
  and row.get('unit') not in RESTARTED_UNITS]
def unaffected_processes_unchanged(baseline,after):
 if (after['flint_unit']!=baseline['flint_unit']
  or after['flint_processes']!=baseline['flint_processes']): return False
 restarted_pids={int(baseline[name]['MainPID']) for name in ('owner_unit','comfy_unit')}
 restarted_pids.update(int(after[name]['MainPID']) for name in ('owner_unit','comfy_unit'))
 old_python=unaffected_processes(baseline['python_processes'],restarted_pids)
 after_python=unaffected_processes(after['python_processes'],restarted_pids)
 return after_python==old_python
config=private/'service.json'; comfy_drop=units/'siyuan-image-comfy.service.d/zz-siyuan-weight-preload.conf'
node_link=home/'apps/ComfyUI/custom_nodes/siyuan_weight_preload'; plan_path=state/('preload-release-'+P['operation']+'.json')
backup=state/('preload-backup-'+P['operation']); release=base/'releases'/P['source_commit']; current=base/'current'
new_private=(private/'weight-preload-central.token',private/'weight-preload.ticket-key',private/'weight-preload.json')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def need(value,code):
 if not value: raise RuntimeError(code)
def sha(raw): return hashlib.sha256(raw).hexdigest()
def get(url,token=None):
 req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token} if token else {})
 with opener.open(req,timeout=8) as response:
  raw=response.read(2*1024*1024+1)
 if len(raw)>2*1024*1024: raise RuntimeError('response_too_large')
 return json.loads(raw)
def unit(name):
 raw=subprocess.check_output(['systemctl','--user','show',name,'-p','ActiveState','-p','MainPID','-p','InvocationID','-p','FragmentPath','-p','DropInPaths'],text=True,timeout=8)
 row=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
 return {key:row.get(key) for key in ('ActiveState','MainPID','InvocationID','FragmentPath','DropInPaths')}
def proc_rows(needle=None):
 rows=[]
 for entry in pathlib.Path('/proc').iterdir():
  if not entry.name.isdigit() or int(entry.name)==os.getpid(): continue
  try:
   raw=(entry/'cmdline').read_bytes(); exe=(entry/'exe').resolve(strict=True).name
   fields=(entry/'stat').read_text().split(); start=fields[21]
   cgroup=(entry/'cgroup').read_text(errors='replace')
  except (OSError,IndexError): continue
  if not exe.startswith('python'): continue
  if needle and needle not in raw.lower(): continue
  unit_name=next((part for line in cgroup.splitlines() for part in line.split('/')
    if part.endswith('.service')),None)
  rows.append({'pid':int(entry.name),'starttime':start,'argv_sha256':sha(raw),'exe':exe,'unit':unit_name})
 return sorted(rows,key=lambda row:row['pid'])
def jobs():
 rows=[]; root=state/'jobs'
 if root.exists():
  for path in sorted(root.glob('op_*.json')):
   try: item=json.loads(path.read_text()); status=item.get('status')
   except Exception: status='unknown'
   rows.append({'operation':path.stem,'status':status,'sha256':sha(path.read_bytes())})
 digest=sha(json.dumps(rows,sort_keys=True,separators=(',',':')).encode())
 terminal=sum(1 for row in rows if row['status'] in ('succeeded','failed'))
 return {'count':len(rows),'terminal_count':terminal,'nonterminal_count':len(rows)-terminal,'sha256':digest}
def verify_release(path,manifest):
 root=pathlib.Path(path)
 need(root.is_dir() and not root.is_symlink(),'release_directory_invalid')
 expected=manifest.get('files')
 need(isinstance(expected,dict) and expected,'release_manifest_files_invalid')
 entries=list(root.rglob('*'))
 need(all(not item.is_symlink() for item in entries),'release_symlink_forbidden')
 actual={str(item.relative_to(root)) for item in entries if item.is_file() and item.name!='release.json'}
 need(actual==set(expected),'release_file_set_mismatch')
 for name,digest in expected.items():
  item=root/name
  need(not item.is_symlink() and item.is_file(),'release_file_type_invalid')
  need(sha(item.read_bytes())==digest,'release_file_hash_mismatch:'+name)
 return True
def comfy_unit_files(row):
 fragment=row['comfy_unit'].get('FragmentPath')
 need(fragment and pathlib.Path(fragment).is_file(),'comfy_unit_fragment_missing')
 dropins=row['comfy_unit'].get('DropInPaths') or ''
 paths=[pathlib.Path(value) for value in dropins.split() if value]
 return {'fragment':{'path':fragment,'sha256':sha(pathlib.Path(fragment).read_bytes())},
  'dropins':[{'path':str(path),'sha256':sha(path.read_bytes())} for path in paths if path.is_file()]}
def proc_environment(pid):
 allowed={'PYTHONPATH','PYTHONDONTWRITEBYTECODE','SIYUAN_WEIGHT_PRELOAD_CONFIG'}
 raw=pathlib.Path('/proc/'+str(pid)+'/environ').read_bytes().split(b'\0')
 result={}
 for item in raw:
  key,sep,value=item.partition(b'=')
  if sep and key.decode(errors='ignore') in allowed:result[key.decode()]=value.decode(errors='strict')
 return result
def snapshot():
 need(subprocess.check_output(['hostname','-s'],text=True).strip()=='nx5','wrong_target')
 ips=subprocess.check_output(['tailscale','ip','-4'],text=True).split(); need('100.118.44.27' in ips,'wrong_tailnet_identity')
 need(current.is_symlink(),'current_release_not_symlink'); target=os.readlink(current)
 manifest=json.loads((current/'release.json').read_text())
 resolved=current.resolve(strict=True)
 need(resolved.parent==(base/'releases').resolve(),'current_release_outside_root')
 verify_release(resolved,manifest)
 health=get('http://100.118.44.27:18347/healthz')
 queue=get('http://127.0.0.1:8188/queue')
 row={'current_target':target,'source_commit':manifest.get('source_commit'),
  'config_sha256':sha(config.read_bytes()),'comfy_dropin_sha256':sha(comfy_drop.read_bytes()) if comfy_drop.exists() else None,
  'node_link':os.readlink(node_link) if node_link.is_symlink() else ('present' if node_link.exists() else None),
  'owner_unit':unit('nx5-ssd-llm.service'),'comfy_unit':unit('siyuan-image-comfy.service'),
  'flint_unit':unit('nx5-flint.service'),'owner':{**{k:health.get(k) for k in ('source_commit','state','mode','active','model_loaded','resident_pid','lease_status')},
   'mode_phase':(health.get('managed_comfy') or {}).get('phase'),'policy_state':(health.get('managed_comfy') or {}).get('policy_state')},
  'queue':{'running':len(queue.get('queue_running',[])),'pending':len(queue.get('queue_pending',[]))},
  'jobs':jobs(),'python_processes':proc_rows(),'flint_processes':proc_rows(b'flint')}
 row['comfy_unit_files']=comfy_unit_files(row)
 row['new_artifacts']={str(path):path.exists() or path.is_symlink() for path in new_private+(node_link,)}
 return row
def stable(row):
 return {key:row.get(key) for key in ('current_target','source_commit','config_sha256','comfy_dropin_sha256','node_link',
  'owner_unit','comfy_unit','flint_unit','comfy_unit_files','new_artifacts','owner','queue','jobs','python_processes','flint_processes')}
def atomic(path,raw,mode=0o600,create_only=False,expected_current_sha=None):
 path=pathlib.Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 need(not path.is_symlink() and not path.parent.is_symlink(),'unsafe_path')
 if create_only:
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
  with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
  os.chmod(path,mode);return
 if expected_current_sha is not None:
  need(path.is_file() and sha(path.read_bytes())==expected_current_sha,'config_cas_changed')
 fd,tmp=tempfile.mkstemp(prefix=path.name+'.new-',dir=path.parent)
 with os.fdopen(fd,'wb') as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
 os.chmod(tmp,mode);os.replace(tmp,path)
def write_journal(record,phase,**extra):
 record.update(phase=phase,**extra);atomic(plan_path,(json.dumps(record,sort_keys=True)+'\n').encode())
def owner_idle(row):
 owner=row['owner']; unitrow=row['owner_unit']
 need(unitrow.get('ActiveState')=='active' and int(unitrow.get('MainPID','0'))>1,'owner_not_active')
 need(owner.get('state')=='ready' and owner.get('mode')=='comfy' and owner.get('active')==0
  and owner.get('model_loaded') is not True and owner.get('resident_pid') is None,'owner_not_idle_comfy')
 need(row['jobs']['nonterminal_count']==0,'owner_job_not_terminal')
 need(row['queue']=={'running':0,'pending':0},'comfy_queue_not_empty')
 need(row['comfy_unit'].get('ActiveState')=='active','comfy_not_active')
 need(all(value is False for value in row['new_artifacts'].values()),'preload_artifact_already_exists')
 need(row['flint_unit']==P['baseline']['flint_unit'],'flint_unit_changed')
def source_install():
 need(not release.exists(),'release_already_exists')
 release.mkdir(parents=True,mode=0o700)
 for name,item in P['files'].items():
  raw=base64.b64decode(item['content'],validate=True);need(sha(raw)==item['sha256'],'bundle_hash_mismatch')
  path=release/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_bytes(raw);os.chmod(path,0o444)
 (release/'release.json').write_text(json.dumps(P['manifest'],sort_keys=True,indent=2)+'\n');os.chmod(release/'release.json',0o444)
 for path in sorted(release.rglob('*'),key=lambda value:len(value.parts),reverse=True):
  if path.is_dir():os.chmod(path,0o555)
 os.chmod(release,0o555)
def install_config():
 existing=json.loads(config.read_bytes()); spec=P['preload_spec']
 merged=merge_owner_config(existing,spec)
 wrapper=json.loads((home/'.config/siyuan-text-ingress/wrapper.json').read_text())['central']
 envname=wrapper['auth']['token_env']; values=[]
 for line in (home/'.config/siyuan-text-ingress/credentials.env').read_text().splitlines():
  for word in shlex.split(line,comments=True):
   if '=' in word:
    key,value=word.split('=',1)
    if key==envname:values.append(value)
 need(len(values)==1 and values[0],'central_token_unavailable')
 cfg=spec.copy(); cfg['central']=dict(cfg['central']); cfg['central']['token_file']=str(private/'weight-preload-central.token')
 cfg['graph_file']=str(base/'current'/P['graph'])
 merged['policy_profiles']['comfy']['weight_preload']=cfg
 node=json.dumps(cfg,sort_keys=True,indent=2).encode()+b'\n'
 owner=json.dumps(merged,sort_keys=True,indent=2).encode()+b'\n'
 ticket=(secrets.token_urlsafe(32)+'\n').encode('ascii')
 need(all(not path.exists() and not path.is_symlink() for path in new_private+(node_link,)),
  'preload_artifact_cas_conflict')
 atomic(private/'weight-preload-central.token',(values[0]+'\n').encode(),0o600,create_only=True)
 atomic(private/'weight-preload.ticket-key',ticket,0o600,create_only=True)
 atomic(private/'weight-preload.json',node,0o600,create_only=True)
 atomic(config,owner,0o600,expected_current_sha=P['baseline']['config_sha256'])
 old_dropin_sha=P['baseline']['comfy_dropin_sha256']
 atomic(comfy_drop,comfy_dropin(str(base/'current'),str(private/'weight-preload.json')).encode(),0o600,
  create_only=old_dropin_sha is None,expected_current_sha=old_dropin_sha)
 need(not node_link.exists() and not node_link.is_symlink(),'preload_node_path_taken')
 node_link.parent.mkdir(parents=True,exist_ok=True)
 node_link.symlink_to(base/'current/scripts/minicpm/vendor/weight_preload_node')
def verify_live():
 row=snapshot(); need(row['source_commit']==P['source_commit'],'source_commit_not_active')
 active_manifest=json.loads((current/'release.json').read_text())
 need(active_manifest==P['manifest'],'active_release_manifest_mismatch')
 health=get('http://100.118.44.27:18347/healthz')
 need(health.get('state')=='ready' and health.get('mode')=='comfy' and health.get('active')==0
  and health.get('model_loaded') is not True and health.get('resident_pid') is None,'owner_post_restart_not_idle')
 for name in ('owner_unit','comfy_unit'):
  before=P['baseline'][name]; after=row[name]
  need(after.get('MainPID')!=before.get('MainPID') and after.get('InvocationID')!=before.get('InvocationID'),
   'service_restart_not_confirmed:'+name)
 expected_env={'PYTHONPATH':str(base/'current/scripts/minicpm/vendor'),
  'PYTHONDONTWRITEBYTECODE':'1','SIYUAN_WEIGHT_PRELOAD_CONFIG':str(private/'weight-preload.json')}
 comfy_pid=int(row['comfy_unit'].get('MainPID','0'))
 need(comfy_pid>1 and proc_environment(comfy_pid)==expected_env,'comfy_import_environment_mismatch')
 need(row['jobs']==P['baseline']['jobs'],'owner_jobs_changed')
 need(row['comfy_unit_files']['fragment']==P['baseline']['comfy_unit_files']['fragment'],
  'comfy_unit_fragment_changed')
 old_dropins={item['path']:item['sha256'] for item in P['baseline']['comfy_unit_files']['dropins']}
 current_dropins={item['path']:item['sha256'] for item in row['comfy_unit_files']['dropins']}
 need(all(current_dropins.get(path)==digest for path,digest in old_dropins.items()),'comfy_existing_dropin_changed')
 capability=get('http://127.0.0.1:8188/siyuan/weight-preload')
 want=P['capability'];need(capability==want,'preload_capability_mismatch')
 queue=get('http://127.0.0.1:8188/queue');need(not queue.get('queue_running') and not queue.get('queue_pending'),'comfy_queue_changed')
 return {'source_commit':P['source_commit'],'capability':capability,'owner_pid':row['owner_unit']['MainPID'],
  'comfy_pid':row['comfy_unit']['MainPID'],'flint_unit':row['flint_unit'],'flint_processes':row['flint_processes'],
  'jobs':row['jobs'],'queue':row['queue']}
if action=='capture':
 row=snapshot(); print(json.dumps(row,sort_keys=True));sys.exit(0)
if action=='prepare':
 row=snapshot();owner_idle(row);need(stable(row)==P['baseline'],'remote_baseline_changed')
 need(not plan_path.exists() and not backup.exists(),'original_operation_exists_inspect_only')
 state.mkdir(parents=True,exist_ok=True,mode=0o700);backup.mkdir(mode=0o700,parents=True,exist_ok=False)
 shutil_copy=__import__('shutil').copyfile
 shutil_copy(config,backup/'service.json')
 if comfy_drop.exists():shutil_copy(comfy_drop,backup/'comfy-dropin.conf')
 record={'operation':P['operation'],'maintenance_revision':P['maintenance_revision'],
  'source_commit':P['source_commit'],'baseline':stable(row),'phase':'preparing'}
 write_journal(record,'preparing')
 try:
  source_install()
  record['backup_files']={'service.json':sha((backup/'service.json').read_bytes()),
   'comfy_dropin.conf':sha((backup/'comfy-dropin.conf').read_bytes()) if (backup/'comfy-dropin.conf').exists() else None}
  write_journal(record,'prepared')
 except Exception as exc:
  write_journal(record,'prepare_unknown',failure_type=type(exc).__name__);raise
 print(json.dumps({'phase':'prepared','source_commit':P['source_commit'],'operation':P['operation']}));sys.exit(0)
if action=='inspect':
 need(plan_path.exists(),'operation_not_found')
 record=json.loads(plan_path.read_text());live=snapshot()
 print(json.dumps({'journal':record,'live':live},sort_keys=True));sys.exit(0)
if action=='apply':
 need(plan_path.exists(),'prepare_required');record=json.loads(plan_path.read_text())
 need(record.get('operation')==P['operation'] and record.get('phase')=='prepared','inspect_only:'+str(record.get('phase')))
 row=snapshot();owner_idle(row);need(stable(row)==record['baseline'],'remote_baseline_changed')
 write_journal(record,'applying')
 try:
  install_config()
  pending=base/('current.pending-'+P['source_commit']);pending.symlink_to(release);os.replace(pending,current)
  subprocess.run(['systemctl','--user','daemon-reload'],check=True,timeout=30)
  loaded=unit('siyuan-image-comfy.service')
  need(str(comfy_drop) in (loaded.get('DropInPaths') or '').split(),'comfy_dropin_not_loaded')
  write_journal(record,'comfy_restart_started')
  subprocess.run(['systemctl','--user','restart','siyuan-image-comfy.service'],check=True,timeout=90)
  write_journal(record,'owner_restart_started')
  subprocess.run(['systemctl','--user','restart','nx5-ssd-llm.service'],check=True,timeout=90)
  deadline=time.monotonic()+40
  while time.monotonic()<deadline:
   try:receipt=verify_live();break
   except Exception:time.sleep(1)
  else:raise RuntimeError('post_restart_verify_failed')
  after=snapshot()
  need(unaffected_processes_unchanged(record['baseline'],after),'unrelated_python_process_changed')
  record['verification']=receipt;write_journal(record,'verified')
  print(json.dumps({'phase':'verified','source_commit':P['source_commit'],'verification':receipt},sort_keys=True))
 except Exception as exc:
  write_journal(record,'unknown',failure_type=type(exc).__name__);raise
 sys.exit(0)
if action=='verify':
 need(plan_path.exists(),'operation_not_found');record=json.loads(plan_path.read_text())
 need(record.get('phase') in ('verified','unknown'),'not_applied')
 result=verify_live();print(json.dumps({'phase':record['phase'],'verification':result},sort_keys=True));sys.exit(0)
raise RuntimeError('unknown_action')
'''


def package(repo, revision):
    repo = Path(repo)
    if not re.fullmatch(r'[a-f0-9]{40}', revision or ''):
        raise ValueError('full_source_sha_required')
    if git(repo, 'rev-parse', revision+'^{commit}').decode().strip() != revision:
        raise ValueError('source_not_commit')
    if git(repo, 'rev-parse', BASELINE+'^{commit}').decode().strip() != BASELINE:
        raise ValueError('baseline_not_commit')
    changed = set(git(repo, 'diff', '--name-only', BASELINE, revision).decode().splitlines())
    if changed - ALLOWED_DELTA:
        raise ValueError('source_delta_out_of_scope:'+','.join(sorted(changed-ALLOWED_DELTA)))
    files = {}
    for name in source_files(repo, revision):
        entry = git(repo, 'ls-tree', revision, '--', name).decode().split()
        if len(entry) < 4 or entry[:2] != ['100644', 'blob']:
            raise ValueError('source_file_type_invalid:'+name)
        raw = git(repo, 'show', revision+':'+name)
        if name.endswith('.py'):
            compile(raw, name, 'exec')
        files[name] = {'sha256': sha(raw), 'content': base64.b64encode(raw).decode('ascii')}
    lock = json.loads(base64.b64decode(files['scripts/minicpm/vendor/source-lock.json']['content']))
    if lock.get('repository') != 'redyuan43/siyuan-media-gateway' or lock.get('commit') != VENDOR_COMMIT:
        raise ValueError('preload_vendor_commit_changed')
    expected_vendor = {
        'scripts/minicpm/vendor/mode_residency.py': 'fleet-control/fleet_control/mode_residency.py',
        'scripts/minicpm/vendor/comfy_residency/__init__.py': 'fleet-control/compat/candidates/nx4/comfy_residency/__init__.py',
        'scripts/minicpm/vendor/fleet_control/__init__.py': 'fleet-control/fleet_control/__init__.py',
        'scripts/minicpm/vendor/fleet_control/comfy_preload.py': 'fleet-control/fleet_control/comfy_preload.py',
        'scripts/minicpm/vendor/fleet_control/weight_preload_protocol.py': 'fleet-control/fleet_control/weight_preload_protocol.py',
        'scripts/minicpm/vendor/weight_preload_node/__init__.py': 'fleet-control/compat/candidates/weight_preload/__init__.py',
    }
    if set(lock.get('files', {})) != set(expected_vendor):
        raise ValueError('preload_vendor_file_set_changed')
    if any(lock['files'][name].get('upstream_path') != upstream for name, upstream in expected_vendor.items()):
        raise ValueError('preload_vendor_path_changed')
    for name, metadata in lock.get('files', {}).items():
        if name in LOCAL_PRELOAD_FILES - {GRAPH}:
            row = files.get(name)
            if not row or row['sha256'] != metadata.get('sha256'):
                raise ValueError('preload_vendor_hash_mismatch:'+name)
    graph = json.loads(base64.b64decode(files[GRAPH]['content']))
    if not {'1', 'preload'} <= set(graph) or graph['1'] != {
            'class_type': 'UNETLoader', 'inputs': {
                'unet_name': 'qwen_image_2.1_int8_convrot.safetensors', 'weight_dtype': 'default'}}:
        raise ValueError('preload_graph_target_changed')
    if graph['preload'] != {'class_type': 'SiyuanPreloadModel', 'inputs': {
            'model': ['1', 0], 'authorization_json': '$AUTH'}}:
        raise ValueError('preload_graph_contract_changed')
    graph_digest = sha(canonical(graph))
    file_hashes = {name: row['sha256'] for name, row in files.items()}
    # Keep the header consumed by scripts/nx5/service.py and existing clients.
    # The fingerprint is the fixed bundle's complete file-hash map, as in the
    # canonical NX5 publisher; source_commit remains the immutable Git identity.
    manifest = {'release': 'nx5-ssd-llm-' + revision[:12],
                'commit': sha(json.dumps(file_hashes, sort_keys=True).encode()),
                'source_commit': revision, 'files': file_hashes,
                'vendor_commit': VENDOR_COMMIT, 'graph_sha256': graph_digest}
    return {'manifest': manifest, 'files': files, 'manifest_sha256': sha(canonical(manifest))}


def config_for_nx5(graph_sha256, *, home='/home/nx'):
    root = Path(home)
    return {
        'schema_version': 1,
        'group_id': 'nx5-igpu-group',
        'resource_id': 'nx5-igpu',
        'mode': 'comfy',
        'profile_id': 'nx5-qwen21',
        'profile_version': 'qwen21-t2i-nx5-20261005',
        'model_class': 'QwenImage21',
        'native_base': 'http://127.0.0.1:8188',
        'central': {'base_url': 'http://100.91.42.28:18292',
                    'token_file': str(root/'.config/nx5-ssd-llm/weight-preload-central.token')},
        'ticket_key_file': str(root/'.config/nx5-ssd-llm/weight-preload.ticket-key'),
        'graph_file': str(root/'.local/share/nx5-ssd-llm/current'/GRAPH),
        'graph_sha256': graph_sha256,
        'budget': {'max_cuda_bytes': 3*1024**3, 'reserve_cuda_bytes': 4*1024**3,
                   'min_ram_available_bytes': 9*1024**3, 'min_loaded_bytes': 256*1024**2,
                   'unified_memory': True},
    }


def capability_for(config):
    return {'schema_version': 1, 'group_id': config['group_id'],
        'profile_id': config['profile_id'], 'profile_version': config['profile_version'],
        'graph_sha256': config['graph_sha256'], 'model_class': config['model_class'],
        'budget': config['budget'], 'scope': 'main_model_partial_cuda',
        'inference_allowed': False}


def merge_owner_config(existing, preload):
    import copy
    result = copy.deepcopy(existing)
    if result.get('managed_comfy') is not True:
        raise ValueError('managed_comfy_required')
    profiles = result.get('policy_profiles')
    if not isinstance(profiles, dict):
        raise ValueError('policy_profiles_missing')
    profile = profiles.get('comfy')
    if (not isinstance(profile, dict) or profile.get('profile_id') != 'nx5-qwen21'
            or profile.get('version') != 'qwen21-t2i-nx5-20261005'
            or set(profile) - {'profile_id', 'version', 'weight_preload'}):
        raise ValueError('comfy_profile_changed')
    profile['weight_preload'] = copy.deepcopy(preload)
    return result


def token_from_credentials(raw, token_env):
    if not isinstance(token_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', token_env):
        raise ValueError('central_token_environment_invalid')
    found = []
    for line in raw.decode('utf-8').splitlines():
        for word in shlex.split(line, comments=True):
            if '=' in word:
                key, value = word.split('=', 1)
                if key == token_env:
                    found.append(value)
    if len(found) != 1 or not re.fullmatch(r'[A-Za-z0-9._~-]{1,480}', found[0]):
        raise ValueError('central_token_unavailable_or_ambiguous')
    return found[0].encode('ascii')


def config_artifacts(existing, graph_sha256, *, home='/home/nx'):
    import copy
    owner = merge_owner_config(existing, config_for_nx5(graph_sha256, home=home))
    node = copy.deepcopy(owner['policy_profiles']['comfy']['weight_preload'])
    owner_bytes = json.dumps(owner, ensure_ascii=False, sort_keys=True, indent=2).encode()+b'\n'
    node_bytes = json.dumps(node, ensure_ascii=False, sort_keys=True, indent=2).encode()+b'\n'
    if owner['policy_profiles']['comfy']['weight_preload'] != node:
        raise ValueError('owner_node_config_mismatch')
    return {'owner_config': owner_bytes, 'node_config': node_bytes,
            'config_sha256': sha(canonical(node)), 'ticket_key': secrets.token_urlsafe(32).encode('ascii')}


def atomic_private_file(path, content, *, expected_sha256=None, allow_existing=False):
    """Create a 0600 artifact with CAS; never follow a symlink or clobber a peer."""
    import os
    import stat
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError('private_parent_unsafe')
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not allow_existing:
            raise ValueError('private_file_already_exists')
        info = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or (expected_sha256 is not None and sha(path.read_bytes()) != expected_sha256)):
            raise ValueError('private_file_baseline_changed')
        if path.read_bytes() != content:
            raise ValueError('private_file_content_conflict')
        return sha(content)
    temporary = path.with_name(path.name+'.new-'+str(os.getpid()))
    fd = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return sha(content)


def save_plan(path, plan, *, expected_phase=None, initial=False):
    import stat
    path = Path(path)
    raw = (json.dumps(plan, sort_keys=True, indent=2)+'\n').encode()
    if initial:
        return atomic_private_file(path, raw)
    if path.is_symlink() or not path.is_file():
        raise ValueError('plan_file_unsafe')
    info = path.stat(follow_symlinks=False)
    current_raw = path.read_bytes()
    current = json.loads(current_raw)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or current.get('phase') != expected_phase):
        raise ValueError('plan_cas_conflict')
    temporary = path.with_name(path.name+'.new-'+str(os.getpid()))
    fd = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    return sha(raw)


def remote_program(payload):
    helpers = '\n'.join(inspect.getsource(function) for function in
        (merge_owner_config, comfy_dropin))+'\n'
    return helpers+REMOTE_PRELOAD.replace('PAYLOAD', repr(payload), 1)


def call_remote(payload, *, failure_log):
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'nx5', 'python3', '-']
    result = subprocess.run(command, input=remote_program(payload), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        path = Path(failure_log)
        parent = path.parent.stat(follow_symlinks=False)
        if (path.is_symlink() or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
            raise RuntimeError('remote_failure_log_path_unsafe')
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(result.stderr.encode('utf-8', errors='replace'))
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        raise RuntimeError('remote_call_failed:'+str(payload.get('action'))+':'+str(result.returncode))
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('capture', 'prepare', 'apply', 'inspect', 'verify'), required=True)
    parser.add_argument('--commit', required=True, help='full immutable native source SHA')
    parser.add_argument('--operation', required=True, help='unique nx5-preload maintenance operation label')
    parser.add_argument('--maintenance-revision', required=True, type=int,
                        help='exact NX5 resource revision after external capacity pause')
    parser.add_argument('--plan', required=True, help='private local operation journal path')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    bundle = package(root, args.commit)
    plan_path = Path(args.plan)
    failure_log = plan_path.with_name(plan_path.name+'.'+args.operation+'.'+args.action+'.remote.stderr')
    if args.action == 'capture':
        central = central_snapshot()
        validate_central_snapshot(central, operation=args.operation,
                                  revision=args.maintenance_revision, now=time.time())
        remote = json.loads(call_remote({'action':'capture','operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit},
            failure_log=failure_log))
        plan = {'schema_version':1,'operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit,
            'manifest':bundle['manifest'],'manifest_sha256':bundle['manifest_sha256'],
            'graph_sha256':bundle['manifest']['graph_sha256'],'central_baseline':central,
            'remote_baseline':remote,'phase':'captured'}
        save_plan(plan_path,plan,initial=True)
        print(json.dumps({'phase':'captured','operation':args.operation,
            'source_commit':args.commit,'manifest_sha256':bundle['manifest_sha256'],
            'remote_baseline':remote},sort_keys=True,indent=2))
        return
    if plan_path.is_symlink() or not plan_path.is_file():
        parser.error('operation plan is missing or unsafe')
    plan=json.loads(plan_path.read_text())
    if (plan.get('schema_version')!=1 or plan.get('operation')!=args.operation
            or plan.get('maintenance_revision')!=args.maintenance_revision
            or plan.get('source_commit')!=args.commit
            or plan.get('manifest_sha256')!=bundle['manifest_sha256']):
        parser.error('fixed source or maintenance plan changed')
    if args.action == 'inspect':
        result=json.loads(call_remote({'action':'inspect','operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit,
            'baseline':plan['remote_baseline']},failure_log=failure_log))
        print(json.dumps({'local_phase':plan.get('phase'),'remote':result},sort_keys=True,indent=2))
        return
    if args.action == 'verify':
        if plan.get('phase') not in ('applying', 'verified'):
            raise ValueError('inspect_only:'+str(plan.get('phase')))
        config = config_for_nx5(plan['graph_sha256'])
        payload = {'action':'verify','operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit,
            'manifest':bundle['manifest'],'baseline':plan['remote_baseline'],
            'capability':capability_for(config)}
        result=json.loads(call_remote(payload,failure_log=failure_log))
        print(json.dumps({'local_phase':plan.get('phase'),'remote':result},sort_keys=True,indent=2))
        return
    central=central_snapshot()
    validate_central_snapshot(central,operation=args.operation,
        revision=args.maintenance_revision,now=time.time())
    if not central_baseline_matches(plan['central_baseline'], central):
        raise ValueError('central_idle_baseline_changed')
    if args.action == 'prepare':
        if plan.get('phase') != 'captured':
            raise ValueError('inspect_only:'+str(plan.get('phase')))
        save_plan(plan_path,{**plan,'phase':'preparing'},expected_phase='captured')
        payload={'action':'prepare','operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit,
            'manifest':bundle['manifest'],'files':bundle['files'],
            'baseline':plan['remote_baseline'],'preload_spec':config_for_nx5(plan['graph_sha256']),
            'graph':GRAPH}
        result=json.loads(call_remote(payload,failure_log=failure_log))
        current=json.loads(plan_path.read_text())
        save_plan(plan_path,{**current,'phase':'prepared','remote_prepare':result},expected_phase='preparing')
    elif args.action == 'apply':
        if plan.get('phase') != 'prepared':
            raise ValueError('inspect_only:'+str(plan.get('phase')))
        save_plan(plan_path,{**plan,'phase':'applying'},expected_phase='prepared')
        payload={'action':'apply','operation':args.operation,
            'maintenance_revision':args.maintenance_revision,'source_commit':args.commit,
            'baseline':plan['remote_baseline'],
            'manifest':bundle['manifest'],'preload_spec':config_for_nx5(plan['graph_sha256']),
            'graph':GRAPH,'capability':capability_for(config_for_nx5(plan['graph_sha256']))}
        result=json.loads(call_remote(payload,failure_log=failure_log))
        current=json.loads(plan_path.read_text())
        save_plan(plan_path,{**current,'phase':'verified','verification':result},expected_phase='applying')
    else:
        parser.error('unsupported operation action')
    print(json.dumps({'phase':json.loads(plan_path.read_text())['phase'],
        'operation':args.operation,'source_commit':args.commit,'result':result},sort_keys=True,indent=2))


if __name__ == '__main__':
    main()
