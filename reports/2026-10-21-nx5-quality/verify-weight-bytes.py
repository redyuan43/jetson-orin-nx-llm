import collections,hashlib,json,os,sys,time
from pathlib import Path
import numpy as np
sys.path.insert(0,'/home/nx/github/qwen38-kvmem/minicpm-les/llama.cpp-omni/gguf-py')
from gguf import GGUFReader
p=Path('/home/nx/github/qwen38-kvmem/models/MiniCPM-V-4.7')
out=Path('/home/nx/github/qwen38-kvmem/minicpm-audit-20261021/quality');out.mkdir(exist_ok=True)
start=time.monotonic()
full=GGUFReader(str(p/'MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf'))
small=GGUFReader(str(p/'stripped.gguf'))
original={t.name:t for t in full.tensors}
for label,r in [('full',full),('stripped',small)]:
 template=r.fields['tokenizer.chat_template'].contents()
 (out/(label+'-chat-template.jinja')).write_text(template)
 print(label,'template offset',r.fields['tokenizer.chat_template'].offset,'data offset',r.data_offset,flush=True)
counts=collections.Counter(t.tensor_type.name for t in full.tensors)
checked=0; dense=0
for t in small.tensors:
 s=original[t.name]
 assert t.tensor_type==s.tensor_type and list(t.shape)==list(s.shape),t.name
 a=np.asarray(t.data).view(np.uint8).reshape(-1)
 b=np.asarray(s.data).view(np.uint8).reshape(-1)
 assert len(a)==len(b),t.name
 for i in range(0,len(a),4*1024*1024):
  assert np.array_equal(a[i:i+4*1024*1024],b[i:i+4*1024*1024]),t.name
 checked+=len(a);dense+=1
print('All non-expert tensors match',dense,checked,flush=True)
manifest=json.loads((p/'experts/manifest.json').read_text())
fd=os.open(p/'MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf',os.O_RDONLY)
expert_slices=0; expert_bytes=0
try:
 for n,e in enumerate(manifest['entries']):
  t=original[e['tensor_name']]
  assert int(t.data_offset)==e['source_offset'],e['tensor_name']
  assert int(t.n_bytes)==e['exact_byte_length'],e['tensor_name']
  assert t.tensor_type.name==e['quant_type']
  assert list(t.shape)==e['shape']
  count=e['shape'][-1];size=e['bytes_per_expert']
  with (p/'experts'/e['repacked_file']).open('rb') as packed:
   for idx in range(count):
    expected=os.pread(fd,size,e['source_offset']+idx*size)
    actual=os.pread(packed.fileno(),size,e['repacked_offset']+idx*e['expert_stride'])
    assert len(expected)==size and expected==actual,(e['tensor_name'],idx)
    expert_slices+=1;expert_bytes+=size
  if (n+1)%30==0:print('Expert tensors checked',n+1,flush=True)
finally:os.close(fd)
result={'source':str(p/'MiniCPM-V-4_7-35B-A3B-mix_q3gud_im.gguf'),'non_expert_tensors':dense,'non_expert_bytes':checked,'expert_tensors':len(manifest['entries']),'expert_tensor_slices':expert_slices,'expert_bytes':expert_bytes,'all_compared_bytes_equal':True,'tensor_quantization_counts':dict(counts),'template_equal':full.fields['tokenizer.chat_template'].contents()==small.fields['tokenizer.chat_template'].contents(),'wall_s':time.monotonic()-start,'limitation':'Checks repacking against the local quantized GGUF, not BF16 equivalence or runtime math'}
(out/'weight-byte-check.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2),flush=True)
