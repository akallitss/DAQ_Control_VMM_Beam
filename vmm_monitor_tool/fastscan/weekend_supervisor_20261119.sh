#!/bin/bash
# Weekend 19-23 Nov 2026 on the lab cosmic stand (docs/HANDOFF_WEEKEND_RUNS_20261119.md, approved by the user 19 Nov ~00:50):
# overlapping passes P1 P2 HS P3 P4 HS ... over the gain x threshold grid (D=3.0, A=4.5, B=6, C=9 mV/fC x R=50/10/2 Hz),
# pulser runs (PRa/PRb on REF, PCa/PCb on C10) and high-statistics blocks (REF, C10), 49 min per run, until Mon 07:00.
#
#   setsid nohup bash ~/trim_work/weekend_supervisor_20261119.sh >> ~/trim_work/out/weekend_20261119/supervisor.log 2>&1 < /dev/null &
#   DRY=1 bash ~/trim_work/weekend_supervisor_20261119.sh     # config_scan --dry-run for every run, no waiting
#
# STOP between runs: touch ~/trim_work/out/weekend_20261119/STOP   (the current run finishes; checked before every run)
#
# One config_scan call PER RUN (not per pass, as the handoff sketched): config_scan takes HH:MM within 24 h and only
# checks its own stop flag between configs, so per-run calls give the STOP flag, the deadline, the HV and the disk checks
# before every run. Before each run: HV check-on (wait up to 30 min, then run anyway and log it: the HV guard decides
# about HV, not this script); free disk >= 75 GB (else wait for the pruner, up to 2 h). A run that did not finish is
# retried once after 15 min; 6 failed attempts in a row -> ALERT and stop (HV left to the guard).
# runs.csv (run, short, file, pass, start, end) is what the lxplus analysis uses to name the runs.
set -u
W=~/trim_work/out/weekend_20261119
DAQ=/local/p2/DAQ_Control_VMM_Beam
T=~/trim_work/vmm_monitor_tool
CE=/local/p2/p2testbench/TestBenchCERN/config_ext
P=p2b-config-cern-ext_gain
S=_peaktime200_opt_cosmic_
DEADLINE=${DEADLINE:-"2026-11-23 07:00"}
SEQ=${SEQ:-"P1 P2 HS P3 P4 HS P1 P2 HS P3 P4 HS P1 P2"}     # longer than needed; the deadline cuts it
MIN=49
SLOT_MIN=56                                                # 49 min of data + ~6 min config_scan overhead + margin
DRY=${DRY:-0}
mkdir -p $W/cs_logs
CSV=$W/runs.csv
[ -f $CSV ] || echo "run,short,file,pass,start,end" > $CSV
say() { echo "[$(date '+%F %T')] $*"; }
note() { [ "$DRY" = 1 ] && return; (cd $DAQ && .venv/bin/python lab_logbook.py note "$*" > /dev/null 2>&1) || say "logbook note failed"; }

declare -A F=(
  [REF]=${P}4.5${S}trimmed_Km18_v1_hvm.txt
  [A50]=${P}4.5${S}bscan_hvon50_v1_hvm.txt [A10]=${P}4.5${S}bscan_hvon10_v1_hvm.txt [A2]=${P}4.5${S}bscan_hvon2_v1_hvm.txt
  [B50]=${P}6.0${S}gscan_hvon50_hvm.txt    [B10]=${P}6.0${S}gscan_hvon10_hvm.txt    [B2]=${P}6.0${S}gscan_hvon2_hvm.txt
  [C50]=${P}9.0${S}gscan_hvon50_hvm.txt    [C10]=${P}9.0${S}gscan_hvon10_hvm.txt    [C2]=${P}9.0${S}gscan_hvon2_hvm.txt
  [D50]=${P}3.0${S}gscan_hvon50_hvm.txt    [D10]=${P}3.0${S}gscan_hvon10_hvm.txt    [D2]=${P}3.0${S}gscan_hvon2_hvm.txt
  [PRa]=${P}4.5${S}trimmed_Km18_v1_hvm_tpA.txt [PRb]=${P}4.5${S}trimmed_Km18_v1_hvm_tpB.txt
  [PCa]=${P}9.0${S}gscan_hvon10_hvm_tpA.txt    [PCb]=${P}9.0${S}gscan_hvon10_hvm_tpB.txt
)
declare -A ORDER=(
  [P1]="REF PRa D50 D10 D2 A50 A10 A2 B50 B10 B2 C50 C10 C2 PCa"
  [P2]="REF PCb C2 C10 C50 B2 B10 B50 A2 A10 A50 D2 D10 D50 PRb"
  [HS]="REF C10 REF C10 REF C10"
  [P3]="REF D50 A50 B50 C50 PRa D10 A10 B10 C10 PCa D2 A2 B2 C2"
  [P4]="REF C2 B2 A2 D2 PCb C10 B10 A10 D10 PRb C50 B50 A50 D50"
)
END_S=$(date -d "$DEADLINE" +%s)

hv_ok() {
    [ "$DRY" = 1 ] && return 0
    local i
    for i in $(seq 1 60); do
        python3 $T/hv_lab.py check-on > /dev/null 2>&1 && return 0
        [ $i = 1 ] && say "HV not at the operating point: $(python3 $T/hv_lab.py check-on 2>&1 | tail -1); waiting up to 30 min"
        [ -f $W/STOP ] && return 0
        sleep 30
    done
    say "HV still not at the operating point after 30 min - running anyway (the guard decides about HV): $(python3 $T/hv_lab.py state 2>&1 | tr '\n' ' ' | cut -c1-600)"
}

disk_ok() {
    [ "$DRY" = 1 ] && return 0
    local i g
    for i in $(seq 1 24); do
        g=$(df -BG --output=avail /local/p2/p2data | tail -1 | tr -dc 0-9)
        [ "$g" -ge 75 ] && return 0
        [ $i = 1 ] && say "only ${g} GB free (< 75): waiting for the pruner, up to 2 h"
        sleep 300
    done
    say "still only ${g} GB free after 2 h - trying anyway (config_scan refuses below 60 GB)"
}

# one run of config file $2 (short $1), pass label $3; returns 0 if config_scan logged "<run> finished"
one_run() {
    local short=$1 file=$2 pass=$3 try=$4 now until_s hhmm log run t0 t1 x
    now=$(date +%s)
    until_s=$(( now + (MIN + 6 + 12) * 60 ))
    hhmm=$(date -d @$until_s +%H:%M)
    log=$W/cs_logs/$(date +%Y%m%d_%H%M)_${pass}_${short}_t${try}.log
    x=""; [ "$DRY" = 1 ] && x="--dry-run"
    say "run $short ($pass, try $try): $file, until $hhmm"
    (cd $DAQ && .venv/bin/python config_scan.py --until $hhmm --minutes $MIN --configs $file $x) > $log 2>&1
    [ "$DRY" = 1 ] && { grep -q "DRY RUN" $log && say "  dry run ok" || { say "  dry run FAILED:"; tail -5 $log; }; return 0; }
    run=$(grep -o "started run_[0-9]*" $log | tail -1 | cut -d' ' -f2)
    t0=$(grep "started run_" $log | tail -1 | cut -c1-19)
    if [ -n "$run" ] && grep -q "$run finished" $log; then
        t1=$(grep "$run finished" $log | tail -1 | cut -c1-19)
        echo "$run,$short,$file,$pass,$t0,$t1" >> $CSV
        say "  $(grep "$run finished" $log | tail -1 | cut -d'|' -f2-)"
        return 0
    fi
    say "  NOT finished (${run:-no run started}): $(grep -v '^\s*$' $log | tail -3 | tr '\n' ' ' | cut -c1-500)"
    [ -n "$run" ] && echo "$run,$short,$file,$pass,$t0,FAILED" >> $CSV
    return 1
}

say "weekend supervisor start (pid $$, DRY=$DRY), deadline $DEADLINE, sequence $SEQ"
fails=0
declare -A K=()
for pass in $SEQ; do
    K[$pass]=$(( ${K[$pass]:-0} + 1 ))
    label="${pass}_${K[$pass]}"
    list=""
    for s in ${ORDER[$pass]}; do
        if [ -f "$CE/${F[$s]}" ]; then list="$list $s"; else say "$label: $s missing (${F[$s]}), skipped"; fi
    done
    say "pass $label:$list"
    note "weekend pass $label: $list (49 min per run; files in fastscan/weekend_supervisor_20261119.sh)"
    for s in $list; do
        if [ -f $W/STOP ]; then say "STOP flag: supervisor ends"; note "weekend runs stopped by the STOP flag"; exit 0; fi
        if [ "$DRY" != 1 ] && [ $(( END_S - $(date +%s) )) -lt $(( SLOT_MIN * 60 )) ]; then
            say "deadline $DEADLINE: no time for another run"; note "weekend runs: deadline reached"; say "ALLDONE weekend"; exit 0
        fi
        hv_ok; disk_ok
        if ! one_run $s "${F[$s]}" $label 1; then
            fails=$((fails + 1))
            if [ -f $W/STOP ]; then continue; fi
            say "  waiting 15 min, then one retry of $s"
            [ "$DRY" = 1 ] || sleep 900
            if [ "$DRY" = 1 ] || [ $(( END_S - $(date +%s) )) -ge $(( SLOT_MIN * 60 )) ]; then
                hv_ok; disk_ok
                if one_run $s "${F[$s]}" $label 2; then fails=0; else fails=$((fails + 1)); fi
            fi
        else
            fails=0
        fi
        if [ $fails -ge 6 ]; then
            say "ALERT: $fails failed attempts in a row - supervisor stops (HV left to the guard)"
            echo "ALERT $(date -Iseconds): $fails failed runs in a row, see supervisor.log" > $W/ALERT
            note "weekend runs: ALERT, $fails failed attempts in a row, supervisor stopped"
            exit 1
        fi
    done
done
say "sequence done"; note "weekend runs: sequence done"; say "ALLDONE weekend"
