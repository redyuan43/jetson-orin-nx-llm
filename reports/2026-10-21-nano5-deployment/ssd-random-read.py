"""Read-only O_DIRECT probe matching the packed expert size; no page-cache hits."""
import argparse,concurrent.futures,json,mmap,os,random,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('experts',type=Path);p.add_argument('--threads',type=int,default=8);p.add_argument('--reads',type=int,default=256);a=p.parse_args()
size=1351680
files=sorted(a.experts.glob('layer_*.bin'))
assert len(files)==40
fds=[os.open(str(f),os.O_RDONLY|os.O_DIRECT) for f in files]
rng=random.Random(42);work=[(rng.randrange(40),rng.randrange(256)*size) for _ in range(a.reads)]
def worker(items):
 total=0
 with mmap.mmap(-1,size) as buf:
  for layer,offset in items:
   n=os.preadv(fds[layer],[buf],offset)
   assert n==size,(layer,offset,n)
   total+=n
 return total
try:
 start=time.monotonic()
 with concurrent.futures.ThreadPoolExecutor(max_workers=a.threads) as pool:
  total=sum(pool.map(worker,[work[i::a.threads] for i in range(a.threads)]))
 elapsed=time.monotonic()-start
 print(json.dumps({'mode':'O_DIRECT read-only','threads':a.threads,'requests':a.reads,'request_bytes':size,'total_bytes':total,'elapsed_seconds':elapsed,'MiB_per_second':total/1024**2/elapsed},indent=2))
finally:
 for fd in fds:os.close(fd)
