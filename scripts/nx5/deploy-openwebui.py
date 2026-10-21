#!/usr/bin/env python3
"""Create the dedicated nx6 OpenWebUI from a committed manifest, preserving existing data.

The nx5 API key travels only through SSH pipes into a private nx6 environment file.
The one-time UI login is saved locally with mode 0600, never printed or committed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--commit', required=True)
p.add_argument('--apply', action='store_true')
a = p.parse_args()
repo = Path(__file__).resolve().parents[2]
commit = subprocess.check_output(['git', 'rev-parse', a.commit+'^{commit}'], cwd=repo, text=True).strip()
read = lambda name: subprocess.check_output(['git','show',commit+':'+name], cwd=repo)
manifest = json.loads(read('deploy/openwebui/release.json'))
settings_bytes = read('deploy/openwebui/settings.json')
assert hashlib.sha256(settings_bytes).hexdigest() == manifest['settings_sha256']
payload = {'manifest': manifest, 'settings': json.loads(settings_bytes), 'source_commit': commit, 'apply': a.apply}
if a.apply:
    payload['api_key'] = subprocess.check_output(['ssh','-o','Hostname=192.168.100.154','nx5',
        'cat','.config/nx5-ssd-llm/client.key'],text=True).strip()
code = r'''
import json,os,secrets,socket,subprocess,hashlib
from pathlib import Path
p=PAYLOAD
base=Path.home()/'.local/share/nx5-openwebui'
private=Path.home()/'.config/nx5-openwebui'
name='nx5-openwebui'
existing=subprocess.run(['docker','container','inspect',name],capture_output=True)
if existing.returncode==0:raise RuntimeError('existing_container: inspect and update deliberately, do not replace')
if (base/'data').exists() or private.exists():raise RuntimeError('existing_data: preserve and reconcile before retrying')
info=json.loads(subprocess.check_output(['docker','image','inspect',p['manifest']['image']],text=True))[0]
assert info['Architecture']=='arm64'
with socket.socket() as s:s.bind(('100.67.179.113',18348))
if not p['apply']:
    print(json.dumps({'preflight':'passed','source_commit':p['source_commit'],'modified':False}))
else:
    assert len(p['api_key'])>=32
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    (base/'data').mkdir(mode=0o700)
    private.mkdir(parents=True,mode=0o700)
    credentials={'url':p['settings']['WEBUI_URL'],'email':'ivan@example.com','password':secrets.token_urlsafe(24)}
    login=private/'login.json';login.write_text(json.dumps(credentials));login.chmod(0o600)
    values={**p['settings'],'WEBUI_SECRET_KEY':secrets.token_urlsafe(48),'OPENAI_API_KEYS':p['api_key'],
        'WEBUI_ADMIN_EMAIL':credentials['email'],'WEBUI_ADMIN_PASSWORD':credentials['password'],'WEBUI_ADMIN_NAME':'Ivan'}
    env=private/'runtime.env'
    assert all('\n' not in v and '\r' not in v for v in values.values())
    env.write_text(''.join(k+'='+v+'\n' for k,v in values.items()));env.chmod(0o600)
    command=['docker','run','-d','--name',name,'--restart','unless-stopped',
        '--publish','100.67.179.113:18348:8080','--memory','1536m','--memory-swap','1536m','--cpus','2',
        '--env-file',str(env),'--mount','type=bind,src='+str(base/'data')+',dst=/app/backend/data',
        '--label','nx5-openwebui.source_commit='+p['source_commit'],p['manifest']['image']]
    identifier=subprocess.check_output(command,text=True).strip()
    record={**p['manifest'],'source_commit':p['source_commit'],'container_id':identifier,
            'url':credentials['url'],'settings':p['settings'],'network':'Tailnet only','auth':True}
    (base/'deployment.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps({'started':True,'container':name,'source_commit':p['source_commit'],'url':credentials['url']}))
'''
subprocess.run(['ssh','nx6','python3','-'],input=code.replace('PAYLOAD',repr(payload),1),text=True,check=True)
if a.apply:
    private = Path.home()/'.config/nx5-openwebui'
    private.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw = subprocess.check_output(['ssh','nx6','cat','.config/nx5-openwebui/login.json'])
    target=private/'login.json'
    with target.open('xb') as f:
        target.chmod(0o600)
        f.write(raw)
    credentials=json.loads(raw)
    text=private/'login.txt'
    with text.open('x') as f:
        text.chmod(0o600)
        f.write('OpenWebUI: '+credentials['url']+'\n账号: '+credentials['email']+'\n密码: '+credentials['password']+'\n')
    print(json.dumps({'login_saved_to':str(text),'secrets_printed':False}))
