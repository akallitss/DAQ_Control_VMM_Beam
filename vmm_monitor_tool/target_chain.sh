#!/bin/bash
# Target mode, pass 2 and check (HV off, no DAQ run), 16 Nov 2026:
#   pass 2 (trims only) from the map of the pass-1 file -> map of the pass-2 file
#   -> 60 s noise capture of pass 2 and of trimmed_Km18_v1 -> GUI file configured again.
set -e
cd ~/trim_work
E=/local/p2/p2testbench/TestBenchCERN/config_ext
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_
F=out/fastscan_20261116
T=$F/target
FIT=$F/full2/fit_full2.json
echo "START $(date)"
curl -s --max-time 15 localhost:5002/hv_data_live | python3 -c "
import sys,json;d=json.load(sys.stdin);v={k:a[-1] for k,a in d['voltage'].items()};print('HV',v)
assert max(v.values())<5,'HV is ON: stop'"

python3 vmm_monitor_tool/vmm_monitor_scan.py target --keep-sdt --map $T/map_p1.json --fit $FIT \
    --trimcurve out/trimcurve_ch5_40.json --sdtcal out/sdtcal.json --out $T/t50_pass2.json \
    --ext-in $E/${P}target50_p1.txt --ext-out $E/${P}target50_p2.txt --force
mkdir -p $T/mapwork
python3 vmm_monitor_tool/vmm_monitor_scan.py map --ext $E/${P}target50_p2.txt --unmask --bin ~/trim_work/build/vmm_monitor \
    --workdir $T/mapwork --out $T/map_p2.json > $T/map_p2.log 2>&1
echo "MAP DONE $(date)"
python3 vmm_monitor_tool/noise_probe.py --ext ${P}target50_p2.txt --ext ${P}trimmed_Km18_v1.txt \
    --seconds 60 --max-mb 300 --out-dir $T/probe --restore
echo "ALLDONE $(date)"
