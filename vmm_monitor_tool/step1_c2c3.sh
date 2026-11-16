#!/bin/bash
# Step 1 of docs/PLAN_PEDESTAL_NOISE.md on chips 2 and 3 (HV off, no DAQ run):
#   (a) fresh monitor map (baseline + threshold per channel, mV) of trimmed_Km18_v1
#   (b) noise rate scan: chips 2,3 sdt shifted -14..+10 counts, 30 s each, sng 0
#   (c) pulser pedestal/noise: chips 2,3 sdt +40, phases 0,1,2 x sdp10 80,160, 5 s each
# The GUI selection (Km18_i5) is configured again at the end.
set -e
cd ~/trim_work
E=/local/p2/p2testbench/TestBenchCERN/config_ext
T=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_step1_c2c3
O=out/step1
mkdir -p $O/files
echo "START $(date)"
curl -s --max-time 15 localhost:5002/hv_data_live | python3 -c "
import sys,json;d=json.load(sys.stdin);v={k:a[-1] for k,a in d['voltage'].items()};print('HV',v)
assert max(v.values())<5,'HV is ON: stop'"

# (a) map
python3 vmm_monitor_tool/vmm_monitor_scan.py map --ext $E/$T --unmask --bin ~/trim_work/build/vmm_monitor \
    --workdir $O --out $O/map_trimmed_v1_now.json > $O/map.log 2>&1
echo "MAP DONE $(date)"

# (b) rate scan files
RS=()
for d in 10 6 3 0 -2 -4 -6 -8 -10 -12 -14; do
  tag=$( [ $d -lt 0 ] && echo m${d#-} || echo p$d )
  f=${P}_ratescan_sdt${tag}.txt
  python3 vmm_monitor_tool/vmm_trim.py sdt $E/$T --out $E/$f --chips 2,3 --by $d --force > /dev/null
  RS+=(--ext $f)
done
python3 vmm_monitor_tool/noise_probe.py "${RS[@]}" --seconds 30 --max-mb 300 --out-dir $O/ratescan
echo "RATESCAN DONE $(date)"

# (c) pulser phases
python3 vmm_monitor_tool/vmm_trim.py sdt $E/$T --out $O/files/base_sdtp40.txt --chips 2,3 --by 40 --force > /dev/null
PS=()
for a in 80 160; do
  python3 vmm_monitor_tool/pulser_files.py $O/files/base_sdtp40.txt --out-prefix $E/${P}_pulser_a$a \
      --chips 2,3 --sdp10 $a --all-phases --unmask --force > /dev/null
  for p in 0 1 2; do PS+=(--ext ${P}_pulser_a${a}_p$p.txt); done
done
python3 vmm_monitor_tool/noise_probe.py "${PS[@]}" --seconds 5 --max-mb 300 --keep-pcap --out-dir $O/pulser --restore
echo "PULSER DONE $(date)"

for f in $O/pulser/*.pcapng; do
  n=$(basename $f .pcapng)
  systemd-run --user --scope -q -p MemoryMax=2G /local/p2/p2-readout/venv/bin/python \
      vmm_monitor_tool/pulser_pedestal.py analyse $f --ext $E/$n.txt --chips 2,3 --out $O/pulser/$n.ped.json --quiet
done
echo "ALLDONE $(date)"
