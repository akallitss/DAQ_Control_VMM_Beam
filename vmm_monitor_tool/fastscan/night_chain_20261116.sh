#!/bin/bash
# Overnight chain 16/17 Nov 2026 (run in tmux on the VMM box):
#   1. threshold-above-baseline scan, HV off (baseline_scan.py), equalised file + map of the pilot
#   2. HV on ONLY if ~/trim_work/out/hv_lab/HV_GO exists (the user confirmed the gas):
#        slow ramp to the operating point (hv_lab.py ramp-up), guard until END_AT (own tmux window),
#        the same scan with HV on, then cosmics runs alternating rt3 / trimmed_Km18_v1 (config_scan)
#      without HV_GO by the time step 1 ends: a second HV-off pass (reproducibility), HV stays off.
# Stop the chain: touch ~/trim_work/out/bscan_20261116/STOP (checked between steps).
set -u
cd ~/trim_work
D=out/bscan_20261116
T=vmm_monitor_tool
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
EQ=${P}bscan_eq_x15.txt
MAPEQ=$D/pilot/map_eq.json
END_AT=${END_AT:-2026-11-17T08:00}
GUARD_END=${GUARD_END:-leave}
ON_TRIP=${ON_TRIP:-detector-off}
HVGO=out/hv_lab/HV_GO
say() { echo "[$(date '+%F %T')] $*"; }
stopped() { [ -e $D/STOP ] && { say "STOP file: chain ends"; exit 0; }; }
status_ok() { grep -q "ALLDONE status: ok" "$1"; }

say "chain start; END_AT $END_AT guard-end $GUARD_END on-trip $ON_TRIP"
stopped
python3 $T/fastscan/baseline_scan.py --out-dir $D/hvoff --hv off --eq-file $EQ --map-eq $MAPEQ \
    > $D/hvoff.stdout 2>&1
say "HV-off scan: $(grep ALLDONE $D/hvoff/scan.log | tail -1)"
status_ok $D/hvoff/scan.log || { say "HV-off scan not ok: chain stops (no HV)"; exit 1; }
stopped

if [ ! -e $HVGO ]; then
    say "no $HVGO: second HV-off pass instead of HV on"
    python3 $T/fastscan/baseline_scan.py --out-dir $D/hvoff2 --hv off --eq-file $EQ --map-eq $MAPEQ \
        --repeat "" > $D/hvoff2.stdout 2>&1
    say "HV-off pass 2: $(grep ALLDONE $D/hvoff2/scan.log | tail -1)"
    exit 0
fi

say "HV_GO present ($(cat $HVGO)): ramping up"
rm -f out/hv_lab/GIVEN_UP.json
python3 $T/hv_lab.py ramp-up --rup 5 --hold 120 --i-max 1.0 || { say "ramp-up failed (no detector left, HV ramped down): chain ends"; exit 1; }
tmux new-window -d -n hvguard "cd ~/trim_work; python3 $T/hv_lab.py guard --until $END_AT --on-trip $ON_TRIP --end $GUARD_END 2>&1 | tee -a out/hv_lab/guard.stdout"
say "guard started (window hvguard)"
stopped

python3 $T/fastscan/baseline_scan.py --out-dir $D/hvon --hv on --eq-file $EQ --map-eq $MAPEQ \
    --restore ${P}target50_p2_rt3.txt > $D/hvon.stdout 2>&1
say "HV-on scan: $(grep ALLDONE $D/hvon/scan.log | tail -1)"
stopped
python3 $T/hv_lab.py check-on || say "WARNING: HV not all at target before the cosmics runs"

# cosmics: 30 min runs alternating, none started unless it can end by 07:55 (config_scan --until)
CFGS=$(for i in 1 2 3 4 5 6; do printf "%starget50_p2_rt3.txt,%strimmed_Km18_v1.txt," $P $P; done)
CFGS=${CFGS%,}
cd /local/p2/DAQ_Control_VMM_Beam
.venv/bin/python lab_logbook.py note "Overnight 17 Nov: cosmics runs, HV at the operating point (420/720, 430/730, 430/730), 30 min each, alternating target50_p2_rt3 / trimmed_Km18_v1 (after the HV-on threshold-above-baseline scan)." >/dev/null 2>&1
.venv/bin/python config_scan.py --until 07:55 --minutes 30 --configs "$CFGS" > ~/trim_work/$D/cosmics.stdout 2>&1
say "cosmics runs done: $(grep 'CONFIG SCAN END' /local/p2/config_scan.log | tail -1)"
say "ALLDONE chain"
