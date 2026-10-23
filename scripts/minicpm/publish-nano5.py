#!/usr/bin/env python3
"""Publish only the Nano5 HTTP owner through the existing nx administrator.

Plan is read-only. Apply consumes a fixed plan and committed candidate. Unknown
operations are query-only, retain their admission fence, and never auto-restart.
No model, Comfy, database, credentials, or sudo policy is modified.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

HOME = Path('/home/nano')
CONFIG = HOME/'.config/nano5-minicpm/config.json'
UNIT = HOME/'.config/systemd/user/nano5-minicpm.service'
SERVICE = 'nano5-minicpm.service'
GROUP = 'nano5-igpu-group'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def atomic(path,value):
    path=Path(path);temp=path.with_name(path.name+'.pending')
    with temp.open('x') as f:
        json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    temp.chmod(0o600);temp.replace(path)


def previous(path,operation,commit):
    if not Path(path).exists():return None
    value=json.loads(Path(path).read_text())
    if value.get('operation_id')!=operation or value.get('source_commit')!=commit:
        raise RuntimeError('publication_identity_conflict')
    return value


def require_baseline(expected,actual):
    keys=('pid','unit_sha256','config_sha256','source_sha256','source_path','cwd')
    if any(expected.get(k)!=actual.get(k) for k in keys):raise RuntimeError('owner_baseline_changed')


def require_idle(health,activity,jobs,intent):
    if (health.get('state')!='ready' or health.get('active')!=0
            or health.get('inference_or_switch_request_active') is not False
            or health.get('coordinator_reachable') is not True):raise RuntimeError('owner_not_idle')
    if (any(type(activity.get(k)) is not list for k in ('leases','pending'))
            or 'transition' not in activity or activity['leases'] or activity['pending'] or activity['transition']):
        raise RuntimeError('central_group_not_idle')
    if any(j.get('status') not in {'succeeded','failed','cancelled'} for j in jobs):
        raise RuntimeError('local_job_not_terminal')
    if intent and intent.get('phase')!='actions_complete':raise RuntimeError('lifecycle_requires_reconciliation')


def next_unit(raw,old,new):
    if (raw.count('WorkingDirectory='+old+'\n')!=1
            or raw.count(old+'/scripts/minicpm/nano5-service.py')!=1 or raw.count(old)!=2):
        raise RuntimeError('unit_layout_not_qualified')
    return raw.replace(old,new)


def command(args,timeout=20):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('owner_control_failed')
    return p.stdout.strip()


def user_systemctl(*args):
    return command(['runuser','-u','nano','--','env','XDG_RUNTIME_DIR=/run/user/2002',
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/2002/bus','systemctl','--user',*args])


def snapshot():
    pid=int(user_systemctl('show',SERVICE,'--property=MainPID','--value'))
    if pid<=1:raise RuntimeError('owner_not_running')
    proc=Path('/proc')/str(pid)
    argv=(proc/'cmdline').read_bytes().decode().split('\0')
    if len(argv)<4 or argv[2:]!=['--config',str(CONFIG),'']:
        raise RuntimeError('owner_cmdline_not_qualified')
    source=Path(argv[1]);cwd=str((proc/'cwd').resolve())
    if str(source)!=cwd+'/scripts/minicpm/nano5-service.py':raise RuntimeError('owner_source_path_conflict')
    children=subprocess.run(['ps','--no-headers','-o','pid=','--ppid',str(pid)],capture_output=True,text=True)
    if children.returncode not in (0,1):raise RuntimeError('owner_children_unknown')
    if children.stdout.strip():raise RuntimeError('owner_child_active')
    return {'pid':pid,'unit_sha256':sha(UNIT.read_bytes()),'config_sha256':sha(CONFIG.read_bytes()),
        'source_sha256':sha(source.read_bytes()),'source_path':str(source),'cwd':cwd}


def api(config,path):
    token=Path(config['controller_token_file']).read_text().strip()
    req=urllib.request.Request(config['central']+path,headers={'Authorization':'Bearer '+token})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=8) as response:return json.load(response)


def idle(config,root,mark,fence_module):
    health=root.marked_request(config,mark)
    state=Path(config['state'])
    jobs=[json.loads(p.read_text()) for p in state.glob('op_*.json')]
    intent=json.loads((state/'last-transition.json').read_text()) if (state/'last-transition.json').exists() else {}
    activity=api(config,'/internal/v1/resource-groups/'+GROUP+'/activity')
    require_idle(health,activity,jobs,intent)
    fence_module.connections_drained(command(['ss','-Htan','sport = :'+str(config['port'])]))
    snapshot()
    return health


def receipt(record):
    return {k:record[k] for k in ('operation_id','source_commit','phase','old_pid','new_pid','error') if k in record}


def remote(payload,stage_module,fence_module):
    if os.geteuid()!=0:raise RuntimeError('existing_administrator_channel_required')
    config=json.loads(CONFIG.read_text())
    if (config['bind']!='100.73.65.44' or config['port']!=18347
            or config['root']!=str(HOME/'minicpm-v47') or config['state']!=str(HOME/'.local/state/nano5-minicpm')):
        raise RuntimeError('nano5_target_not_qualified')
    operation=payload['operation_id']
    if not re.fullmatch('op_[a-f0-9]{32}',operation):raise RuntimeError('stable_release_operation_required')
    folder=HOME/'minicpm-v47/owner-publications'/operation
    journal=folder/'publication.json'
    old=previous(journal,operation,payload['source_commit'])
    if old:return receipt(old)
    current=snapshot()
    root=fence_module.Root(prefix=())
    firewall=root(['-S','INPUT'])
    baseline_sha=sha(firewall.encode())
    mark=int(operation[3:11],16) or 1
    idle(config,root,mark,fence_module)
    if not payload.get('apply'):
        stage_module.stage(HOME/'minicpm-v47/releases',CONFIG.read_bytes(),payload,apply=False)
        return {'phase':'preflight','operation_id':operation,'source_commit':payload['source_commit'],
            'expected':current,'firewall_sha256':baseline_sha}
    plan=payload['plan']
    if plan.get('operation_id')!=operation or plan.get('source_commit')!=payload['source_commit']:
        raise RuntimeError('plan_identity_conflict')
    require_baseline(plan['expected'],current)
    if plan['firewall_sha256']!=baseline_sha:raise RuntimeError('firewall_baseline_changed')
    target=HOME/'minicpm-v47/releases'/payload['source_commit']
    stage_module.stage(target.parent,CONFIG.read_bytes(),payload,apply=True)
    for p in [target,*target.rglob('*')]:os.chown(p,2002,2002)
    folder.mkdir(parents=True,mode=0o700,exist_ok=False)
    record={'operation_id':operation,'source_commit':payload['source_commit'],'phase':'prepared','old_pid':current['pid']}
    atomic(journal,record)
    fence=fence_module.Fence(folder/'fence.json',config,operation,root)
    try:
        fence.shield(plan['firewall_sha256'])
        record['phase']='shielded';atomic(journal,record)
        require_baseline(plan['expected'],snapshot())
        idle(config,root,mark,fence_module)
        unit_raw=UNIT.read_bytes();config_raw=CONFIG.read_bytes()
        for name,raw in [('unit.before',unit_raw),('config.before.json',config_raw),
                ('owner.log',command(['journalctl','_UID=2002','_SYSTEMD_USER_UNIT='+SERVICE,'-n','100','--no-pager']).encode())]:
            p=folder/name;p.write_bytes(raw);p.chmod(0o600)
        unit_after=next_unit(unit_raw.decode(),current['cwd'],str(target)).encode()
        after_config=dict(config,deployment=str(target))
        config_after=(json.dumps(after_config,indent=2)+'\n').encode()
        require_baseline(plan['expected'],snapshot());idle(config,root,mark,fence_module)
        record['phase']='restarting';atomic(journal,record)
        for path,raw in [(UNIT,unit_after),(CONFIG,config_after)]:
            temp=path.with_name(path.name+'.owner-release-pending')
            with temp.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chown(temp,2002,2002);temp.chmod(0o600);temp.replace(path)
        user_systemctl('daemon-reload')
        user_systemctl('restart',SERVICE)
        expected_source=payload['manifest']['files']['scripts/minicpm/nano5-service.py']
        for _ in range(20):
            try:
                now=snapshot()
                if (now['pid']==current['pid'] or now['source_path']!=str(target/'scripts/minicpm/nano5-service.py')
                        or now['source_sha256']!=expected_source or now['unit_sha256']!=sha(unit_after)
                        or now['config_sha256']!=sha(config_after)):
                    raise RuntimeError('new_owner_identity_not_ready')
                health=idle(after_config,root,mark,fence_module)
                if health.get('commit')!=expected_source:raise RuntimeError('new_owner_health_identity_changed')
                break
            except Exception:time.sleep(1)
        else:raise RuntimeError('new_owner_verification_failed')
        record.update(phase='verified',new_pid=now['pid']);atomic(journal,record)
        fence.unshield()
        record['phase']='active';atomic(journal,record)
        return receipt(record)
    except BaseException as error:
        record.update(phase='unknown',error=str(error) if isinstance(error,RuntimeError) else type(error).__name__)
        atomic(journal,record)
        return receipt(record)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--commit',required=True);p.add_argument('--operation',required=True)
    p.add_argument('--expected-config-sha256',required=True);p.add_argument('--plan',type=Path)
    p.add_argument('--apply',action='store_true');args=p.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',args.commit):raise RuntimeError('full_git_commit_required')
    if args.apply and not args.plan:raise RuntimeError('reviewed_plan_required')
    repo=Path(__file__).resolve().parents[2]
    read=lambda name:subprocess.check_output(['git','show',args.commit+':'+name],cwd=repo)
    scope={'__name__':'committed_stage'}
    stage_source=read('scripts/minicpm/stage-nano5.py').decode();exec(compile(stage_source,'committed_stage','exec'),scope)
    raw={name:read(name) for name in scope['FILES']}
    payload={'source_commit':args.commit,'operation_id':args.operation,'expected_config_sha256':args.expected_config_sha256,
        'manifest':{'source_commit':args.commit,'files':{n:sha(v) for n,v in raw.items()}},
        'files':{n:base64.b64encode(v).decode() for n,v in raw.items()},'apply':args.apply,
        'plan':json.loads(args.plan.read_text()) if args.plan else None}
    source='import types,json\n'
    for var,path in [('stage_module','scripts/minicpm/stage-nano5.py'),
            ('fence_module','scripts/nx5/temporary_admission_fence.py'),('publisher','scripts/minicpm/publish-nano5.py')]:
        source+=var+'=types.ModuleType('+repr(var)+')\nexec(compile('+repr(read(path).decode())+','+repr(path)+',"exec"),'+var+'.__dict__)\n'
    source+='try:\n result=publisher.remote('+repr(payload)+',stage_module,fence_module)\n print(json.dumps(result))\nexcept Exception as error:\n print(json.dumps({"error":str(error) if isinstance(error,RuntimeError) else type(error).__name__}));raise SystemExit(1)\n'
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','nx@nano5','sudo','-n','python3','-'],
        input=source,text=True,capture_output=True,timeout=90)
    if result.stdout.strip():print(result.stdout.strip())
    if result.returncode:raise RuntimeError('nano5_publication_failed_query_original_operation')


if __name__=='__main__':main()
