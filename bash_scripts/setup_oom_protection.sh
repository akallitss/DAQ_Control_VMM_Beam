#!/bin/bash
# One-time ROOT setup of the kernel-level memory backstop for a small-RAM DAQ box.
#
#   Run ONCE per machine:   sudo bash_scripts/setup_oom_protection.sh
#
# This is the second layer beneath mem_guardian.py (which kills a runaway QA job
# but is DAQ-aware and userspace). Here we add:
#
#   1. earlyoom — a tiny daemon that kills the largest process when free RAM gets
#      critically low, BEFORE the kernel's slow OOM killer lets the machine freeze.
#      Tuned RAM-driven (-s 100) because this box has a huge swapfile: the default
#      swap-gated trigger would only fire after ~14 GB is swapped, long after the
#      machine has thrashed to a halt. --avoid protects the processes it CAN name
#      (system-critical + dumpcap/flask/tmux). Our Python DAQ vs QA look identical
#      to earlyoom (both 'python3'), so the QA marks itself as the preferred victim
#      via oom_score_adj (see qa_watcher.py) — earlyoom then picks QA over the DAQ.
#
#   2. vm.swappiness = 10 — stop the kernel from thrashing the working set into the
#      16 GB swap under pressure (the actual cause of the "machine goes unresponsive"
#      behaviour on this 8 GB box). Memory pressure then surfaces as RAM pressure
#      that earlyoom/OOM can act on quickly.
#
# Idempotent: safe to re-run. systemd-oomd is left as-is (it exists but its default
# 90%-swap trigger is ineffective with a 16 GB swapfile).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root:  sudo bash_scripts/setup_oom_protection.sh" >&2
    exit 1
fi

echo "== 1/3  install earlyoom =="
if ! command -v earlyoom >/dev/null 2>&1; then
    apt-get install -y earlyoom || { echo "apt install failed; trying apt-get update first"; apt-get update && apt-get install -y earlyoom; }
else
    echo "earlyoom already installed ($(earlyoom --help 2>&1 | head -1))"
fi

echo "== 2/3  configure + enable earlyoom =="
cat > /etc/default/earlyoom <<'EOF'
# earlyoom for the VMM DAQ box — see bash_scripts/setup_oom_protection.sh.
# -r 3600  : hourly memory report (low log noise)
# -m 10    : SIGTERM the biggest process when available RAM < 10% (SIGKILL < 5%)
# -s 100   : do NOT gate on swap (huge swapfile) — act on RAM pressure
# --avoid  : never target these system-critical / DAQ-visible names. The Python
#            DAQ is protected instead by the QA raising its own oom_score_adj.
EARLYOOM_ARGS="-r 3600 -m 10 -s 100 --avoid '^(systemd|sshd|dbus-daemon|Xorg|gnome-shell|dumpcap|flask|tmux: server)$'"
EOF
systemctl enable earlyoom >/dev/null 2>&1 || true
systemctl restart earlyoom
sleep 1
systemctl is-active earlyoom >/dev/null && echo "earlyoom active" || { echo "earlyoom failed to start"; systemctl status earlyoom --no-pager | head; }

echo "== 3/3  lower swappiness (reduce thrash into the 16 GB swap) =="
cat > /etc/sysctl.d/99-vmm-lowswap.conf <<'EOF'
# Small-RAM DAQ box: keep the working set in RAM instead of thrashing to swap.
vm.swappiness = 10
EOF
sysctl -p /etc/sysctl.d/99-vmm-lowswap.conf

echo
echo "== done =="
echo "earlyoom : $(systemctl is-active earlyoom)  ($(grep EARLYOOM_ARGS /etc/default/earlyoom))"
echo "swappiness: $(cat /proc/sys/vm/swappiness)"
