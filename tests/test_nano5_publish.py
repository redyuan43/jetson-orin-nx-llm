"""CPU acceptance for a restart that cannot replay uncertain publication."""
import importlib.util
from pathlib import Path
import pytest


def load():
    p=Path(__file__).parents[1]/"scripts/minicpm/publish-nano5.py"
    spec=importlib.util.spec_from_file_location("nano5_publish",p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_busy_or_incomplete_evidence_never_reaches_owner_restart():
    m=load()
    healthy={"state":"ready","active":0,"inference_or_switch_request_active":False,"coordinator_reachable":True}
    idle={"leases":[],"pending":[],"transition":None}
    m.require_idle(healthy,idle,[],{})
    for h,a,j,t in [(healthy|{"active":1},idle,[],{}),(healthy,{"leases":[]},[],{}),
                    (healthy,idle,[{"status":"unknown"}],{}),(healthy,idle,[],{"phase":"dispatching"}),
                    (healthy,idle|{"transition":{"id":"foreign"}},[],{})]:
        with pytest.raises(RuntimeError):m.require_idle(h,a,j,t)


def test_publication_cas_includes_pid_unit_config_and_original_program():
    m=load();before={"pid":12,"unit_sha256":"unit","config_sha256":"config","source_sha256":"source","source_path":"/release/owner.py","cwd":"/release"}
    m.require_baseline(before,before)
    for key in before:
        with pytest.raises(RuntimeError,match="owner_baseline_changed"):
            m.require_baseline(before,before|{key:"parallel"})


def test_unit_rewrite_changes_only_exact_owner_paths():
    m=load();old="/home/nano/minicpm-v47/releases/"+("a"*40);new="/home/nano/minicpm-v47/releases/"+("b"*40)
    raw=f"[Service]\nWorkingDirectory={old}\nExecStart=/python {old}/scripts/minicpm/nano5-service.py --config /cfg\nTimeoutStopSec=2400\n"
    changed=m.next_unit(raw,old,new)
    assert changed==raw.replace(old,new)
    with pytest.raises(RuntimeError):m.next_unit(raw.replace("WorkingDirectory=","Other="),old,new)


def test_nonterminal_journal_is_query_only_and_never_replays_actions(tmp_path):
    m=load();p=tmp_path/"op.json";m.atomic(p,{"phase":"restarting","operation_id":"op_"+"a"*32,"source_commit":"b"*40})
    assert m.previous(p,"op_"+"a"*32,"b"*40)["phase"]=="restarting"
    with pytest.raises(RuntimeError,match="publication_identity_conflict"):m.previous(p,"op_"+"a"*32,"c"*40)


def test_exact_held_maintenance_blocks_central_switch_and_foreign_work():
    m=load()
    hold={'executor_id':'nx6-image-executor','operation_id':'op_'+'d'*32,
          'mode':'comfy','resources':[{'resource_id':'nano5-igpu','units':1}],
          'lease_id':'lease_known','lease_request_digest':'a'*64,
          'reservation_request_digest':'b'*64,'backend_id':'nano5-comfy'}
    lease={k:hold[k] for k in ('executor_id','operation_id','resources')}|{'status':'held'}
    health={'state':'ready','active':0,'inference_or_switch_request_active':False,'coordinator_reachable':True,'mode':'comfy'}
    activity={'group_id':'nano5-igpu-group','resources':['nano5-igpu'],'state':'ready','fresh':True,
              'active':0,'mode':'comfy','leases':[lease],'pending':[],'transition':None}
    m.require_idle(health,activity,[],{},hold)
    m.require_idle(health,activity|{'pending':[{'status':'waiting'}]},[],{},hold)
    changes=[{'leases':[]},{'leases':[lease,lease|{'operation_id':'foreign'}]},
             {'leases':[lease|{'executor_id':'foreign'}]},{'leases':[lease|{'operation_id':'op_'+'e'*32}]},
             {'leases':[lease|{'status':'released'}]},
             {'leases':[lease|{'resources':[{'resource_id':'nano5-igpu','units':2}]}]},
             {'pending':[{'status':'preparing'}]},{'pending':[{'status':'unknown'}]},
             {'transition':{'target_mode':'off'}},
             {'fresh':False},{'mode':'off'},{'active':1}]
    for change in changes:
        with pytest.raises(RuntimeError):m.require_idle(health,activity|change,[],{},hold)
    with pytest.raises(RuntimeError):m.require_idle(health,activity,[],{},hold|{'lease_request_digest':'unknown'})


def test_apply_without_held_barrier_stops_before_any_publication_action(tmp_path,monkeypatch):
    import json
    import types
    m=load();home=tmp_path/'nano';home.mkdir();config_path=home/'config'
    config_path.write_text(json.dumps({'bind':'100.73.65.44','port':18347,
        'root':str(home/'minicpm-v47'),'state':str(home/'.local/state/nano5-minicpm')}))
    monkeypatch.setattr(m,'HOME',home);monkeypatch.setattr(m,'CONFIG',config_path)
    monkeypatch.setattr(m.os,'geteuid',lambda:0)
    monkeypatch.setattr(m,'snapshot',lambda:pytest.fail('missing hold reached owner inspection'))
    with pytest.raises(RuntimeError,match='held_maintenance_barrier_required'):
        m.remote({'operation_id':'op_'+'e'*32,'source_commit':'a'*40,'apply':True},types.SimpleNamespace(),types.SimpleNamespace())


@pytest.mark.parametrize("restart_error",[False,True])
def test_full_publication_restores_admission_only_after_verified_owner_and_never_restarts_twice(tmp_path,monkeypatch,restart_error):
    import json
    import types
    m=load();home=tmp_path/"nano";home.mkdir()
    old=home/"minicpm-v47/releases"/("a"*40);old.mkdir(parents=True)
    config_path=home/"config.json";unit_path=home/"owner.service"
    config={"root":str(home/"minicpm-v47"),"state":str(home/".local/state/nano5-minicpm"),"bind":"100.73.65.44","port":18347,"deployment":str(old)}
    config_path.write_text(json.dumps(config));unit_path.write_text(f"WorkingDirectory={old}\nExecStart=/python {old}/scripts/minicpm/nano5-service.py --config {config_path}\n")
    monkeypatch.setattr(m,"HOME",home);monkeypatch.setattr(m,"CONFIG",config_path);monkeypatch.setattr(m,"UNIT",unit_path)
    monkeypatch.setattr(m.os,"geteuid",lambda:0);monkeypatch.setattr(m.os,"chown",lambda *a:None)
    calls=[];switched=[False]
    expected={"pid":12,"unit_sha256":m.sha(unit_path.read_bytes()),"config_sha256":m.sha(config_path.read_bytes()),"source_sha256":"old","source_path":str(old/"scripts/minicpm/nano5-service.py"),"cwd":str(old)}
    commit="b"*40;target=old.parent/commit
    def snapshot():
        if not switched[0]:return expected.copy()
        return expected|{"pid":13,"unit_sha256":m.sha(unit_path.read_bytes()),"config_sha256":m.sha(config_path.read_bytes()),"source_sha256":"new","source_path":str(target/"scripts/minicpm/nano5-service.py"),"cwd":str(target)}
    monkeypatch.setattr(m,"snapshot",snapshot)
    def control(*args):
        calls.append(args)
        if args[0]=="restart":
            switched[0]=True
            if restart_error:raise RuntimeError("restart_response_unknown")
    monkeypatch.setattr(m,"user_systemctl",control);monkeypatch.setattr(m,"command",lambda *a,**k:"owner log")
    monkeypatch.setattr(m,"idle",lambda *a:{"commit":"new"})
    def stage(base,raw,payload,apply=False):
        if apply:target.mkdir()
    class Root:
        def __init__(self,**kw):pass
        def __call__(self,*args,**kw):return "-P INPUT ACCEPT\n"
    class Fence:
        def __init__(self,*args):pass
        def shield(self,*args):calls.append(("shield",))
        def unshield(self):calls.append(("unshield",))
    fence=types.SimpleNamespace(Root=Root,Fence=Fence)
    hold={'proof':'stubbed only inside the complete orchestration test'}
    payload={"operation_id":"op_"+"c"*32,"source_commit":commit,"apply":True,"maintenance_hold":hold,"manifest":{"files":{"scripts/minicpm/nano5-service.py":"new"}}}
    payload["plan"]={"operation_id":payload["operation_id"],"source_commit":commit,"expected":expected,"firewall_sha256":m.sha(b"-P INPUT ACCEPT\n"),"maintenance_hold":hold}
    result=m.remote(payload,types.SimpleNamespace(stage=stage),fence)
    assert result["phase"]==("unknown" if restart_error else "active")
    assert ("unshield",) in calls if not restart_error else ("unshield",) not in calls
    before=list(calls)
    assert m.remote(payload,types.SimpleNamespace(stage=stage),fence)==result
    assert calls==before and calls.count(("restart",m.SERVICE))==1
