#!/bin/bash
# fw_compare.sh — take the same chip configuration under two firmware versions, so the VMM 4/7/9 bursts
# (LAB_COSMICS_OBSERVATIONS.md obs. 13-69) can be compared between them.
#
# Run on vmm_daplxa (dedippce185), in tmux. The firmware itself is loaded by the firmware people, between
# the two takes; this script does not touch it.
#
#   fw_compare.sh check                         bench idle? (no run, no scan, session lock FREE)
#   fw_compare.sh record  <label>               save firmware / FEC registers / link status for <label>
#   fw_compare.sh manual  <label> [min] [n]     n cycles (default 3) of warm reset + apply + <min> (default 5)
#                                               minutes of capture with the bare p2basket tools + dumpcap; holds
#                                               the session lock itself. RECOMMENDED for this test (see below).
#   fw_compare.sh stack   <label> [min] [n]     the same through our DAQ stack (config_scan: QA, session lock,
#                                               RUN_QUALITY.json) — but the rate guard stops any run above
#                                               100 MB/min after ~45 s, so only for a config that does not burst
#   fw_compare.sh compare <label> [<label>...]  per-channel rates of every take (burst channels and their rates)
#
# Typical session (about 20 min per firmware):
#   fw_compare.sh check
#   fw_compare.sh manual fwA           # firmware A (what is loaded now)
#   ... firmware B is loaded (bench must be FREE: fw_compare.sh check) ...
#   fw_compare.sh manual fwB
#   fw_compare.sh compare fwA fwB
#
# Self-test on 6 Nov 2026 (FEC fw 0x20102026, hybrid fw 0x20211104, default config, 1 cycle x 1 min):
# 282 MB/min, VMM 7 ch23 at 758 kHz and ch24 at 38 kHz — the burst is there, so the default config shows
# the effect; expect ~1.4 GB per 5-min cycle. Captures go to $OUT_ROOT (not under runs/: no QA, no prune,
# no EOS backup — copy them yourself if they are to be kept).
#
# Why n cycles: the burst channel changes at every reconfiguration, so one apply per firmware cannot tell a
# firmware effect from that. Each cycle is a fresh warm reset followed by an apply, in that order (applying
# then warm-resetting reverts the trigger hybrid's registers — the July offset = -16 problem).
#
# Environment overrides: CFG (config_ext file name), OUT_ROOT (output folder), LIMIT_HZ (compare: list
# channels above this rate, default 1000).

set -u -o pipefail

REPO=/local/p2/DAQ_Control_VMM_Beam
CONF_DIR=/local/p2/p2testbench/TestBenchCERN
BASE=config_base/p2b-config-cern-base.yaml
CFG=${CFG:-p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_safe_m9c16to21_m4c36to41_h0open.txt}
SC=/local/p2/deploy/bin/p2basket-sc
WARM_RESET=/local/p2/p2testbench/TestBenchCosmics/p2basket_sc_warm_reset.py
APPLY=$CONF_DIR/p2basket_sc_config.py
IFACE=enp4s0f1
OUT_ROOT=${OUT_ROOT:-/local/p2/p2data/cosmics_3det_202610/fw_test}
LIMIT_HZ=${LIMIT_HZ:-1000}
ADAPT=$REPO/logs/adaptive/adaptive_thr_scan.py

say()  { echo "$(date '+%F %T') | $*"; }
die()  { say "ERROR: $*"; exit 1; }
note() { (cd "$REPO" && .venv/bin/python lab_logbook.py note "$*" >/dev/null) || say "(logbook note failed)"; }
sc()   { (cd "$CONF_DIR" && "$SC" --config-file "$BASE" "$@" </dev/null); }
lock_state() { "$SC" --session-lock </dev/null 2>&1 | tail -1; }

need_label() { [ -n "${1:-}" ] || die "missing <label> (e.g. fwA)"; [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die "label: letters, digits, . _ - only"; }

cmd_check() {
    local ok=0 st
    st=$(curl -s -m 5 localhost:5002/live/status | grep -o '"run_active":[a-z]*')
    case "$st" in
        *false) say "no run active" ;;
        *true)  say "A RUN IS ACTIVE — stop it first"; ok=1 ;;
        *)      say "warning: DAQ GUI not answering on :5002 (fine for 'manual', not for 'stack')" ;;
    esac
    if pgrep -f "config_scan.py|adaptive_thr_scan.py --tag|run_2026.*\.sh" >/dev/null; then
        say "A SCAN IS RUNNING:"; pgrep -af "config_scan.py|adaptive_thr_scan.py --tag|run_2026.*\.sh" | cut -c1-150; ok=1
    else
        say "no scan running"
    fi
    st=$(lock_state); say "session lock: $st"
    [[ "$st" == *FREE* ]] || ok=1
    [ -f "$CONF_DIR/config_ext/$CFG" ] || { say "config not found: config_ext/$CFG"; ok=1; }
    [ $ok -eq 0 ] && say "bench idle — OK" || say "bench NOT ready"
    return $ok
}

cmd_record() {
    need_label "${1:-}"; local out=$OUT_ROOT/$1; mkdir -p "$out"
    {
        echo "label: $1"; echo "date: $(date '+%F %T %Z')"; echo "host: $(hostname)  user: $(id -un)"
        echo "p2basket-sc: $("$SC" --version </dev/null 2>&1 | head -1)  ($(readlink -f /local/p2/deploy/bin))"
        echo "config: config_ext/$CFG  md5 $(md5sum "$CONF_DIR/config_ext/$CFG" | cut -d' ' -f1)"
        echo "base:   $BASE  md5 $(md5sum "$CONF_DIR/$BASE" | cut -d' ' -f1)"
        echo "daq code: $(git -C "$REPO" log -1 --format='%h %s' 2>/dev/null)"
    } > "$out/meta.txt"
    sc --read-fw-version  > "$out/fw_version.txt"  2>&1 || say "read-fw-version failed (see $out/fw_version.txt)"
    sc --read-sys-regs    > "$out/sys_regs.txt"    2>&1 || say "read-sys-regs failed"
    sc --read-link-status > "$out/link_status.txt" 2>&1 || say "read-link-status failed"
    say "recorded $1 in $out"
    grep -m1 'ReadSystemRegisters.fwVersion' "$out/sys_regs.txt" | awk '{print "    FEC firmware " $(NF-1)}'
    grep firmwareVersion "$out/fw_version.txt" | awk '{print $(NF-1)}' | sort | uniq -c | awk '{print "    " $1 " hybrids with firmware " $2}'
}

cmd_stack() {
    need_label "${1:-}"; local label=$1 min=${2:-5} n=${3:-3} out=$OUT_ROOT/$1
    [ -e "$out/runs.txt" ] && die "$out/runs.txt exists — pick a new label, or remove it to retake"
    cmd_check || die "bench not ready"
    say "note: the rate guard stops runs above 100 MB/min after ~45 s; for a bursting config use 'manual'"
    cmd_record "$label"
    local cfgs="" i; for i in $(seq 1 "$n"); do cfgs+="${cfgs:+,}$CFG"; done
    local until; until=$(date -d "+$(( n * (min + 4) + 10 )) min" +%H:%M)
    [ "$(date -d "+$(( n * (min + 4) + 10 )) min" +%F)" = "$(date +%F)" ] || die "would run past midnight; start after 00:00"
    note "FW test $label: $n cycles x $min min of $CFG through config_scan (firmware: see $out/fw_version.txt)."
    say "config_scan: $n x $min min, until $until — log in $out/config_scan.out"
    (cd "$REPO" && .venv/bin/python config_scan.py --until "$until" --minutes "$min" --configs "$cfgs") 2>&1 \
        | tee "$out/config_scan.out"
    grep -oE 'started run_[0-9]+' "$out/config_scan.out" | awk '{print $2}' > "$out/runs.txt"
    say "runs for $label: $(tr '\n' ' ' < "$out/runs.txt")"
    note "FW test $label done: runs $(tr '\n' ' ' < "$out/runs.txt")"
    [ "$(wc -l < "$out/runs.txt")" -eq "$n" ] || say "warning: expected $n runs — check $out/config_scan.out"
}

LOCK_PID=""; ACQ_ON=0
manual_cleanup() {
    [ $ACQ_ON -eq 1 ] && { say "acq-off (cleanup)"; sc --acq-off >/dev/null 2>&1; ACQ_ON=0; }
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        kill -INT "$LOCK_PID"; wait "$LOCK_PID" 2>/dev/null; say "session lock released"
    fi
    LOCK_PID=""
}

cmd_manual() {
    need_label "${1:-}"; local label=$1 min=${2:-5} n=${3:-3} out=$OUT_ROOT/$1
    [ -e "$out/runs.txt" ] && die "$out/runs.txt exists — pick a new label, or remove it to retake"
    cmd_check || die "bench not ready"
    command -v dumpcap >/dev/null || die "dumpcap not found"
    cmd_record "$label"

    local lock_min=$(( n * (min + 3) + 10 ))
    trap 'manual_cleanup; exit 130' INT TERM
    trap manual_cleanup EXIT
    "$SC" --session-lock "$lock_min" > "$out/session_lock.out" 2>&1 </dev/null &
    LOCK_PID=$!
    local t; for t in $(seq 1 15); do grep -q "SUCCESSFULLY LOCKED" "$out/session_lock.out" && break; sleep 1; done
    grep -q "SUCCESSFULLY LOCKED" "$out/session_lock.out" || die "could not lock the session: $(tr '\r' '\n' < "$out/session_lock.out" | tail -2)"
    say "session lock held for up to $lock_min min (released at the end or on Ctrl-C)"
    note "FW test $label (manual): $n cycles x $min min of $CFG, captures in $out (firmware: see $out/fw_version.txt)."

    : > "$out/runs.txt.tmp"
    local i rc
    for i in $(seq 1 "$n"); do
        local cyc=$out/cycle$i raw=$out/cycle$i/manual/raw_daq_data   # layout readable by adaptive_thr_scan --replay
        mkdir -p "$raw"
        say "=== cycle $i/$n ==="
        (cd "$CONF_DIR" && "$WARM_RESET" -c "$CONF_DIR" -b "$BASE" -W -w 3 </dev/null) > "$cyc/warm_reset.log" 2>&1
        rc=$?; say "warm reset: $rc failed hybrids"
        [ $rc -eq 0 ] || die "warm reset failed (cycle $i) — see $cyc/warm_reset.log"
        (cd "$CONF_DIR" && "$APPLY" -c "$CONF_DIR" -b "$BASE" -e "config_ext/$CFG" </dev/null) > "$cyc/apply.log" 2>&1
        rc=$?; say "apply: rc=$rc"
        [ $rc -eq 0 ] || die "apply failed (cycle $i) — see $cyc/apply.log"
        sc --read-link-status > "$cyc/link_status.txt" 2>&1
        sc --acq-on > "$cyc/acq_on.log" 2>&1 || die "acq-on failed (cycle $i) — see $cyc/acq_on.log"
        ACQ_ON=1; say "acq-on; capturing $min min on $IFACE"
        dumpcap -i "$IFACE" -q -g -b duration:120 -a "duration:$(( min * 60 ))" \
            -w "$raw/$IFACE.pcapng" > "$cyc/dumpcap.log" 2>&1 || say "dumpcap returned non-zero — see $cyc/dumpcap.log"
        sc --acq-off > "$cyc/acq_off.log" 2>&1 || say "acq-off returned non-zero — see $cyc/acq_off.log"
        ACQ_ON=0
        say "cycle $i: $(ls "$raw" | wc -l) files, $(du -sh "$raw" | cut -f1)"
        echo "$cyc" >> "$out/runs.txt.tmp"
    done
    mv "$out/runs.txt.tmp" "$out/runs.txt"
    manual_cleanup
    note "FW test $label (manual) done: $n cycles in $out"
    say "done — next: fw_compare.sh compare <labelA> <labelB>"
}

cmd_compare() {
    [ $# -ge 1 ] || die "usage: fw_compare.sh compare <label> [<label>...]"
    local label take name
    for label in "$@"; do
        local out=$OUT_ROOT/$label
        [ -s "$out/runs.txt" ] || { say "no takes recorded for $label ($out/runs.txt)"; continue; }
        echo; echo "################ $label — FEC firmware $(grep -m1 'ReadSystemRegisters.fwVersion' "$out/sys_regs.txt" 2>/dev/null | awk '{print $(NF-1)}'), hybrid firmware $(grep -m1 firmwareVersion "$out/fw_version.txt" 2>/dev/null | awk '{print $(NF-1)}')"
        while read -r take; do
            name=$(basename "$take")
            echo "---- $label / $name   (channels over $LIMIT_HZ Hz)"
            (cd "$REPO" && python3 "$ADAPT" --tag fwcompare --replay "$take" --ch-limit-hz "$LIMIT_HZ") \
                > "$out/replay_$name.txt" 2>&1 || echo "   (decode failed — see $out/replay_$name.txt)"
            grep -vE '^\s*VMM +[0-9]+:.* -$' "$out/replay_$name.txt"
            local rq; rq=$(find "/local/p2/p2data/cosmics_3det_202610/runs/$take" -name RUN_QUALITY.json 2>/dev/null | head -1)
            [ -n "$rq" ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('   hits total', d.get('hits_total'), '| MB', round(d.get('capture_bytes',0)/1e6))" "$rq"
        done < "$out/runs.txt"
    done
    echo; say "full outputs: $OUT_ROOT/<label>/replay_*.txt"
}

case "${1:-}" in
    check)   shift; cmd_check "$@" ;;
    record)  shift; cmd_record "$@" ;;
    stack)   shift; cmd_stack "$@" ;;
    manual)  shift; cmd_manual "$@" ;;
    compare) shift; cmd_compare "$@" ;;
    *) sed -n '2,30p' "$0"; exit 2 ;;
esac
