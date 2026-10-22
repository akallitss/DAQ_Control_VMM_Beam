# Lab cosmics campaign: observations log

Things noticed while bringing up and running the three-chamber cosmic scan on vmm_daplxa.
Kept as feedback for the firmware / slow-control people: the stack was **upgraded between the
SPS beam test and this campaign**, so differences from July are worth reporting. Append new
entries at the bottom with the date; state what was seen, not what it means, unless verified.

Setup: ALINX/FEC at 192.168.0.12, hybrids H1-H9 (VMM 2-19), data NIC enp4s0f1, `p2basket-sc` from
`/local/p2/deploy`. No trigger hybrid in this setup (H0 not used).

## 2026-10-22

### Slow control / firmware

1. **Tool banner:** every `p2basket-sc` call prints `2026-10-20T161337Z-69ca52a-dirty-main`
   ("dirty"). The deploy dir is `/local/p2/deploy/install_2026-10-22T08h46m22sZ`.
2. **Error after every slow-control call:** warm reset, chip apply and link-status reads all end with
   `Error: [Errno 13] Permission denied: '/local/p2/deploy/install_2026-10-22T08h46m22sZ/python-venv'`
   then `p2basket-python: line 9: .../python-venv/bin/pip: No such file or directory` and
   `line 17: .../python-venv/bin/activate: No such file or directory`. The command itself still
   succeeds (rc 0, 0 failed hybrids). **Cause (read from `bin/p2basket-python`):** the wrapper does
   `if [ ! -d $INSTALL_DIR/python-venv ]; then python3 -m venv ...`, and the 2026-10-22 install dir
   (`install_2026-10-22T08h46m22sZ`) has no `python-venv` directory (the 2026-06-17 install has one),
   so every call tries to create it in a directory our user cannot write.
3. **Link status of hybrid 0:** `hybrid[00].linkStatus = 4` (ready) like hybrids 1-9, although this
   stand has no trigger hybrid. Hybrids 10-15: 0. No VMM 0/1 hits appear in the data.
4. **`vmmAppReg[15]` changed during a run:** `0x00000044` before the run, `0x00002044` in the link
   check logged when the run finished, and still `0x00002044` in a standalone read minutes later
   (hybrid links all still `4`). `vmmAppReg[16]` stayed `0x44444444`. Meaning unknown: the new
   bit is bit 13.
5. **July `_opt` config registers vs comments:** the H0 lines say `# 300` and `# 1023` but the
   registers (`spi2*64 + spi1`) give 428 for both. For H1-H9 the comments match the registers.
6. **Masks:** the `_opt` file's "Masked channels" section writes `sm 0`, which is the base yaml
   default for all 1280 channels. If `sm 1` is the masking value those three channels were not
   masked in July. Not verified on hardware.

### Readout

7. **Flood at the July `_opt` thresholds** (run with the chambers at HV, 2 min): 1.5 GB in one
   file, about 48 GB/h. From a 60 MB sample (6634 packets, 5.9 s): 1.66 MHz total, VMM 9 = 89 %,
   VMM 4 = 10 %, VMM 14 = 0.4 %, all others < 0.03 %. VMM 4 and 9 were at sdt 300 (the "walking
   burst" chips of the beam test). All of VMM 2-19 present.
8. **QA on a 1.5 GB file does not finish (cause found: killed by mem_guardian, not I/O):** `mem_guardian.log`
   shows `KILL ... rss=4454MB ... vmm_pcapng_qa.py .../run_56/...pcapng` at 17:21:04 (MemAvailable 865 MB).
   Original observation: `vmm_pcapng_qa.py` with `--max-packets 100000` ran 3.5 min
   and wrote nothing (killed by timeout, mostly I/O wait). The same script on the first 60 MB took
   well under 200 s. At this rate every 120 s capture file is ~1.5 GB, so online QA would not keep up.

### Machine / software

9. `screen` is not installed, so `bash_scripts/restart_all_tmux_processes.sh` kills tmux and does not
   restart anything.
10. `start_servers.sh` is not idempotent: for a session that already exists it types its command into
    the existing pane.
11. The Kerberos ticket on the machine had expired on 3 Aug (needed for the EOS backup).
12. The apply returns before the chips have necessarily settled: the first `/chip_config/status` poll
    after `apply` still showed the previous file, so poll on `last.file` as well as `running`.

### Noise scan, first points (2026-10-22 18:08-18:14)

13. **The flood is a few hot channels, independent of threshold.** run_1 (d=50, VMM 4 and 9 held
    at sdt 400): 635 MB/min, ~1.6 MHz, VMM 9 76 %, VMM 4 23 %, VMM 7 1 %, everything else ~100 Hz.
    The same as at sdt 300 in the smoke test. From a 30 MB head of the capture: VMM 4 ch 49 = 99.2 %
    of VMM 4; VMM 9 ch 41/63/33/44/42 = 32/19/17/15/14 % of VMM 9; VMM 7 ch 24 = 99.1 %. Masking
    (`sm 1`) is the fix being tried (configs `..._cosmic_m1_d<d>`), polarity still to be confirmed by
    the first masked point.
14. **Write before QA:** with the QA watcher off, `RUN_QUALITY.json` has no hits per VMM (it reads the
    QA's events.json). The per-VMM numbers above were computed by hand from the pcapng.
15. `bash_scripts/stop_scan_after_current.sh` is hard-wired to the July tree and BEAM_QUALITY.json,
    so it cannot be used here; the sequencer was stopped with SIGINT and the run in progress finished
    by itself.
16. **The hot channel hops (masking channel by channel fails).** run_3 (m1 masks, d=50): `sm 1` works
    (every masked channel reads 0, so 1 = masked), but VMM 9 moved its burst to ch 20/21 (98 % of the
    chip) and VMM 4 to ch 36/38; still ~420 MB/min. Same rate at sdt 300 (smoke test), 400 (run_1), and
    ~250 (run_3), so it is not set by the threshold. VMM 4 = H2 Vmm 0 (P2_IN c5), VMM 9 = H4 Vmm 1
    (P2_MID c4), VMM 7 ch 24 (13 kHz) is a single steady channel. These chips need a separate look
    (hybrid, cable, or the firmware change?).
17. **With VMM 4 and 9 fully masked (m2, all 64 channels) the data rate is ~1 MB/min** (run_4, d=50),
    about 130x lower (635 -> 5 MB/min; an earlier "1 MB/min" and "600x" were wrong), and every other chip is quiet.
18. **ReadADC (per-channel pedestal and threshold) is not reachable from `p2basket-sc`.** The vmmsc
    calibration (gitlab.cern.ch/rd51-slow-control/vmmsc, `calibration_module.cpp`) works by reading
    the VMM monitor ADC through slow control, not from hits. The new library has it
    (`include/p2b/vmmdcs/fec_config_module.h: int ReadADC(HwIndex iHyb, HwIndex iVmm, int adc_chan)`)
    but the `p2basket-sc` command list has no such command, and `lib/python/site-packages/p2basket`
    has no `ReadADC` either. Asking the firmware/software people whether a command or a Python
    binding exists would save writing a tool against `libp2b-sc.so`.
19. **VMM 7 joins the hopping bursters.** m2 scan, run_6 (d=34, sdt 234 on VMM 7): 179 MB/min (d=40 was
    9 MB/min); VMM 7 = 99 % of the hits, 4.77 M of 4.82 M in a 30 MB sample, on ch 23 (the masked ch 24
    reads 0). Next largest: VMM 15 ch 56 (0.8 %, 35 k hits). So the three chips with a burst are VMM 4
    (H2 Vmm 0), VMM 7 (H3 Vmm 1) and VMM 9 (H4 Vmm 1), exactly the three that ran at the highest July
    sdt (300 / 285 / 300) and whose baselines were extrapolated.

### Noise scan result (2026-10-22, run_4..run_13, m2 configs, d = 50..16 above the assumed baselines)

Per-chip hit rate in kHz from the first 40 MB of each point (`quick_scripts/noise_scan_summary.py`;
table and per-channel CSV in `analysis_noise_scan/` of the campaign dir). VMM 4 and 9 fully masked,
VMM 6 fixed at sdt 230.

20. **First d at which a chip exceeds 1 kHz (chip total):** VMM 7: 40 (burst, see 19); VMM 15: 34;
    VMM 12, 14, 19: 30; VMM 3, 5: 27; VMM 8, 13, 17: 24; VMM 16, 18: 22; VMM 11: 20; VMM 10: 18;
    VMM 2: not reached (0.33 kHz at d=16). So the onset spans 16 DAC across chips: the assumed
    baselines (July ADC-edge estimates) are off by up to that much, which is what the per-channel
    calibration is meant to remove.
21. **Rates fall again at the lowest d for some chips, unexplained:** VMM 7: 489 (d=34), 332, 60, 4.2,
    0.5, 0.04 kHz (d=20); VMM 5: 416 (d=20) then 0.33 (d=18), 2.9 (d=16). Lowering the threshold should
    not lower the rate. Possible: readout starvation when the link is saturated (total volume is 4 GB/min
    at d=16), a start-of-run transient in the head of the file, or dumpcap drops. Not checked yet:
    needs the tail of the same files and the packet/drop counters.
