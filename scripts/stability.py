#!/usr/bin/env python3
"""连续请求稳定性测试：N 次 4K prompt，统计存活数。"""
import json, sys, time, urllib.request

port, nreq, ntok = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])

def one(n):
    parts = [f"[s{time.time():.9f}] "]
    i = 0
    while len(parts) < n // 20:
        parts.append(f"Record {i} notes sensor {i%97} value {i*7%1000} in the sweep. ")
        i += 1
    body = {"model": "x", "messages": [{"role": "user", "content": "".join(parts)}],
            "max_tokens": 32, "stream": False}
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=1200) as resp:
            d = json.load(resp)
        u = d.get("usage", {})
        return True, u.get("prompt_tokens"), time.time() - t0
    except Exception as e:
        return False, type(e).__name__, time.time() - t0

ok = 0
for i in range(1, nreq + 1):
    good, info, dt = one(ntok)
    print(f"  req{i}: {'OK' if good else 'FAIL'} {info} {dt:.1f}s", flush=True)
    if not good:
        break
    ok += 1
print(f"RESULT survived {ok}/{nreq}")
