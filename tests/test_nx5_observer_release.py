import importlib.util
from pathlib import Path

PATH=Path(__file__).parents[1]/'scripts/nx5/publish-observer.py'

def load():
    assert PATH.is_file(), 'NX5 observer publisher not implemented'
    spec=importlib.util.spec_from_file_location('nx5_observer_release',PATH)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_fixed_target_only_observer_differences_and_no_comfy_restart():
    m=load()
    assert m.BASELINE=='f9b117d9d4c996355aa87d9c821493ae0e294f3a'
    assert m.ALLOWED=={'scripts/nx5/resident.py',*m.VENDOR}
    assert m.OPERATION=='nx5-residency-7b979e5e377b4f90ba657f6283e7f00b'


def helpers(module):
    import ast
    tree=ast.parse(module.REMOTE)
    tree.body=[x for x in tree.body if isinstance(x,(ast.Import,ast.ImportFrom,ast.FunctionDef))]
    scope={};exec(compile(tree,'actual-nx5-remote','exec'),scope);return scope


def test_embedded_atomic_writes_private_real_json(tmp_path):
    import json
    h=helpers(load());p=tmp_path/'journal.json';h['atomic'](p,{'phase':'prepared'})
    assert json.loads(p.read_text())=={'phase':'prepared'} and p.stat().st_mode & 0o777==0o600


def test_remote_audit_rejects_writable_candidate_files_and_directories(tmp_path):
    import pytest
    h=helpers(load());root=tmp_path/'source';root.mkdir();f=root/'resident.py';f.write_bytes(b'# fixture\n');f.chmod(0o444);manifest={'files':{'resident.py':h['sha'](f.read_bytes())}}
    (root/'release.json').write_text('{}');(root/'release.json').chmod(0o444);root.chmod(0o555)
    h['audit'](root,manifest,True)
    f.chmod(0o644)
    with pytest.raises(RuntimeError,match='mode_or_owner'):h['audit'](root,manifest,True)


def test_real_git_package_and_plan_reject_any_other_source_change(tmp_path,monkeypatch):
    import json,subprocess,base64,pytest
    m=load();repo=tmp_path/'git';repo.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
    git('init');git('config','user.name','CPU fixture');git('config','user.email','fixture@invalid.local')
    actual_repo=PATH.parents[2]
    for name in m.ORIGINAL:
        dest=repo/name;dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(subprocess.check_output(['git','-C',str(actual_repo),'show',m.BASELINE+':'+name]))
    git('add','.');git('commit','-qm','fixture baseline');old=git('rev-parse','HEAD').decode().strip()
    monkeypatch.setattr(m,'BASELINE',old)
    for name in m.ALLOWED:
        dest=repo/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((actual_repo/name).read_bytes())
    git('add','.');git('commit','-qm','fixture observer');source=git('rev-parse','HEAD').decode().strip()
    bundle=m.package(repo,source);assert len(bundle['files'])==17
    media={'operation_id':m.OPERATION,'source_sha':'a'*40,'manifest_sha256':'b'*64};media['plan_sha256']=m.sha(m.canonical(media))
    media_path=tmp_path/'media.json';m.atomic(media_path,media)
    plan={**bundle,'operation_id':m.OPERATION,'publisher_sha256':m.sha(PATH.read_bytes()),'original_media':m.media_identity(media_path),'media_plan_path':str(media_path)}
    plan['plan_sha256']=m.sha(m.canonical(plan));planpath=tmp_path/'plan.json';m.atomic(planpath,plan)
    assert m.read_plan(planpath,repo)['manifest_sha256']==bundle['manifest_sha256']
    # Even a self-consistent modified manifest cannot replace fixed Git bytes.
    plan['files']['scripts/nx5/resident.py']['content']=base64.b64encode(b'# wrong source\n').decode();plan['plan_sha256']=m.sha(m.canonical({k:v for k,v in plan.items() if k!='plan_sha256'}));m.atomic(planpath,plan)
    with pytest.raises(RuntimeError,match='fixed_git_package_changed'):m.read_plan(planpath,repo)
    (repo/'scripts/nx5/service.py').write_text('# unrelated runtime change\n');git('add','.');git('commit','-qm','unrelated')
    with pytest.raises(RuntimeError,match='non_observer_source_difference'):m.package(repo,git('rev-parse','HEAD').decode().strip())


def test_full_remote_publication_preserves_jobs_controls_and_never_restarts_twice(tmp_path,monkeypatch):
    import base64,io,json,subprocess,sys,time,urllib.request,pytest
    from contextlib import redirect_stdout
    m=load();home=tmp_path/'home';base=home/'.local/share/nx5-ssd-llm';state=home/'.local/state/nx5-ssd-llm';cfg=home/'.config/nx5-ssd-llm/service.json';units=home/'.config/systemd/user'
    for d in [base/'releases',state/'jobs',cfg.parent,units]:d.mkdir(parents=True,exist_ok=True)
    original={n:subprocess.check_output(['git','-C',str(PATH.parents[2]),'show',m.BASELINE+':'+n]) for n in m.ORIGINAL}
    candidate=dict(original)
    for n in m.ALLOWED:candidate[n]=(PATH.parents[2]/n).read_bytes()
    source='c'*40
    def manifest(commit,raw):
        hashes={n:m.sha(b) for n,b in raw.items()}
        return {'source_commit':commit,'release':'nx5-ssd-llm-'+commit[:12],'commit':m.sha(json.dumps(hashes,sort_keys=True).encode()),'files':hashes}
    old=base/'releases'/m.BASELINE;old.mkdir()
    for n,b in original.items():p=old/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    (old/'release.json').write_text(json.dumps(manifest(m.BASELINE,original)))
    (base/'current').symlink_to(old)
    cfg.write_text(json.dumps({'state':str(state/'jobs')}));cfg_before=cfg.read_bytes()
    for i,status in enumerate(['succeeded','failed']):(state/'jobs'/('op_'+str(i)+'.json')).write_text(json.dumps({'status':status,'original_operation':i}))
    jobs_before={p.name:p.read_bytes() for p in (state/'jobs').iterdir()}
    private=home/'.config/siyuan-text-ingress';private.mkdir(parents=True)
    (private/'wrapper.json').write_text(json.dumps({'central':{'base_url':'http://100.91.42.28:18292','auth':{'token_env':'CPU_TOKEN'}}}))
    (private/'credentials.env').write_text('CPU_TOKEN=cpu-fixture-only\n')
    ids={'nx5-ssd-llm.service':[2561067,'original-manager'],'siyuan-image-comfy.service':[3679409,'555b78c99f9c40e295762d9fbf22cc48'],'nx5-flint-ingress.service':[2571583,'bd9d392167d041b6adeed70d6d553a31'],'nx5-flint.service':[0,''],'role-switch.service':[3679414,'original-role']}
    for n in ids:(units/n).write_text('[Service]\nOriginal='+n+'\n')
    calls=[];manager_source=[m.BASELINE];lost_restart=[False]
    def output(argv,**kw):
        if argv==['hostname','-s']:return 'nx5\n'
        if argv==['tailscale','ip','-4']:return '100.118.44.27\n'
        assert argv[:2]==['systemctl','--user']
        if argv[2]=='restart':
            assert argv==['systemctl','--user','restart','nx5-ssd-llm.service'];calls.append(argv)
            ids['nx5-ssd-llm.service']=[55555,'new-manager'];manager_source[0]=source
            if lost_restart[0]:raise subprocess.CalledProcessError(1,argv)
            return ''
        assert argv[2]=='show';name=argv[3];pid,inv=ids[name]
        return '\n'.join(f'{k}={v}' for k,v in {'ActiveState':'active' if pid else 'inactive','SubState':'running' if pid else 'dead','MainPID':str(pid),'InvocationID':inv,'FragmentPath':str(units/name),'DropInPaths':''}.items())
    class Opener:
        def open(self,req,timeout=0):
            url=req.full_url
            if url.endswith('/healthz'):
                body={'service_id':'nx5-ssd-llm','source_commit':manager_source[0],'state':'ready','active':0,'resident_pid':None,'model_loaded':False,'lease_status':'released','resident_error':None,'observed_at':time.time(),
                  'managed_comfy':{'mode':'comfy','state':'ready','active':0,'profile_id':'nx5-qwen21','profile_version':'qwen21-t2i-nx5-20261005','phase':'completed','policy_state':'applied','model_loaded':False if manager_source[0]==source else None}}
            else:
                assert url.endswith('/internal/v1/resource-groups/nx5-igpu-group/activity')
                body={'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'comfy','state':'ready','fresh':True,'observed_at':time.time(),'active':0,'leases':[],'pending':[],'transition':None,'policy_demand':None}
            return io.BytesIO(json.dumps(body).encode())
    original_text=Path.read_text;original_bytes=Path.read_bytes
    def read_text(p,*a,**kw):
        if str(p).startswith('/proc/') and p.name=='stat':return p.parent.name+' (cpu fixture) '+' '.join(['0']*19+[p.parent.name])
        return original_text(p,*a,**kw)
    def read_bytes(p):
        if str(p).startswith('/proc/') and p.name=='cmdline':return b'cpu\0original\0'+p.parent.name.encode()+b'\0'
        return original_bytes(p)
    monkeypatch.setattr(Path,'home',lambda:home);monkeypatch.setattr(Path,'read_text',read_text);monkeypatch.setattr(Path,'read_bytes',read_bytes)
    monkeypatch.setattr(subprocess,'check_output',output);monkeypatch.setattr(urllib.request,'build_opener',lambda *a:Opener())
    def execute(action,p):
        stream=io.StringIO();monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(p)));monkeypatch.setattr(sys,'argv',['publisher',action])
        with redirect_stdout(stream):
            try:exec(compile(m.REMOTE,'frozen-real-remote','exec'),{})
            except SystemExit as e:assert e.code==0
        return json.loads(stream.getvalue())
    baseline=execute('capture',{})
    bundle=manifest(source,candidate);p={'operation_id':m.OPERATION,'source_commit':source,'manifest':bundle,'manifest_sha256':m.sha(m.canonical(bundle)),'plan_sha256':'a'*64,'baseline':baseline,'candidate_path':str(base/'releases'/source),'original_files':{n:m.sha(b) for n,b in original.items()},'files':{n:{'content':base64.b64encode(b).decode(),'sha256':m.sha(b)} for n,b in candidate.items()}}
    # Simulate the restart succeeding while its command result is lost.
    lost_restart[0]=True
    with pytest.raises(subprocess.CalledProcessError):execute('apply',p)
    assert execute('inspect',p)['phase']=='restart_intent'
    with pytest.raises(RuntimeError,match='original_journal_exists_query_only'):execute('apply',p)
    latest=execute('verify',p)
    assert latest['manager']['pid']==55555 and latest['health']['managed_comfy']['model_loaded'] is False
    assert len(calls)==1 and latest['jobs']==baseline['jobs'] and latest['protected']==baseline['protected']
    assert cfg.read_bytes()==cfg_before and {q.name:q.read_bytes() for q in (state/'jobs').iterdir()}==jobs_before
    assert all(q.stat().st_mode&0o777==(0o555 if q.is_dir() else 0o444) for q in [base/'releases'/source,* (base/'releases'/source).rglob('*')])


def test_health_and_authoritative_activity_fail_closed_without_inference():
    import copy,time,pytest
    m=load();now=time.time();activity={'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'comfy','state':'ready','fresh':True,'observed_at':now,'active':0,'leases':[],'pending':[],'transition':None,'policy_demand':None}
    h={'source_commit':'a'*40,'service_id':'nx5-ssd-llm','state':'ready','active':0,'resident_pid':None,'model_loaded':False,'lease_status':'released','resident_error':None,'observed_at':now,'managed_comfy':{'mode':'comfy','state':'ready','active':0,'profile_id':'nx5-qwen21','profile_version':'qwen21-t2i-nx5-20261005','phase':'completed','policy_state':'applied','model_loaded':False}}
    node={'health':h,'jobs':{'count':7},'central_activity':activity,'protected':{'units':{'nx5-flint.service':{'ActiveState':'inactive','MainPID':'0'}}}}
    m.validate_node(node,candidate=True,source='a'*40,now=now)
    for key,value in [('resident_pid',123),('active',1),('lease_status','unknown'),('observed_at',now-61),('source_commit','b'*40)]:
        bad=copy.deepcopy(node);bad['health'][key]=value
        with pytest.raises(RuntimeError):m.validate_node(bad,candidate=True,source='a'*40,now=now)
    bad=copy.deepcopy(node);bad['health']['managed_comfy']['model_loaded']=None
    with pytest.raises(RuntimeError,match='comfy_residency_unknown'):m.validate_node(bad,candidate=True,source='a'*40,now=now)
    for key,value in [('mode','off'),('state','unknown'),('policy_demand',{'state':'unknown'}),('transition',{}),('pending',[{'operation_id':'preserve'}]),('leases',[{'lease_id':'keep'}]),('fresh',False),('observed_at',now-61)]:
        bad=copy.deepcopy(activity);bad[key]=value
        with pytest.raises(RuntimeError):m.validate_activity(bad,now)
