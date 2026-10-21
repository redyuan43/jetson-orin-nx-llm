#!/usr/bin/env python3
"""Check cache isolation and graceful unload using synthetic prompts, never user chats.

Run on nx5 after the Octop fixture, with the fixture job id as an argument.
Credentials are read privately on nx5; reports exclude prompts and keys.
"""
import argparse
import json
from pathlib import Path
import time
import urllib.request

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--fixture-job',required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
root=Path.home()/'.local/state/nx5-ssd-llm'
key=(Path.home()/'.config/nx5-ssd-llm/client.key').read_text().strip()
base='http://100.118.44.27:18347'
def call(path,body=None):
    req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=900) as r:
        data=json.load(r);job=r.headers.get('X-Job-ID')
    if job:
        proof=json.loads((root/'jobs'/(job+'.json')).read_text())
        data['_process']={k:proof['result'][k] for k in ('resident_pid','cold_start','model_loaded_after','lease_status','min_available_mib')}
    return data
health=call('/healthz');assert health['state']=='ready' and health['active']==0
source=root/'jobs'/a.fixture_job/'prompt.txt'
chat=json.loads(source.read_text());assert len(chat['messages'])==2 and chat['messages'][-1]['role']=='user'
assert 'current_time' in chat['messages'][-1]['content'], 'Only the synthetic acceptance fixture is allowed'
proof={'before':health,'isolation':[]}
for secret in ('ORANGE_739','VIOLET_826'):
    body={**chat,'model':'minicpm-64k','max_tokens':512,'temperature':0,'enable_thinking':True}
    body['messages']=[chat['messages'][0],{'role':'user','content':f'这是缓存隔离验收，不需要任何工具。本轮口令为 {secret}。只原样回答本轮口令，不要使用其他轮的口令。'}]
    started=time.monotonic();answer=call('/v1/chat/completions',body)
    assert answer['choices'][0]['message']['content'].strip()==secret, 'Answer leaked or failed to follow the changed user input'
    assert answer['usage']['prompt_tokens_details']['cached_tokens']>7000
    assert answer['_process']['resident_pid']==health['resident_pid']
    proof['isolation'].append({'expected':secret,'answer':secret,'wall_s':time.monotonic()-started,
        'usage':answer['usage'],'timings':answer['timings'],'process':answer['_process']})
assert call('/healthz')['active']==0
unloaded=call('/v1/model/unload',{});assert not unloaded['model_loaded'] and unloaded['lease_status']=='released'
assert not Path('/proc',str(health['resident_pid'])).exists()
proof['unloaded']=unloaded
a.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
# Reload once, leaving the model resident; callers may warm their own prefixes.
answer=call('/v1/chat/completions',{'model':'minicpm-64k','messages':[{'role':'user','content':'请计算 17+23，只回答数字。'}],'max_tokens':256,'enable_thinking':True,'temperature':0})
message=answer['choices'][0]['message']
assert '40' in message.get('content','')+message.get('reasoning_content','')
assert answer['choices'][0]['finish_reason']=='stop'
assert answer['_process']['cold_start'] and answer['_process']['resident_pid']!=health['resident_pid']
final=call('/healthz');assert final['state']=='ready' and final['model_loaded'] and final['lease_status']=='held'
proof.update(reload={'content':message.get('content',''),'reasoning_content':message.get('reasoning_content',''),'usage':answer['usage'],'timings':answer['timings'],'process':answer['_process']},after=final,passed=True)
a.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(proof,ensure_ascii=False),flush=True)
