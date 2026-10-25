#!/usr/bin/env python3
"""NX5 CPU-manager observer publication; no model or Comfy lifecycle actions."""
import argparse
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import time
import urllib.request

BASELINE='f9b117d9d4c996355aa87d9c821493ae0e294f3a'
OPERATION='nx5-residency-7b979e5e377b4f90ba657f6283e7f00b'
VENDOR={'scripts/minicpm/vendor/mode_residency.py','scripts/minicpm/vendor/comfy_residency/__init__.py','scripts/minicpm/vendor/source-lock.json'}
ALLOWED={'scripts/nx5/resident.py',*VENDOR}
ORIGINAL=('scripts/nx5/resident.py','scripts/nx5/compat_api.py','configs/nx5-chat-runtime.json','scripts/nx5/chat-runner.cpp','scripts/nx5/service.py','scripts/nx5/run-model.py','scripts/minicpm/bench-audit.py','configs/qwen36-iq4xs-128k.json','configs/nx5-minicpm-service.json','configs/minicpm-v47-instruct.jinja','docs/nx5-service/API.md','docs/nx5-service/catalog.json','docs/nx5-service/openapi.json','deploy/nx5/nx5-ssd-llm.service')
ADMIN=Path('/home/ai/.local/state/siyuan-gateway-releases/fleet-lifecycle-enrollment-20261023-001/nano1-registration/maintenance-admin.key')
MEDIA_PLAN=Path('/home/ai/.local/state/siyuan-gateway-releases/candidate/nx5-residency-e29362bf3bea185cda618c56a42a6ce0eb72bb3d-plan.json')
CHECKS=('source_hashes','allowed_observer_delta','config_units_unchanged','jobs_unchanged','protected_processes_unchanged','native_child_absent','central_pause_drained','health_residency_known')


def need(value,code):
    if not value: raise RuntimeError(code)


def sha(raw): return hashlib.sha256(raw).hexdigest()


def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def atomic(path,value):
    path=Path(path);need(not path.is_symlink(),'journal_symlink_refused');path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.new-'+str(os.getpid()));fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(canonical(value)+b'\n');stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def get(url,token=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): raise RuntimeError('redirect_forbidden')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token} if token else {})
    with opener.open(request,timeout=8) as response:
        raw=response.read(2*1024*1024+1);need(len(raw)<=2*1024*1024,'response_too_large');return json.loads(raw)


def validate_activity(activity,now=None):
    now=time.time() if now is None else now
    need(activity.get('group_id')=='nx5-igpu-group' and activity.get('resources')==['nx5-igpu']
         and activity.get('mode')=='comfy' and activity.get('state')=='ready' and activity.get('fresh') is True
         and type(activity.get('observed_at')) in (int,float) and 0<=now-activity['observed_at']<=60
         and type(activity.get('active')) is int and activity['active']==0 and activity.get('leases')==[] and activity.get('pending')==[]
         and activity.get('transition') is None and 'policy_demand' in activity and activity['policy_demand'] is None,'central_not_fresh_drained')


def validate_node(node,*,candidate=False,source=None,now=None):
    now=time.time() if now is None else now
    health=node['health'];m=health.get('managed_comfy') or {}
    need(health.get('source_commit')==source and health.get('service_id')=='nx5-ssd-llm' and health.get('state')=='ready'
         and type(health.get('active')) is int and health['active']==0 and 'resident_pid' in health and health['resident_pid'] is None
         and health.get('model_loaded') is False and health.get('lease_status')=='released' and health.get('resident_error') is None
         and type(health.get('observed_at')) in (int,float) and 0<=now-health['observed_at']<=60,'manager_not_fresh_idle_without_child')
    need(m.get('mode')=='comfy' and m.get('state')=='ready' and m.get('active')==0 and m.get('profile_id')=='nx5-qwen21'
         and m.get('profile_version')=='qwen21-t2i-nx5-20261005' and m.get('phase') in ('completed','idle')
         and m.get('policy_state')=='applied','managed_comfy_not_settled')
    if candidate:need(type(m.get('model_loaded')) is bool,'comfy_residency_unknown')
    native=node['protected']['units']['nx5-flint.service']
    need(native['ActiveState']=='inactive' and native['MainPID']=='0','flint_native_not_off')
    need(node['jobs']['count']>=0,'jobs_invalid')
    validate_activity(node['central_activity'],now)


def package(repo,source):
    need(re.fullmatch('[0-9a-f]{40}',source or '') is not None,'full_source_sha_required')
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
    need(git('rev-parse',source+'^{commit}').decode().strip()==source,'source_not_commit')
    old={n:sha(git('show',BASELINE+':'+n)) for n in ORIGINAL};files={}
    for name in sorted(set(ORIGINAL)|VENDOR):
        info=git('ls-tree',source,'--',name).decode().split()
        need(len(info)>=4 and info[:2]==['100644','blob'],'source_type_invalid')
        raw=git('show',source+':'+name)
        if name not in ALLOWED:need(sha(raw)==old[name],'non_observer_source_difference:'+name)
        if name.endswith('.py'):compile(raw,name,'exec')
        files[name]={'sha256':sha(raw),'content':base64.b64encode(raw).decode()}
    lock=json.loads(base64.b64decode(files['scripts/minicpm/vendor/source-lock.json']['content']))
    pinned={'scripts/minicpm/vendor/mode_residency.py':('fleet-control/fleet_control/mode_residency.py','9d8468043ff45b69c61d6cdac89580abb908163cd4dce0114b050c9e48e68e5f'),
            'scripts/minicpm/vendor/comfy_residency/__init__.py':('fleet-control/compat/candidates/nx4/comfy_residency/__init__.py','8882886555c3d4b0dc980137f2bfabfb2f2be2aa4d56c319716d890a1bef2811')}
    need(lock=={'repository':'redyuan43/siyuan-media-gateway','commit':'8f687edf013d57ad3ebf5d955d2e0fed5cbafc44','files':{n:{'upstream_path':v[0],'sha256':v[1]} for n,v in pinned.items()}},'vendor_lock_changed')
    need(all(files[n]['sha256']==v[1] for n,v in pinned.items()),'vendor_body_changed')
    hashes={n:r['sha256'] for n,r in files.items()};manifest={'release':'nx5-ssd-llm-'+source[:12],'source_commit':source,'commit':sha(json.dumps(hashes,sort_keys=True).encode()),'files':hashes}
    return {'source_commit':source,'files':files,'manifest':manifest,'manifest_sha256':sha(canonical(manifest)),'original_files':old}


REMOTE=r'''
import base64,fcntl,hashlib,json,os,pathlib,re,shlex,subprocess,sys,time,urllib.request
from pathlib import Path
p=json.loads(sys.stdin.readline());action=sys.argv[1];home=pathlib.Path.home();base=home/'.local/share/nx5-ssd-llm';current=base/'current'
config=home/'.config/nx5-ssd-llm/service.json';state=home/'.local/state/nx5-ssd-llm';unit_name='nx5-ssd-llm.service'
def run(argv):
    return subprocess.check_output(argv,text=True,stderr=subprocess.DEVNULL,timeout=120).strip()
def props(name):
    return dict(v.split('=',1) for v in run(['systemctl','--user','show',name,'-p','ActiveState','-p','SubState','-p','MainPID','-p','InvocationID','-p','FragmentPath','-p','DropInPaths']).splitlines() if '=' in v)
def proc(pid):
    q=pathlib.Path('/proc')/str(pid)
    return {'pid':pid,'start':q.joinpath('stat').read_text().rsplit(')',1)[1].split()[19],'argv_sha256':sha(q.joinpath('cmdline').read_bytes())}
def jobs():
    cfg=json.loads(config.read_text());rows={}
    for f in sorted(pathlib.Path(cfg['state']).glob('op_*.json')):
        need(not f.is_symlink() and json.loads(f.read_text()).get('status') in ('succeeded','failed'),'nonterminal_job')
        rows[f.name]=sha(f.read_bytes())
    return {'count':len(rows),'sha256':sha(canonical(rows))}
def activity():
    private=home/'.config/siyuan-text-ingress';cfg=json.loads((private/'wrapper.json').read_text())['central'];env={}
    for line in (private/'credentials.env').read_text().splitlines():
        for word in shlex.split(line,comments=True):
            if '=' in word:k,v=word.split('=',1);env[k]=v
    need(cfg['base_url'].rstrip('/')=='http://100.91.42.28:18292','central_origin_changed')
    return get(cfg['base_url'].rstrip('/')+'/internal/v1/resource-groups/nx5-igpu-group/activity',env[cfg['auth']['token_env']])
def audit(root,manifest,strict=False):
    need(not root.is_symlink() and root.is_dir(),'release_root_unsafe')
    for name,want in manifest['files'].items():
        f=root/name;need(not f.is_symlink() and sha(f.read_bytes())==want,'source_hash_changed:'+name)
    if strict:
        need(not any(f.is_symlink() for f in root.rglob('*')),'source_symlink')
        for f in [root,*root.rglob('*')]:need(f.stat().st_uid==os.getuid() and f.stat().st_mode&0o777==(0o555 if f.is_dir() else 0o444),'candidate_mode_or_owner_changed')
        need({str(f.relative_to(root)) for f in root.rglob('*') if f.is_file()}==set(manifest['files'])|{'release.json'},'source_file_set_changed')
def capture():
    need(run(['hostname','-s'])=='nx5' and '100.118.44.27' in run(['tailscale','ip','-4']).split(),'wrong_host')
    need(current.is_symlink(),'current_not_symlink');target=os.readlink(current);root=pathlib.Path(target);need(root.is_absolute(),'current_target_not_absolute')
    manifest=json.loads((root/'release.json').read_text());audit(root,manifest)
    u=props(unit_name);pid=int(u['MainPID']);need(u['ActiveState']=='active' and pid>1,'manager_inactive')
    health=get('http://100.118.44.27:18347/healthz')
    names=['siyuan-image-comfy.service','nx5-flint-ingress.service','nx5-flint.service','role-switch.service']
    units={n:props(n) for n in names};processes={n:proc(int(v['MainPID'])) for n,v in units.items() if int(v['MainPID'])>1};hashes={}
    for v in [u,*units.values()]:
        for name in [v['FragmentPath'],*shlex.split(v['DropInPaths'])]:
            if name:hashes[name]=sha(pathlib.Path(name).read_bytes())
    return {'manager':{'pid':pid,'invocation_id':u['InvocationID'],'proc':proc(pid),'source_commit':manifest['source_commit'],'current_target':target},'health':health,'jobs':jobs(),'protected':{'units':units,'processes':processes,'unit_hashes':hashes,'config_sha256':sha(config.read_bytes())},'central_activity':activity(),'source_manifest':manifest}
def verify():
    node=capture();b=p['baseline'];need(node['manager']['current_target']==p['candidate_path'],'candidate_link_changed')
    need(node['manager']['source_commit']==p['source_commit'],'candidate_manifest_wrong_source');audit(pathlib.Path(p['candidate_path']),p['manifest'],True)
    need(node['source_manifest']==p['manifest'],'candidate_manifest_changed');validate_node(node,candidate=True,source=p['source_commit'])
    need(node['manager']['pid']!=b['manager']['pid'] and node['manager']['invocation_id']!=b['manager']['invocation_id'],'manager_not_restarted')
    need(node['protected']==b['protected'] and node['jobs']==b['jobs'],'protected_state_or_jobs_changed')
    return node
if action=='capture':print(json.dumps(capture()));sys.exit(0)
need(p['operation_id']=='nx5-residency-7b979e5e377b4f90ba657f6283e7f00b','wrong_original_operation')
need(re.fullmatch('[a-f0-9]{40}',p['source_commit']) is not None,'invalid_source_commit')
journal_path=state/('observer-publication-'+p['operation_id']+'.json')
if action=='inspect':print(json.dumps(json.loads(journal_path.read_text()) if journal_path.exists() else {'phase':'not_applied'}));sys.exit(0)
if action=='verify':print(json.dumps(verify()));sys.exit(0)
need(action=='apply','unsupported_action');need(not journal_path.exists(),'original_journal_exists_query_only')
fd=os.open(state/'.observer-publication.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
node=capture();validate_node(node,source='f9b117d9d4c996355aa87d9c821493ae0e294f3a')
need(node['manager']==p['baseline']['manager'] and node['protected']==p['baseline']['protected'] and node['jobs']==p['baseline']['jobs'],'baseline_changed')
need(node['source_manifest']['files']==p['original_files'],'original_source_not_fixed_git')
root=pathlib.Path(p['candidate_path']);need(root==base/'releases'/p['source_commit'] and not root.exists(),'candidate_path_collision')
record={'operation_id':p['operation_id'],'source_commit':p['source_commit'],'manifest_sha256':p['manifest_sha256'],'plan_sha256':p['plan_sha256'],'before':node}
def mark(phase):
    record.update(phase=phase,updated_at=time.time());atomic(journal_path,record)
mark('prepared');backup=state/('observer-backup-'+p['operation_id']);backup.mkdir(mode=0o700,exist_ok=False)
atomic(backup/'before.json',node);atomic(backup/'original-link.json',{'target':os.readlink(current)})
atomic(backup/'controls.json',{'config_base64':base64.b64encode(config.read_bytes()).decode(),'unit_files_base64':{name:base64.b64encode(pathlib.Path(name).read_bytes()).decode() for name in node['protected']['unit_hashes']}})
root.mkdir(mode=0o700)
for name,row in p['files'].items():
    rel=pathlib.PurePosixPath(name);need(not rel.is_absolute() and '..' not in rel.parts,'invalid_source_path')
    raw=base64.b64decode(row['content'],validate=True);need(sha(raw)==row['sha256']==p['manifest']['files'][name],'source_payload_changed')
    f=root/name;f.parent.mkdir(parents=True,exist_ok=True,mode=0o700);f.write_bytes(raw);f.chmod(0o444)
(root/'release.json').write_bytes((json.dumps(p['manifest'],sort_keys=True,indent=2)+'\n').encode());(root/'release.json').chmod(0o444)
for f in sorted([root,*[q for q in root.rglob('*') if q.is_dir()]],key=lambda q:len(q.parts),reverse=True):f.chmod(0o555)
audit(root,p['manifest'],True);latest=capture();validate_node(latest,source='f9b117d9d4c996355aa87d9c821493ae0e294f3a')
need(latest['manager']==node['manager'] and latest['protected']==node['protected'] and latest['jobs']==node['jobs'],'baseline_changed_before_link')
need(os.readlink(current)==node['manager']['current_target'],'original_link_cas_changed');mark('link_switch_intent')
link=base/('current.observer-'+p['operation_id']);need(not link.exists() and not link.is_symlink(),'pending_link_exists');link.symlink_to(root);os.replace(link,current)
mark('restart_intent');run(['systemctl','--user','restart',unit_name]);mark('restart_returned')
deadline=time.monotonic()+90
while True:
    try:result=verify();break
    except Exception:
        need(time.monotonic()<deadline,'verification_timeout_query_original');time.sleep(1)
record['after']=result;mark('verified');print(json.dumps(result))
'''
REMOTE='\n'.join(inspect.getsource(f) for f in (need,sha,canonical,atomic,get,validate_activity,validate_node))+'\n'+REMOTE


def remote(action,payload=None):
    need(action in ('capture','apply','inspect','verify'),'invalid_action')
    cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','nx5','python3 -c '+shlex.quote(REMOTE)+' '+action]
    result=subprocess.run(cmd,input=json.dumps(payload or {})+'\n',text=True,capture_output=True,timeout=240)
    need(result.returncode==0,'remote_result_unknown_query_original:'+action)
    return json.loads(result.stdout)


def central():
    token=ADMIN.read_text().strip();base='http://100.91.42.28:18292'
    inventory=get(base+'/console/admin/v1/resources',token);leases=get(base+'/console/admin/v1/leases',token)
    rows=[r for r in inventory['resources'] if r.get('resource_id')=='nx5-igpu'];need(len(rows)==1,'central_resource_ambiguous');resource={k:rows[0][k] for k in ('resource_id','capacity','revision','occupied')}
    need(resource=={'resource_id':'nx5-igpu','capacity':0,'revision':10,'occupied':0},'original_pause_cas_changed')
    held=[l for l in leases['leases'] if l.get('status')=='held' and any(x.get('resource_id')=='nx5-igpu' for x in l.get('resources',[]))];need(not held,'target_lease_held')
    return resource


def media_identity(path):
    media=json.loads(Path(path).read_text())
    need(media.get('plan_sha256')==sha(canonical({k:v for k,v in media.items() if k!='plan_sha256'})) and media.get('operation_id')==OPERATION,'original_media_plan_hash_changed')
    return {k:media[k] for k in ('operation_id','source_sha','manifest_sha256','plan_sha256')}


def read_plan(path,repo=None):
    p=json.loads(Path(path).read_text());need(p.get('plan_sha256')==sha(canonical({k:v for k,v in p.items() if k!='plan_sha256'})),'plan_hash_changed')
    need(p.get('publisher_sha256')==sha(Path(__file__).read_bytes()) and p.get('operation_id')==OPERATION,'fixed_publisher_or_operation_changed')
    need(sha(canonical(p['manifest']))==p['manifest_sha256'],'manifest_hash_changed')
    fixed=package(repo or Path(__file__).resolve().parents[2],p['source_commit'])
    need(all(p[k]==fixed[k] for k in ('source_commit','files','manifest','manifest_sha256','original_files')),'fixed_git_package_changed')
    need(p['original_media']==media_identity(p['media_plan_path']),'original_media_identity_changed')
    return p


def verify_receipt(plan):
    central();node=remote('verify',plan);validate_node(node,candidate=True,source=plan['source_commit']);resource=central()
    after=dict(node);after['central']={'resource':resource,'activity':node['central_activity'],'observed_at':time.time()}
    before=dict(plan['baseline']);before['central']={'resource':plan['central_resource'],'activity':before['central_activity'],'observed_at':plan['prepared_at']}
    return {'schema':'siyuan.nx5-observer-publication.v1','operation_id':OPERATION,'original_media':plan['original_media'],
      'source_commit':plan['source_commit'],'manifest_sha256':plan['manifest_sha256'],'files':plan['manifest']['files'],'candidate_path':plan['candidate_path'],
      'before':before,'after':after,'checks':{k:True for k in CHECKS},'verified_at':time.time(),'operator_model_requests':0,'phase':'verified'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','apply','inspect','verify'));parser.add_argument('--plan',required=True);parser.add_argument('--source-sha');parser.add_argument('--repo',default=str(Path(__file__).resolve().parents[2]));parser.add_argument('--media-plan',default=str(MEDIA_PLAN));parser.add_argument('--receipt')
    args=parser.parse_args(argv)
    try:
        if args.action=='prepare':
            need(not Path(args.plan).exists(),'plan_exists_query_original');bundle=package(args.repo,args.source_sha);media=media_identity(args.media_plan)
            node=remote('capture');validate_node(node,source=BASELINE);need(node['manager']['pid']==2561067 and node['source_manifest']['files']==bundle['original_files'],'original_manager_source_changed')
            need(node['protected']['units']['siyuan-image-comfy.service']['MainPID']=='3679409' and node['protected']['units']['siyuan-image-comfy.service']['InvocationID']=='555b78c99f9c40e295762d9fbf22cc48','original_comfy_changed')
            need(node['protected']['units']['nx5-flint-ingress.service']['MainPID']=='2571583' and node['protected']['units']['nx5-flint-ingress.service']['InvocationID']=='bd9d392167d041b6adeed70d6d553a31','original_gate_changed')
            p={**bundle,'operation_id':OPERATION,'original_media':media,'media_plan_path':str(Path(args.media_plan).resolve()),'baseline':node,'central_resource':central(),'prepared_at':time.time(),'candidate_path':'/home/nx/.local/share/nx5-ssd-llm/releases/'+args.source_sha,'publisher_sha256':sha(Path(__file__).read_bytes())}
            p['plan_sha256']=sha(canonical(p));atomic(args.plan,p);result={'phase':'prepared','operation_id':OPERATION,'source_commit':args.source_sha,'manifest_sha256':p['manifest_sha256'],'plan_sha256':p['plan_sha256']}
        else:
            p=read_plan(args.plan,args.repo)
            if args.action=='inspect':result=remote('inspect',p)
            else:
                central()
                if args.action=='apply':remote('apply',p)
                result=verify_receipt(p)
                if args.receipt:atomic(args.receipt,result)
        print(json.dumps(result,sort_keys=True));return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        print(json.dumps({'phase':'unknown_query_original','error':str(error)}),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
