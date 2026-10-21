# 环境与硬件事实（nx5）

采集于 2026-10-19。

## 硬件

| 项目 | 值 |
|---|---|
| 板卡 | NVIDIA Jetson Orin NX Engineering Reference Developer Kit **Super**（p3768 + p3767-0000-super） |
| SoC | tegra234 |
| CPU | 8× Cortex-A78AE @ 1984 MHz |
| GPU | Orin，compute capability **8.7**，8 TPC，1024 CUDA cores，1173 MHz（jetson_clocks 锁定） |
| 内存 | 15 597 MiB 统一内存（LPDDR5，与 GPU 共享） |
| 显存（nvidia-smi 视角） | 15 597 MiB（Unified） |
| EMC | 3199 MHz（locked） |
| 磁盘 | /dev/nvme0n1p1 915G，可用 778G |
| 功耗模式 | MAXN_SUPER |
| 实测显存带宽 | **93–95 GB/s**（自写只读 kernel，见 docs/03-benchmarks.md） |

## 软件

| 项目 | 值 |
|---|---|
| OS | Ubuntu 24.04.4 LTS (noble), aarch64 |
| L4T / JetPack | R39.2.1（nvidia-l4t-core 39.2.1-20260806224157） |
| Kernel | 6.8.12-1021-tegra |
| NVIDIA Driver | 595.78 |
| CUDA | 13.2（/usr/local/cuda-13.2） |
| gcc | 13.3.0 |
| cmake / ninja | 3.28.3 / Ninja |
| Python | 3.12 |

## 网络

- 国内直连 ModelScope（`--noproxy '*' `）实测 **24–26 MB/s**，11.3 GiB 模型约 7.5 分钟下载完成。
- HuggingFace 需走 xray 代理（`http://127.0.0.1:10808`），直连超时。
- 因此模型全部走 ModelScope，模型仓库：`ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF`。

## 本机原有服务（已停）

| unit | 用途 | 状态 |
|---|---|---|
| siyuan-image-comfy.service | ComfyUI (:8188) | 已停，仍 enabled |
| siyuan-text-ingress.service | 文本入口 (:18086) | 已停，仍 enabled |
| siyuan-text-compat-bridge.service | 兼容桥 (:18087) | 已停，仍 enabled |
| siyuan-fleet-control.service | 机群控制 | 已停，仍 enabled |
| role-switch.service | comfy/llm 角色切换 | 已停，仍 enabled |
| gdm.service | 图形登录 | **已 disable 并停止**，系统默认 target 改为 multi-user.target |

恢复命令：

```bash
# 恢复图形模式
sudo systemctl enable --now gdm.service
sudo systemctl set-default graphical.target

# 恢复业务服务
systemctl --user start siyuan-image-comfy.service siyuan-text-ingress.service \
  siyuan-text-compat-bridge.service siyuan-fleet-control.service role-switch.service
```

保留运行：tailscaled、ssh、codex-proxy（xray 代理，下载/软件安装需要）。
