#!/usr/bin/env python3
"""KVMem / llama-kvmem-server 上下文基准：prefill / decode / TTFT。

按本地分词器精确控制 prompt token 数；每轮加唯一前缀，避免命中前缀缓存。

用法:
  python3 bench_ctx.py --tokens 4096 --max-tokens 128 --rounds 3
  python3 bench_ctx.py --tokens 131072 --max-tokens 64 --rounds 1
"""
import argparse, json, time
import urllib.request

def http(port, path, payload=None, timeout=7200):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                               headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=timeout)

RECORD = ("Record {i} notes that sensor bank {b} reported value {v} "
          "during the calibration sweep of the assembly line. ")

def build_prompt(target_tokens, record_tokens=20):
    """单个 RECORD 约 20 token；按目标 token 数生成，首条带唯一时间戳。"""
    n = max(16, int(target_tokens / record_tokens))
    out = [f"[session {time.time():.9f}] "]
    for i in range(n):
        out.append(RECORD.format(i=i, b=i % 97, v=i * 7 % 1000))
    return "".join(out)

def run_once(port, prompt, max_tokens, model):
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.0, "stream": True,
               "stream_options": {"include_usage": True}}
    t0 = time.time(); ttft = None; text = []; usage = None
    with http(port, "/v1/chat/completions", payload) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            blob = line[5:].strip()
            if blob == "[DONE]":
                break
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices", []):
                d = ch.get("delta", {})
                piece = d.get("content") or d.get("reasoning_content") or ""
                if piece:
                    if ttft is None:
                        ttft = time.time() - t0
                    text.append(piece)
    return {"ttft_s": ttft, "total_s": time.time() - t0, "usage": usage,
            "sample": "".join(text)[:80]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--tokens", type=int, default=4096, help="目标 prompt token 数")
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--n-ctx", type=int, default=None, help="可用上下文，用于把 prompt 夹到上限内")
    a = ap.parse_args()

    with http(a.port, "/v1/models") as r:
        model = json.load(r)["data"][0]["id"]

    rows = []
    ratio = 1.0
    for i in range(a.rounds):
        want = int(a.tokens * ratio)
        if a.n_ctx:
            want = min(want, a.n_ctx - a.max_tokens - 64)
        prompt = build_prompt(want)
        res = run_once(a.port, prompt, a.max_tokens, model)
        u = res["usage"] or {}
        pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
        if pt and i == 0:
            ratio = a.tokens / pt          # 用首轮实测校准后续轮次
        ttft, total = res["ttft_s"], res["total_s"]
        prefill = pt / ttft if (pt and ttft) else None
        decode = (ct - 1) / (total - ttft) if (ct and ttft and total > ttft) else None
        row = {"round": i, "prompt_tokens": pt, "completion_tokens": ct,
               "ttft_s": round(ttft, 3) if ttft else None,
               "total_s": round(total, 2),
               "prefill_tps": round(prefill, 2) if prefill else None,
               "decode_tps": round(decode, 2) if decode else None,
               "cache_hit": u.get("prompt_cache_hit_tokens"),
               "sample": res["sample"][:60]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    measured = rows[1:] if len(rows) > 1 else rows
    def avg(k):
        v = [r[k] for r in measured if r[k]]
        return round(sum(v) / len(v), 2) if v else None
    print("SUMMARY " + json.dumps({
        "target_tokens": a.tokens, "measured_rounds": len(measured),
        "prompt_tokens": measured[0]["prompt_tokens"],
        "avg_prefill_tps": avg("prefill_tps"), "avg_decode_tps": avg("decode_tps"),
        "avg_ttft_s": avg("ttft_s")}, ensure_ascii=False))

if __name__ == "__main__":
    main()