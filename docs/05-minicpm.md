# MiniCPM-V 4.7 35B-A3B on Jetson Orin NX 16GB

## 1. Model architecture

Read from the GGUF metadata (`qwen35moe`), not from documentation:

| Category | Parameter | Value |
|---|---|---|
| Size | layers | 40 |
| | total params | 35.5 B |
| | **activated per token** | **~3 B** (8 of 256 experts) |
| Attention | GDN (linear) layers | **30** |
| | full-attention layers | **10** |
| | pattern | `GGGF` repeating (`full_attention_interval=4`) |
| | heads | 16 Q / **2 KV** (GQA 8:1), head_dim 256 |
| MoE | experts per layer | **256** |
| | experts per token | **8** + 1 shared |
| | expert FFN width | 512 |
| GDN state | conv_kernel / state_size / time_step_rank | 4 / 128 / 32 |

Only **10 of 40 layers** keep a KV cache, which is why long context is cheap here.

## 2. Why the experts live on SSD

| Item | Size |
|---|---|
| non-expert weights (`stripped.gguf`) | 1.55 GB |
| expert weights (`experts/`, 40 × 256 experts) | 12.89 GB |
| unified memory pool | 15.6 GB |

12.89 GB cannot coexist with weights, KV and compute buffers, so LES streams
experts from NVMe and keeps a per-layer decode cache in RAM.

## 3. Measured memory (peak RSS, swap off)

| context | batch | peak RSS | decode |
|---:|---:|---:|---:|
| 8 K | 512 | 7.7 GB | 11.2 t/s |
| 8 K | 2048 | 7.7 GB | 11.6 t/s |
| 64 K | 512 | 10.3 GB | 10.8 t/s |
| **64 K** | **2048** | **10.3 GB** | **11.2–11.3 t/s** |

Two things worth remembering:

- `-b` (batch) does **not** change peak RSS: llama.cpp sizes the logits buffer
  by what is actually requested. `-b 2048` makes prefill ~50 % faster than
  `-b 512` for free.
- 64 K only costs ~2.6 GB more than 8 K because only 10 layers hold KV
  (q8_0 KV at 64 K is ~0.68 GB).

## 4. Tuning knobs that matter, and what they measured

| Knob | Effect |
|---|---|
| `LLAMA_MOE_PREWARM` (hot-expert table) | **+17 %**: 12.4 → 14.5 t/s in benchmarks. Removes the all-miss cold start. |
| `LLAMA_MOE_DECODE_WINDOW` | 32 → 128 slots/layer: 10.3 → 11.1 t/s. Beyond ~128 the gain disappears. |
| `LLAMA_MOE_IO_THREADS` | 1 → 8 threads: 10.2 → 14.5 t/s. Serialised reads are much worse. |
| `--moe-slot-bank` (prefill segment) | Raising it costs memory fast: 6400 slots → 8.0 GB peak, 3200 → 15.6 GB. Keep it small. |
| `LLAMA_MOE_WINDOW_MAX_ROWS` | Must stay 1 without speculation. See §5. |

## 5. DSpark / DFlash speculative decoding: not usable here

`dspark-Q4_K_M.gguf` (`general.architecture = dflash`, 62 tensors, 5 layers,
`target_layers=[4,12,20,28,36]`, `block_size=15`) ships **no token embedding and
no lm head**; it reads both from the target model through `cparams.ctx_other`.

Wiring `ctx_other` is necessary but not sufficient in this fork:

| Driver | State |
|---|---|
| `llama-cli` (`tools/main/main.cpp`) | **no speculation loop at all** — zero calls to `common_speculative_begin/draft/accept` |
| `llama-server` | `tools/server/server.cpp` is stale (uses `common_chat_syntax`, `speculative.n_max`, … removed in this branch); does not compile |
| `llama-speculative-simple` | runs, but measured **0.8–5.4 % acceptance** and 1.4–2.3 t/s |

Enabling `LLAMA_MOE_WINDOW_MAX_ROWS=8` so the verification batches hit the
decode cache made it *worse*: 11.4 → 5.8 t/s, because each 8-row verification
step reads ~8× the experts and the acceptance rate is too low to pay for it.

**Conclusion: leave speculation off. It is a net loss at these acceptance rates.**

## 6. System prep (measured, not guessed)

| Action | Why |
|---|---|
| `swapoff -a`, `vm.swappiness=0` | 2 GB swapfile was active with 600 MB used; a few hundred MB swapped out turns a 90 ms step into seconds |
| stop + disable `jtop` | polls GPU/CPU clocks every second, perturbs the governor and any timing |
| `jetson_clocks` | without it the CPU governor idles at 729 MHz; locked to 1984 MHz CPU / 1173 MHz GPU |
| kill stale `llama-cli` | a single orphan pins its whole expert bank (measured: 14.8 GB of a 15.6 GB pool) and silently corrupts every later run |

Background services are **not** the problem: total anonymous memory of every
process on the box is **53 MB**. The ~2.2 GB that `free` reports as used outside
processes is Jetson kernel/driver reservation and is not reclaimable.

Do **not** drop page caches before a run. The ~5.5 GB of page cache holds expert
data and speeds reads up.

## 7. Running it

```bash
cd ~/github/qwen38-kvmem/minicpm
./start.sh                          # interactive
./start.sh -p "hello" -n 256        # one-shot
./start.sh -f prompt.txt -st        # prompt from a file
```

`start.sh` sources `env.sh` (all tuned values) and runs `prep-system.sh`
(reap orphans, swap off, jtop off, clocks locked) first.

Estimated decode: **~11.2 t/s at 64 K context**, peak RSS 10.3 GB.
