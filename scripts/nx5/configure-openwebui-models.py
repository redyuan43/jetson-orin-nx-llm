#!/usr/bin/env python3
"""Apply the dedicated text-only model profiles through the OpenWebUI admin API."""
import argparse
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
proof={'profiles':[]}
for profile in json.loads(a.profiles.read_text()):
    assert profile['id'] in ('qwen36-128k','minicpm-64k')
    try:
        old=call('/api/v1/models/model?id='+profile['id'])
    except urllib.error.HTTPError as error:
        if error.code not in (401,404):raise
        old=None
    if old:
        # Only update the documented parameters/capabilities; preserve unrelated preferences.
        profile={**profile,'params':{**old.get('params',{}),**profile['params']},
                 'meta':{**old.get('meta',{}),**profile['meta']},'access_grants':old.get('access_grants')}
        result=call('/api/v1/models/model/update?id='+profile['id'],profile)
    else:
        result=call('/api/v1/models/create',profile)
    assert result['meta']['capabilities']['builtin_tools'] is False
    proof['profiles'].append({k:result[k] for k in ('id','name','params','meta')})
proof['models']=[{'id':m['id'],'name':m['name']} for m in call('/api/models')['data']]
proof['tasks']={k:v for k,v in call('/api/v1/tasks/config').items() if k.startswith('ENABLE_')}
for name in ('ENABLE_AUTOCOMPLETE_GENERATION','ENABLE_FOLLOW_UP_GENERATION','ENABLE_TAGS_GENERATION','ENABLE_TITLE_GENERATION'):
    assert proof['tasks'][name] is False
proof['passed']=True
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'models':[m['id'] for m in proof['models']],'builtin_tools':False,'max_tokens':7680}))
