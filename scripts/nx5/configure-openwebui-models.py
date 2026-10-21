#!/usr/bin/env python3
"""Apply the dedicated text-only model profiles through the OpenWebUI admin API."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import urllib.error

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--profiles', type=Path, required=True)
p.add_argument('--login', type=Path, default=Path.home()/'.config/nx5-openwebui/login.json')
p.add_argument('--output', type=Path, required=True)
a=p.parse_args()
login=json.loads(a.login.read_text())
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
token=None

def call(path,body=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(login['url']+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
    with opener.open(req,timeout=30) as r:return json.load(r)

token=call('/api/v1/auths/signin',{'email':login['email'],'password':login['password']})['token']
filter_id='nx5_generation_stats'
filter_source=(Path(__file__).resolve().parents[2]/'deploy/openwebui/generation_stats.py').read_text()
old_filter=next((f for f in call('/api/v1/functions/') if f['id']==filter_id),None)
filter_body={'id':filter_id,'name':'NX5 生成速度与耗时','content':filter_source,'meta':{'description':'显示真实生成TPS、纯生成耗时和含加载的请求总耗时；不修改回答正文。'}}
if old_filter:
    installed=call('/api/v1/functions/id/'+filter_id)
    if installed['content']!=filter_source:
        installed=call('/api/v1/functions/id/'+filter_id+'/update',filter_body)
else:
    installed=call('/api/v1/functions/create',filter_body)
if not installed['is_active']:
    installed=call('/api/v1/functions/id/'+filter_id+'/toggle',{})
assert installed['is_active'] and installed['content']==filter_source
proof={'filter':{'id':filter_id,'active':True,'sha256':hashlib.sha256(filter_source.encode()).hexdigest()},'profiles':[]}
for profile in json.loads(a.profiles.read_text()):
    assert profile['id'] in ('qwen36-128k','minicpm-64k')
    try:
        old=call('/api/v1/models/model?id='+profile['id'])
    except urllib.error.HTTPError as error:
        if error.code not in (401,404):raise
        old=None
    if old:
        profile['meta']['filterIds']=list(dict.fromkeys((old.get('meta',{}).get('filterIds') or [])+profile['meta']['filterIds']))
        # Only update the documented parameters/capabilities; preserve unrelated preferences.
        profile={**profile,'params':{**old.get('params',{}),**profile['params']},
                 'meta':{**old.get('meta',{}),**profile['meta']},'access_grants':old.get('access_grants')}
        result=call('/api/v1/models/model/update?id='+profile['id'],profile)
    else:
        result=call('/api/v1/models/create',profile)
    assert result['meta']['capabilities']['builtin_tools'] is False
    assert filter_id in result['meta']['filterIds']
    proof['profiles'].append({k:result[k] for k in ('id','name','params','meta')})
proof['models']=[{'id':m['id'],'name':m['name']} for m in call('/api/models')['data']]
proof['tasks']={k:v for k,v in call('/api/v1/tasks/config').items() if k.startswith('ENABLE_')}
for name in ('ENABLE_AUTOCOMPLETE_GENERATION','ENABLE_FOLLOW_UP_GENERATION','ENABLE_TAGS_GENERATION','ENABLE_TITLE_GENERATION'):
    assert proof['tasks'][name] is False
proof['passed']=True
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'models':[m['id'] for m in proof['models']],'builtin_tools':False,'max_tokens':7680}))
