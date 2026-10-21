#!/usr/bin/env python3
"""KVMem / llama-kvmem-server 4K 上下文基准：prefill / decode / 首 token 延迟。

用法: python3 bench_4k.py [--tokens 4096] [--max-tokens 128] [--port 8400]
"""
import argparse, json, time, sys, urllib.request

def post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=3600)

def build_prompt(target_tokens):
    # 近似 4 token/英文单词；用不重复的填充文本，避免前缀缓存干扰
    base = ("The quick brown fox jumps over the lazy dog while the engineer documents "
            "the benchmark procedure for the local inference stack. ")
    words = []
    i = 0
    while len(words) * 1.4 < target_tokens * 1.4:
        words.append(base if i % 5 == 0 else f"item-{i}")
        i += 1
        if len(words) >= target_tokens // 4:
            break
    return " ".join(words)

def stream_run(port, prompt, max_tokens, model):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    ttft = None
    chunks = 0
    text = []
    usage = None
    with post(port, "/v1/chat/completions", payload) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices", []):
                delta = ch.get("delta", {})
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if piece:
                    if ttft is None:
                        ttft = time.time() - t0
                    chunks += 1
                    text.append(piece)
    total = time.time() - t0
    return {"ttft_s": ttft, "total_s": total, "chunks": chunks,
            "usage": usage, "sample": "".join(text)[:160]}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--tokens", type=int, default=4096)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/v1/models", timeout=30) as r:
        models = json.load(r)
    model_id = models["data"][0]["id"] if models.get("data") else args.model

    prompt = build_prompt(args.tokens)
    out = {"model_id": model_id, "target_ctx_tokens": args.tokens, "rounds": []}
    for i in range(args.rounds):
        # 每轮加一个唯一前缀，避免命中前缀缓存
        p = f"[run {i} {time.time()}] " + prompt
        res = stream_run(args.port, p, args.max_tokens, model_id)
        if i == 0:
            out["cold"] = res
        else:
            out["rounds"].append(res)
        print(json.dumps({"round": i, **res}, ensure_ascii=False), flush=True)
    print(json.dumps(out, ensure_ascii=False))

if __name__ == "__main__":
    main()
