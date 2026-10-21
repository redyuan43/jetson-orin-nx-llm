#!/usr/bin/env python3
"""Live OpenAI SDK smoke on nx5; uses only the node-local dedicated token."""
from openai import OpenAI
import openai,json,time
from openai import DefaultHttpxClient
from pathlib import Path
import argparse
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--output", type=Path, required=True)
args = p.parse_args()
args.output.parent.mkdir(parents=True, exist_ok=True)
key=(Path.home()/'.config/nx5-ssd-llm/client.key').read_text().strip()
client=OpenAI(base_url='http://100.118.44.27:18347/v1',api_key=key,max_retries=0,timeout=300,
              http_client=DefaultHttpxClient(trust_env=False))
models=client.models.list()
assert {m.id for m in models.data}=={'qwen36-128k','minicpm-64k'}
start=time.time();chunks=[]
stream=client.chat.completions.create(model='minicpm-64k',
    messages=[{'role':'user','content':'请严格原样输出以下内容，不要加其他文字：ALPHA STOP OMEGA'}],
    temperature=0,seed=42,stop=['STOP'],max_completion_tokens=64,stream=True,stream_options={'include_usage':True})
for chunk in stream:chunks.append(chunk.model_dump())
text=''.join(c['choices'][0]['delta'].get('content') or '' for c in chunks if c['choices'])
assert 'ALPHA' in text and 'STOP' not in text and 'OMEGA' not in text,text
assert chunks[-1]['usage']['completion_tokens']>0
assert chunks[-2]['choices'][0]['finish_reason']=='stop'
proof={'sdk_version':openai.__version__,'model':'minicpm-64k','text':text,'chunks':chunks,'wall_s':time.time()-start,'passed':True}
args.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in proof.items() if k!='chunks'}),flush=True)
