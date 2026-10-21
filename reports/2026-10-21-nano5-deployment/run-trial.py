import argparse,json,subprocess,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('tag');p.add_argument('--ctx',type=int,default=8192);p.add_argument('--window',type=int,required=True);p.add_argument('--rows',type=int,default=8);p.add_argument('--draft',type=int,default=0);p.add_argument('--ubatch',type=int,default=8);p.add_argument('--tokens',type=int,default=512);p.add_argument('--prefetch',type=int,default=0);p.add_argument('--prompt',default='samples/pelican-prompt.txt');p.add_argument('--sampling',default='benchmark');p.add_argument('--timeout',type=int,default=600);a=p.parse_args()
root=Path('/home/nano/minicpm-v47');repo=root/'deployment';out=repo/'validation'
cmd=[sys.executable,str(repo/'scripts/minicpm/bench-audit.py'),a.tag,'--root',str(root),'--prompt',str(repo/a.prompt),'--output',str(out),'--ctx',str(a.ctx),'--window',str(a.window),'--bank','64','--ubatch',str(a.ubatch),'--rows',str(a.rows),'--tokens',str(a.tokens),'--draft',str(a.draft),'--prefetch',str(a.prefetch),'--mapped-experts','--io-threads','8','--threads','6','--sampling',a.sampling,'--reasoning','off','--chat-template-file',str(repo/'configs/minicpm-v47-instruct.jinja'),'--ctx-checkpoints','2','--cache-ram-mib','64','--library-dir',str(root/'optimization/cuda-mapped-v2'),'--min-available-mib','512','--timeout',str(a.timeout)]
with (out/(a.tag+'.driver.log')).open('w') as f:rc=subprocess.call(cmd,stdout=f)
p=out/(a.tag+'.json')
if p.exists():
 j=json.loads(p.read_text());print(json.dumps({'tag':a.tag,**{k:j[k] for k in ['rc','stop_reason','wall_s','min_available_kib','physical_read_bytes','metrics']}},indent=2),flush=True)
else:print((out/(a.tag+'.driver.log')).read_text()[-3000:],flush=True)
sys.exit(rc)
