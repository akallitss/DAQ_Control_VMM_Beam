#!/bin/bash
# Step 1b: does the excess spread of the neighbour ADC scale with the pulse (amplitude, number of pulsed channels)?
# Chips 2,3, phase 0, 5 s each, HV off, no DAQ run. GUI file configured again at the end.
set -e
cd ~/trim_work
E=/local/p2/p2testbench/TestBenchCERN/config_ext
T=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt
P=p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_step1b_c2c3
O=out/step1b
mkdir -p $O/files
echo "START $(date)"
curl -s --max-time 15 localhost:5002/hv_data_live | python3 -c "
import sys,json;d=json.load(sys.stdin);v={k:a[-1] for k,a in d['voltage'].items()};print('HV',v)
assert max(v.values())<5,'HV is ON: stop'"
python3 vmm_monitor_tool/vmm_trim.py sdt $E/$T --out $O/files/base_sdtp40.txt --chips 2,3 --by 40 --force > /dev/null
G=vmm_monitor_tool/pulser_files.py
python3 $G $O/files/base_sdtp40.txt --out $E/${P}_sdtp40_s6_a80.txt --chips 2,3 --phase 0 --stride 6 --sdp10 80 --unmask --force
python3 $G $E/$T --out $E/${P}_thr_s3_a40.txt --chips 2,3 --phase 0 --stride 3 --sdp10 40 --unmask --force
python3 $G $E/$T --out $E/${P}_thr_s3_a25.txt --chips 2,3 --phase 0 --stride 3 --sdp10 25 --unmask --force
python3 $G $E/$T --out $E/${P}_thr_s6_a40.txt --chips 2,3 --phase 0 --stride 6 --sdp10 40 --unmask --force
python3 vmm_monitor_tool/noise_probe.py --ext ${P}_sdtp40_s6_a80.txt --ext ${P}_thr_s3_a40.txt --ext ${P}_thr_s3_a25.txt \
    --ext ${P}_thr_s6_a40.txt --seconds 5 --max-mb 300 --keep-pcap --out-dir $O --restore
for f in $O/*.pcapng; do
  n=$(basename $f .pcapng)
  systemd-run --user --scope -q -p MemoryMax=2G /local/p2/p2-readout/venv/bin/python -W ignore \
      vmm_monitor_tool/pulser_commonmode.py $f --ext $E/$n.txt --chips 2,3 --out $O/$n.cm.json
done
echo "ALLDONE $(date)"
