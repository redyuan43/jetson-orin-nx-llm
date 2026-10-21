import base64,json,time,urllib.request,urllib.error
from pathlib import Path
base='http://100.73.65.44:18347';root=Path('/home/nano/minicpm-v47/deployment');token=(Path.home()/'.config/nano5-minicpm/client.key').read_text().strip();proof={}
def api(method,path,body=None,authorized=True):
 h={'Content-Type':'application/json'}
 if authorized:h['Authorization']='Bearer '+token
 req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=h,method=method)
 try:
  with urllib.request.urlopen(req,timeout=30) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
def submit(body):
 code,r=api('POST','/v1/jobs',body);assert code==202,(code,r);return r
def wait(r):
 start=time.monotonic()
 while time.monotonic()-start<600:
  code,j=api('GET',r['status_url']);assert code==200
  if j['status'] in ('succeeded','failed','unknown','cancelled'):
   print(json.dumps({'id':j['id'],'status':j['status'],'wall_s':j.get('wall_s'),'error':j.get('error')},ensure_ascii=False),flush=True);assert j['status']=='succeeded',j;return j
  time.sleep(3)
 raise TimeoutError('keep_original_job')
proof['unauthorized']=api('GET','/v1/jobs/op_'+'0'*32,authorized=False)[0];assert proof['unauthorized']==401
qa={'request_id':'api-qa-20261021-01','prompt':(root/'samples/nano5-qa-prompt.txt').read_text(),'profile':'fast-8k','max_tokens':512}
r=submit(qa);proof['idempotent']=submit(qa)['id']==r['id'];assert proof['idempotent']
proof['conflict']=api('POST','/v1/jobs',{**qa,'prompt':'different'})[0];assert proof['conflict']==409
proof['busy_mode_rejected']=api('POST','/v1/jobs',{'request_id':'busy-mode-20261021-01','action':'select_mode','mode':'comfy'})[0];assert proof['busy_mode_rejected']==409
proof['qa']=wait(r)
image={'request_id':'api-image-20261021-01','prompt':'请只回答这张照片中最明显的一件物品，最多十个汉字。','profile':'vision-8k','max_tokens':32,'image_base64':base64.b64encode((root/'samples/map-photo.jpg').read_bytes()).decode()}
proof['image']=wait(submit(image))
proof['return_comfy']=wait(submit({'request_id':'return-comfy-20261021-01','action':'select_mode','mode':'comfy'}))
proof['health']=api('GET','/healthz')[1]
p=root/'reports/service-acceptance.json';p.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n');print('acceptance saved',flush=True)
