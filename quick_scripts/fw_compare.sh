#!/bin/bash
# fw_compare.sh — take the same chip configuration under two firmware versions, so the VMM 4/7/9 bursts
# (LAB_COSMICS_OBSERVATIONS.md obs. 13-69) can be compared between them.
#
# Run on vmm_daplxa (dedippce185), in tmux. The firmware itself is loaded by the firmware people, between
# the two takes; this script does not touch it.
#
#   fw_compare.sh check                         bench idle? (no run, no scan, session lock FREE)
#   fw_compare.sh record  <label>               save firmware / FEC registers / link status for <label>
#   fw_compare.sh manual  <label> [min] [n]     n cycles (default 3) of warm reset + apply + <min> (default 1)
#                                               minutes of capture with the bare p2basket tools + dumpcap; holds
#                                               the session lock itself. RECOMMENDED for this test (see below).
#                                               One cycle takes ~1.5 min (warm reset + apply + acq-on/off ~20 s).
#   fw_compare.sh stack   <label> [min] [n]     the same through our DAQ stack (config_scan: QA, session lock,
#                                               RUN_QUALITY.json; min >= 2, default 2) — but the rate guard stops
#                                               any run above 100 MB/min after ~45 s, so only for a config that
#                                               does not burst
#   fw_compare.sh compare <label> [<label>...]  per cycle: the link status of all 16 FEC ports and the warm-
#                                               reset result; the burst channels with rate, ADC (median, 10-90 %)
#                                               and time between hits (fw_burst_stats.py); and the rate of each
#                                               hybrid (VMM 0 / 1), with and without its burst channels, plus
#                                               how many channels have hits — hybrid 11's link status and
#                                               vmmAppReg[15] (the flicker, obs. 67-68) on the header line
#   fw_compare.sh summary <label> [<label>...]  the short version: one table per take, one column per cycle
#                                               (warm reset, link status, volume, hot channels, trigger rate,
#                                               other VMMs). Decodes first where compare has not run yet.
#
# Two configurations (CFG=...):
#   default   the wk2 base config, with VMM 9 (H4V1) ch16-21 and VMM 4 (H2V0) ch36-41 masked: comparable to
#             all the October/November data, but mainly VMM 7 bursts
#   unmasked  the same file with those 12 mask lines commented out (created on first use, next to it in
#             config_ext/, "..._fwtest_unmasked.txt"): VMM 4 and 9 can burst too
#
# Typical session, with the HV OFF for both firmwares (the bursts happen without HV; this removes HV, gas and
# cosmics from the comparison). HV: VMM GUI "HV Control" tab, http://dedippce185.extra.cea.fr:5002/hvctl/,
# Unlock, then ALL OFF. About 4-5 min per take (3 cycles x 1 min), ~10 min per firmware:
#   fw_compare.sh check
#   fw_compare.sh manual fwA_default                  # firmware A (what is loaded now)
#   CFG=unmasked fw_compare.sh manual fwA_unmasked
#   ... firmware B is loaded (bench must be FREE: fw_compare.sh check) ...
#   fw_compare.sh manual fwB_default
#   CFG=unmasked fw_compare.sh manual fwB_unmasked
#   fw_compare.sh summary fwA_default fwB_default fwA_unmasked fwB_unmasked   # the tables
#   fw_compare.sh compare fwA_default fwB_default                             # the full detail
# A minute is plenty for the bursts (kHz-MHz). More cycles cost ~1.5 min each and give more applies to
# compare (hot channel, hybrid 11 flicker): e.g. "manual fwA_default 1 5".
#
# Self-tests on 6 Nov 2026 (FEC fw 0x20102026, hybrid fw 0x20211104, 1 cycle x 1 min): default config
# 282 MB (VMM 7 ch23 758 kHz) in the morning and 29 MB (VMM 7 ch24 66 kHz) after lunch — same file, the
# burst changes with each apply; unmasked 434 MB (VMM 9 ch19 847 kHz, VMM 4 ch36 255 kHz). Captures go to
# $OUT_ROOT (not under runs/: no QA, no prune, no EOS backup — copy them yourself if they are to be kept).
#
# Why n cycles: the burst channel changes at every reconfiguration, so one apply per firmware cannot tell a
# firmware effect from that. Each cycle is a fresh warm reset followed by an apply, in that order (applying
# then warm-resetting reverts the trigger hybrid's registers — the July offset = -16 problem).
#
# Environment overrides: CFG (default | unmasked | a config_ext file name), OUT_ROOT (output folder),
# LIMIT_HZ (compare: list channels above this rate, default 1000).

set -u -o pipefail

REPO=/local/p2/DAQ_Control_VMM_Beam
CONF_DIR=/local/p2/p2testbench/TestBenchCERN
BASE=config_base/p2b-config-cern-base.yaml
DEFAULT_CFG=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_safe_m9c16to21_m4c36to41_h0open.txt
UNMASKED_CFG=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_safe_h0open_fwtest_unmasked.txt
case "${CFG:-default}" in
    default)  CFG=$DEFAULT_CFG ;;
    unmasked) CFG=$UNMASKED_CFG ;;
esac
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BURST_STATS=$HERE/fw_burst_stats.py
P2_PY=/local/p2/p2-readout/venv/bin/python
SC=/local/p2/deploy/bin/p2basket-sc
WARM_RESET=/local/p2/p2testbench/TestBenchCosmics/p2basket_sc_warm_reset.py
APPLY=$CONF_DIR/p2basket_sc_config.py
IFACE=enp4s0f1
OUT_ROOT=${OUT_ROOT:-/local/p2/p2data/cosmics_3det_202610/fw_test}
LIMIT_HZ=${LIMIT_HZ:-1000}

say()  { echo "$(date '+%F %T') | $*"; }
die()  { say "ERROR: $*"; exit 1; }
note() { (cd "$REPO" && .venv/bin/python lab_logbook.py note "$*" >/dev/null) || say "(logbook note failed)"; }
sc()   { (cd "$CONF_DIR" && "$SC" --config-file "$BASE" "$@" </dev/null); }
lock_state() { "$SC" --session-lock </dev/null 2>&1 | tail -1; }

need_label() { [ -n "${1:-}" ] || die "missing <label> (e.g. fwA)"; [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die "label: letters, digits, . _ - only"; }

# The unmasked variant: the default config with the VMM 9 ch16-21 / VMM 4 ch36-41 mask lines commented out,
# nothing else changed. Created once; refused if the result does not differ by exactly those 12 lines.
ensure_cfg() {
    local dir=$CONF_DIR/config_ext
    [ "$CFG" = "$UNMASKED_CFG" ] || return 0
    [ -f "$dir/$UNMASKED_CFG" ] && return 0
    local tmp; tmp=$(mktemp "$dir/.fwtest.XXXXXX")
    {
        echo "# fw_compare.sh $(date +%F): $DEFAULT_CFG with the VMM 9 (Hyb 4 Vmm 1) ch16-21 and"
        echo "# VMM 4 (Hyb 2 Vmm 0) ch36-41 masks commented out; nothing else changed."
        sed -E 's/^(Sys\s+0\s+Bec\s+0\s+Hyb\s+4\s+Vmm\s+1\s+Reg\s+channel\[(1[6-9]|2[01])\]\s+Field\s+sm\s+1)/#\1/;
                s/^(Sys\s+0\s+Bec\s+0\s+Hyb\s+2\s+Vmm\s+0\s+Reg\s+channel\[(3[6-9]|4[01])\]\s+Field\s+sm\s+1)/#\1/' \
            "$dir/$DEFAULT_CFG"
    } > "$tmp"
    local changed; changed=$(diff <(sed 1,2d "$tmp") "$dir/$DEFAULT_CFG" | grep -c '^<')
    if [ "$changed" -ne 12 ] || grep -qE '^\s*Sys.*Hyb\s+(4\s+Vmm\s+1.*channel\[(1[6-9]|2[01])\]|2\s+Vmm\s+0.*channel\[(3[6-9]|4[01])\]).*sm\s+1' "$tmp"; then
        rm -f "$tmp"; die "could not build the unmasked config ($changed lines changed, expected 12)"
    fi
    chmod 660 "$tmp"; mv "$tmp" "$dir/$UNMASKED_CFG"
    say "created config_ext/$UNMASKED_CFG (12 mask lines commented out)"
}

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
    ensure_cfg
    [ -f "$CONF_DIR/config_ext/$CFG" ] && say "config: $CFG" || { say "config not found: config_ext/$CFG"; ok=1; }
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
    need_label "${1:-}"; local label=$1 min=${2:-2} n=${3:-3} out=$OUT_ROOT/$1
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
    need_label "${1:-}"; local label=$1 min=${2:-1} n=${3:-3} out=$OUT_ROOT/$1
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

# hybrid 11 link status (+ vmmAppReg[15]) from a link_status.txt, for the flicker (obs. 67-68)
link11() {
    [ -f "$1" ] || { echo "n/a"; return; }
    local ls reg
    ls=$(grep -m1 'hybrid\[11\]\.linkStatus' "$1" | awk '{print $NF}')
    reg=$(grep -m1 'vmmAppReg\[15\]\.value' "$1" | awk '{print $(NF-1)}')
    echo "hybrid 11 linkStatus=${ls:-?} vmmAppReg[15]=${reg:-?}"
}

# link status of all 16 FEC ports after the apply (4 = connected and ready; 0 = nothing; hybrids 0-9 are used)
links_all() {
    [ -f "$1" ] || { echo "   link status: n/a (no link_status.txt for this take)"; return; }
    local p v line="" bad=""
    for p in $(seq 0 15); do
        v=$(grep -m1 "hybrid\[$(printf %02d "$p")\]\.linkStatus" "$1" | awk '{print $NF}')
        line+="${v:-?}"; [ "$p" -eq 9 ] && line+=" |" ; line+=" "
        [ "$p" -le 9 ] && [ "${v:-?}" != 4 ] && bad+=" H$p=${v:-?}"
    done
    echo "   link status H0-H9 | H10-H15:  $line"
    [ -n "$bad" ] && echo "   !! hybrids not at 4 (ready):$bad"
}

cmd_summary() {
    [ $# -ge 1 ] || die "usage: fw_compare.sh summary <label> [<label>...]"
    local label take name
    for label in "$@"; do
        local out=$OUT_ROOT/$label
        [ -s "$out/runs.txt" ] || continue
        while read -r take; do
            name=$(basename "$take")
            [ -s "$out/bursts_$name.json" ] && continue
            say "decoding $label / $name ..."
            systemd-run --user --scope -q -p MemoryMax=2G "$P2_PY" -I "$BURST_STATS" \
                --limit-hz "$LIMIT_HZ" --json "$out/bursts_$name.json" "$take" > "$out/bursts_$name.txt" 2>&1 \
                || say "decode failed — see $out/bursts_$name.txt"
        done < "$out/runs.txt"
    done
    python3 "$HERE/fw_summary.py" "$OUT_ROOT" "$@"
}

cmd_compare() {
    [ $# -ge 1 ] || die "usage: fw_compare.sh compare <label> [<label>...]"
    local label take name
    for label in "$@"; do
        local out=$OUT_ROOT/$label
        [ -s "$out/runs.txt" ] || { say "no takes recorded for $label ($out/runs.txt)"; continue; }
        echo
        echo "################ $label — FEC firmware $(grep -m1 'ReadSystemRegisters.fwVersion' "$out/sys_regs.txt" 2>/dev/null | awk '{print $(NF-1)}'), hybrid firmware $(grep -m1 firmwareVersion "$out/fw_version.txt" 2>/dev/null | awk '{print $(NF-1)}')"
        echo "                 $(grep -m1 '^config:' "$out/meta.txt" 2>/dev/null | awk '{print $2}')"
        while read -r take; do
            name=$(basename "$take")
            echo "---- $label / $name   $(link11 "$take/link_status.txt")"
            links_all "$take/link_status.txt"
            grep -m1 'failed hybrids' "$take/warm_reset.log" 2>/dev/null | sed 's/^/   warm reset: /'
            systemd-run --user --scope -q -p MemoryMax=2G "$P2_PY" -I "$BURST_STATS" \
                --limit-hz "$LIMIT_HZ" --json "$out/bursts_$name.json" "$take" > "$out/bursts_$name.txt" 2>&1 \
                || echo "   (decode failed — see $out/bursts_$name.txt)"
            cat "$out/bursts_$name.txt"
        done < "$out/runs.txt"
    done
    echo
    say "per-take tables and JSON: $OUT_ROOT/<label>/bursts_*.{txt,json}"
}

case "${1:-}" in
    check)   shift; cmd_check "$@" ;;
    record)  shift; cmd_record "$@" ;;
    stack)   shift; cmd_stack "$@" ;;
    manual)  shift; cmd_manual "$@" ;;
    compare) shift; cmd_compare "$@" ;;
    summary) shift; cmd_summary "$@" ;;
    *) sed -n '2,58p' "$0"; exit 2 ;;
esac
