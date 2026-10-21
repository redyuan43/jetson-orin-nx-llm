#!/usr/bin/env bash
# One-shot system prep for on-device inference.
# Idempotent; safe to run at boot and again before a long run.
set -uo pipefail

say() { printf '  %s\n' "$*"; }

# 0) Reap stale inference processes. A single orphaned llama-cli pins its whole
#    expert bank (up to ~15 GB of this 15.6 GB pool); leaving one behind makes
#    every later run OOM and silently corrupts timing measurements.
stale=$(pgrep -x llama-cli 2>/dev/null || true)
if [ -n "${stale:-}" ]; then
    say "reaping stale llama-cli: $(echo "$stale" | tr '\n' ' ')"
    pkill -9 -x llama-cli 2>/dev/null || true
    sleep 2
fi
stale=$(pgrep -x llama-speculative-simple 2>/dev/null || true)
[ -n "${stale:-}" ] && pkill -9 -x llama-speculative-simple 2>/dev/null || true
stale=$(pgrep -x llama-omni-server 2>/dev/null || true)
[ -n "${stale:-}" ] && pkill -9 -x llama-omni-server 2>/dev/null || true

# 1) Disable swap. The model's working set is ~10 GB of a 15.6 GB unified pool;
#    even a few hundred MB swapped out turns a 90 ms decode step into seconds.
if [ "$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)" != "0" ]; then
    say "disabling swap"
    sudo swapoff -a || say "  swapoff failed"
else
    say "swap already off"
fi
sudo sysctl -q -w vm.swappiness=0

# 2) Stop jtop. It polls GPU/CPU clocks every second, which perturbs both the
#    clock governor and any timing measurement.
if systemctl is-active --quiet jtop.service 2>/dev/null; then
    say "stopping jtop"
    sudo systemctl stop jtop.service
    sudo systemctl disable jtop.service >/dev/null 2>&1 || true
else
    say "jtop not running"
fi

# 3) Lock clocks. Without this the CPU governor drops to 729 MHz when idle and
#    the first tokens of every request stall. 1984 MHz CPU / 1173 MHz GPU are
#    the Orin NX Super ceilings.
say "locking clocks"
sudo jetson_clocks || say "  jetson_clocks failed"

say "cpu0  = $(awk '{printf "%d MHz", $1/1000}' /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)"
gpu=$(cat /sys/devices/platform/gpu.0/devfreq/*/cur_freq 2>/dev/null | head -1)
[ -n "${gpu:-}" ] && say "gpu   = $((gpu/1000000)) MHz"
say "avail = $(( $(awk '/MemAvailable/{print $2}' /proc/meminfo) / 1024 )) MB"
say "swap  = $(awk '/^SwapTotal:/{print $2/1024 " MB"}' /proc/meminfo)"
