#!/usr/bin/env python3
"""读取 speculative 统计，测真实 decode 速度。"""
import json, sys, time, urllib.request

def chat(port, prompt, max_tokens):
    body = {"model": "q", "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.0, "stream": False}
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                               data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=1800) as resp:
        d = json.load(resp)
    wall = time.time() - t0
    spec = d.get("kvmem", {}).get("speculative", {})
    tm = d.get("timings", {})
    return {
        "wall_s": round(wall, 2),
        "predicted_n": tm.get("predicted_n"),
        "predicted_per_second": round(tm.get("predicted_per_second", 0), 2),
        "acceptance": spec.get("acceptance_rate"),
        "tokens_per_round": spec.get("tokens_per_round"),
        "ms_per_round": round(spec.get("ms_per_round", 0), 1),
        "drafted": spec.get("drafted_tokens"),
        "accepted": spec.get("accepted_tokens"),
        "per_pos": spec.get("accepted_per_position"),
    }

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8400
    ntok = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    warm = "Explain in detail how a hash table works, covering collisions, resizing and complexity."
    prompts = [
        "Write a Python function that merges two sorted lists. Include docstring and comments. Then explain the time complexity step by step.",
        "Explain the difference between TCP and UDP, listing at least five concrete use cases for each protocol.",
        "Describe how a CPU cache hierarchy works, including L1 L2 L3, cache lines and associativity. Give examples.",
    ]
    print("warmup:", json.dumps(chat(port, warm, 64)))
    results = []
    for p in prompts:
        r = chat(port, p, ntok)
        print(json.dumps(r), flush=True)
        results.append(r)
    avg = sum(r["predicted_per_second"] for r in results) / len(results)
    acc = [r["acceptance"] for r in results if r["acceptance"] is not None]
    print(f"AVG tok/s = {avg:.2f}   acceptance = {sum(acc)/len(acc):.3f}" if acc else f"AVG tok/s = {avg:.2f} (no MTP)")
