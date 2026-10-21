"""One-shot administrator comparison, under the existing central resource lease."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

root = Path('/home/nano/minicpm-v47')
release = root / 'releases/1a51ae63257c745b1f56cde5d0648fcb60564199'
folder = root / 'deployment/validation-thinking'
folder.mkdir(exist_ok=True)
proof_path = folder / 'lease-proof.json'
if proof_path.exists():
    raise SystemExit('Existing operation: inspect its original receipt; do not replay')
config = json.loads((Path.home()/'.config/nano5-minicpm/config.json').read_text())
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(method, path, body=None, central=True):
    base = config['central'] if central else 'http://100.73.65.44:18347'
    key = 'executor_token_file' if central else 'client_token_file'
    headers = {'Authorization': 'Bearer '+Path(config[key]).read_text().strip(), 'Content-Type':'application/json'}
    request = urllib.request.Request(base+path, data=None if body is None else json.dumps(body).encode(),headers=headers,method=method)
    with opener.open(request,timeout=20) as r:
        return json.load(r)

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def save():
    temp=proof_path.with_suffix('.tmp')
    temp.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o600);temp.replace(proof_path)

def wait_service_mode(mode, seconds=60):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        h=api('GET','/healthz',central=False)
        if h['mode']==mode and h['state']=='ready' and h['active']==0 and h['coordinator_reachable']:
            return h
        time.sleep(3)
    raise RuntimeError('Mode not observed ready; do not run inference')

prompt=root/'deployment/samples/pelican-user-prompt-20261021.txt'
settings=json.loads((release/'configs/minicpm-v47-nano5.json').read_text())
for name,expected in settings['library_sha256'].items():
    assert hashlib.sha256((root/settings['library_dir']/name).read_bytes()).hexdigest()==expected
assert hashlib.sha256((release/settings['chat_template']).read_bytes()).hexdigest()==settings['chat_template_sha256']
assert hashlib.sha256((root/settings['cli']).read_bytes()).hexdigest()==settings['cli_sha256']
request={'test':'nano-pelican-thinking-20261021-01','prompt_sha256':hashlib.sha256(prompt.read_bytes()).hexdigest(), 'reasoning':'on','max_tokens':6144,'profile':'fast-8k'}
op='op_'+hashlib.sha256(request['test'].encode()).hexdigest()[:32]
proof={'operation_id':op,'request':request,'created_at':time.time(),'status':'waiting','initial_health':api('GET','/healthz',central=False)}
save()
body={'operation_id':op,'request_digest':digest(request),'candidates':[{'backend_id':'nano5-minicpm','mapping_version':'nano5-minicpm-20261021-r1','resources':[{'resource_id':'nano5-igpu','units':1}],'mode':'minicpm','probe_status':'ready','probed_at':time.time()}]}
started=time.monotonic();lease=None
while time.monotonic()-started<1200:
    body['candidates'][0]['probed_at']=time.time()
    reservation=api('POST','/internal/v1/reservations',body)
    if reservation['status']=='held':
        lease=api('GET','/internal/v1/leases/'+op)
        assert lease['status']=='held' and lease['operation_id']==op
        break
    if reservation['status'] not in ('waiting','preparing'):
        raise RuntimeError('Unexpected reservation status')
    time.sleep(3)
if lease is None:raise SystemExit('Reservation timeout; reconcile original operation')
proof.update(status='held',lease_digest=lease['request_digest'],mode_before_run=wait_service_mode('minicpm'))
proof['comfy_before_run']=subprocess.check_output(['systemctl','--user','show',config['comfy_unit'],'--property=ActiveState,MainPID'],text=True)
assert 'ActiveState=inactive' in proof['comfy_before_run'] and 'MainPID=0' in proof['comfy_before_run']
save()
args=[sys.executable,str(release/'scripts/minicpm/bench-audit.py'),'nano-pelican-thinking',
      '--root',str(root),'--prompt',str(prompt),'--output',str(folder),'--tokens','6144',
      '--library-dir',str(root/settings['library_dir']),'--chat-template-file',str(release/settings['chat_template'])]
parameters={**settings['common_parameters'],**settings['profiles']['fast-8k']['parameters'],'reasoning':'on'}
for k,v in parameters.items():
    option='--'+k.replace('_','-')
    if isinstance(v,bool):
        if v:args.append(option)
    else:args.extend([option,str(v)])
proof.update(status='running',started_at=time.time(),command=args);save()
print(json.dumps({'operation':op,'status':'running','reasoning':'on','comfy_stopped':True}),flush=True)
with (folder/'driver.log').open('w') as log:
    result=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT)
summary_path=folder/'nano-pelican-thinking.json'
if not summary_path.exists():
    proof.update(status='unknown',error='Inference receipt missing; keep original lease');save();raise SystemExit(1)
summary=json.loads(summary_path.read_text())
proof.update(status='releasing',finished_at=time.time(),rc=result.returncode,wall_s=summary['wall_s'],stop_reason=summary['stop_reason'])
# The benchmark owns and reaps the child process. Verify no inference remains before release.
for p in Path('/proc').iterdir():
    if not p.name.isdigit():continue
    try:
        if (p/'exe').resolve(strict=True).name.startswith('llama-'):
            proof.update(status='unknown',error='Inference process remains; keep lease');save();raise SystemExit(1)
    except (FileNotFoundError,PermissionError,ProcessLookupError):pass
save()
terminal='succeeded' if result.returncode==0 and summary['stop_reason'] is None else 'failed'
api('POST','/internal/v1/leases/'+op+'/release',{'terminal_status':terminal,'evidence_digest':digest(summary),'request_digest':lease['request_digest']})
proof['lease_after']=api('GET','/internal/v1/leases/'+op)['status'];proof['status']=terminal;save()
wait_service_mode('minicpm')
restore=api('POST','/v1/jobs',{'request_id':'return-comfy-after-thinking-20261021-01','action':'select_mode','mode':'comfy'},central=False)
proof['restore_id']=restore['id'];save()
for _ in range(100):
    job=api('GET',restore['status_url'],central=False)
    if job['status'] in ('succeeded','failed','unknown','cancelled'):
        proof['restore_result']=job;save();break
    time.sleep(3)
else:raise SystemExit('Keep polling original restore job')
assert job['status']=='succeeded'
proof['final_health']=wait_service_mode('comfy');save()
print(json.dumps({'status':terminal,'wall_s':summary['wall_s'],'restored':'comfy','lease':proof['lease_after']}),flush=True)
