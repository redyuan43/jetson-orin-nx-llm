# 服务化运行

## 手动启停

```bash
cd ~/github/qwen38-kvmem
./scripts/start.sh      # 后台启动，pid 写入 logs/server.pid
./scripts/stop.sh       # 停止
```

启动约 **14 秒**加载 11.29 GiB 权重，随后监听 `http://127.0.0.1:8400`。

## 健康检查

```bash
curl -s --noproxy '*' http://127.0.0.1:8400/health          # {"status":"ok"}
curl -s --noproxy '*' http://127.0.0.1:8400/v1/models
curl -s --noproxy '*' http://127.0.0.1:8400/props           # 含 kvmem 预算状态
```

## API 用法

OpenAI 兼容，模型名取 `/v1/models` 返回值。

```bash
curl -s --noproxy '*' http://127.0.0.1:8400/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{
    "model": "Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf",
    "messages": [{"role": "user", "content": "用一句话说明什么是 KV cache"}],
    "max_tokens": 128, "stream": true
  }'
```

端点：`/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/props`、`/slots`、`/health`。

## 关键参数含义

| 参数 | 本站取值 | 含义 |
|---|---|---|
| `-c` | 131072 | 逻辑上下文上限（API 层校验用） |
| `--kvmem-budget B` | 16384 | **每条请求**在 GPU 上保留的历史 token 数 |
| `--kvmem-gen-reserve R` | 4096 | 生成阶段的追加工作空间；当前实现下输出上限也受 R 约束 |
| `--kvmem-host-mib` | 2048 | KVMem 管理的主机 KV 内存硬配额（MiB） |
| `--kvmem-sink-tokens` | 2048 | B 内必须常驻的前缀（system prompt 等） |
| `--kvmem-recent-tokens` | 1024 | B 内必须常驻的最近后缀 |
| `--kvmem-disk-mib` | 0 | SSD 分层，已关闭（实测拖慢首 token，收益低） |
| `--kv-dtype` | q4_0 | GPU KV 类型（需 `GGML_CUDA_FA_QUANTS=all` 编译） |
| `-fa on` | — | FlashAttention，q4_0 KV 必需 |
| `--spec-type none` | — | MTP 关闭（按需求） |

> `--kvmem-gen-reserve` 同时限制单次输出长度。若需要更长输出，同步调大 R
> （会多占一点 GPU 显存）。

## 系统服务（可选）

如需开机自启，创建 user unit：

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/qwen38-kvmem.service <<'EOF'
[Unit]
Description=Qwen3.8-27B + KVMem llama.cpp server
After=network-online.target

[Service]
Type=simple
ExecStart=%h/github/qwen38-kvmem/scripts/start.sh-foreground
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
```

注意 `start.sh` 是后台脚本，做 unit 时需要一个前台执行版本（去掉末尾的 `&`）。

## 内存水位（运行时）

| 项 | 占用 |
|---|---:|
| 进程 RSS | 约 13.5 GB |
| 其中 GPU 权重（CUDA0） | 10.83 GiB |
| 其中主机权重（CUDA_Host） | 388 MiB |
| KVMem 主机 KV | ≤ 2 GiB（配额） |
| 系统剩余可用 | 约 1.9–2.3 GB |

**不要再在这台机器上跑第二个大模型**，余量只够 SSH / tailscale / 代理。
