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

Per-chip hit rate in kHz from the first 40 MB of each point (`cosmic_vmm_analysis/noise_scan_summary.py`;
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

### Per-channel calibration: what the new software has (checked 2026-10-22, install_2026-10-22T08h46m22sZ)

22. **The measurement primitives exist in `libp2b-sc.so` but are not reachable.** `include/p2b/sc/cmd/MeasureVmmI2C.h`
    (dated 2026-10-16) defines `MeasurePulserDac`, `MeasureThresholdDac`, `MeasureBandgapReference`,
    `MeasureTemperature`, `MeasureVmmChannel(channel)` and `MeasureChannelThreshold(channel)` ("measures one
    VMM channel with threshold mode on and off and returns the signed difference"), plus `ReadVmmAdc` /
    `VmmAdcOperation.h`. `sc::Backend` instantiates every one of them for each active FEC
    (`UdpCommandTuple`) and `Connector::runCommand(cmdId)` can run them. But:
    - `p2basket-sc --help` and `CmdLine.all_action_cmds()` list only `read-temperature` among them;
    - the Python binding exposes only `Cmd/CmdLine/SessionLock` and `Backend.connector()`, whose return type
      (`p2b::sc::Backend::Connector`) is not registered, so Python cannot call it;
    - the instances created by the Backend use the default channel 0, so a per-channel scan needs commands
      constructed with each channel number;
    - `p2basket-gui` and `test_p2b-sc` contain no measure/calibration strings.
    This is the vmmsc calibration (pedestal per channel, threshold per channel, trim `sd`), so the new
    firmware/software seems to be heading there. Question for the developers: a CLI command (e.g.
    `--measure-channel-threshold`, all channels, all chips) or a Python binding for `Connector` and the
    measure commands?

### HV-off scan (2026-10-23, runs 14-27; all six HV channels switched off by hand)

Runs 14-23 = the same configs as the HV-on m2 scan (`..._m2hvoff_d<d>`), runs 24-27 = nothing masked
(`..._m0hvoff_d50/40/34/30`). Tables: `analysis_noise_scan_hvoff/` (chip table, per-channel CSV,
`hv_on_off_channels.csv` from `cosmic_vmm_analysis/compare_hv_noise.py`).

23. **The electronics are silent at d=50 with HV off: 0.00 kHz on every chip**, against 0.04-0.13 kHz per
    chip with HV on. The low-level rate in the HV-on scan therefore all comes from the detector side
    (cosmics, chamber activity), none from the electronics.
24. **The spread of the noise onset between chips is an electronics effect.** First d above 1 kHz with HV
    off: VMM 7: 40; 15: 34; 12, 19: 27; 3, 13, 14, 17: 24; 18: 22; 8, 11, 16: 20; 10: 18; 5: 16; 2: not
    reached; VMM 6 never fires. The ordering is the HV-on one, usually 0-6 DAC later (VMM 5 is the exception: 27 with HV on, 16 with HV off), so the chip-to-chip
    spread of ~16 DAC remains with no voltage on the chambers.
25. **The VMM 4, 7 and 9 bursts do not need HV.** Unmasked, HV off (kHz at d=50/40/34/30): VMM 4:
    69 / 1709 / 383 / 33; VMM 9: 1101 / 144 / 857 / 423; VMM 7: 53 / 80 / 0.7 / 0.01. So they come from
    the chip, hybrid or cable (VMM 4 = H2 Vmm 0, VMM 9 = H4 Vmm 1), not from the detector pads.
26. **Channels still noisy with HV off (more than 1 kHz with HV on and at least half of that with HV off):**
    VMM 12 (29 channels) and VMM 15 (31 channels) have wide sets, so a chip-wide electronics problem;
    smaller sets on VMM 11 (ch 10, 19, 33, 38, 52), 13 (8 channels), 14 (ch 10, 11, 31, 32, 47, 55),
    19 (ch 3, 11, 13, 18, 20, 24, 27, 53); single channels VMM 3 ch 37, 7 ch 23, 10 ch 22, 17 ch 32.
    For every other chip the noisy channels disappear with the HV off: the pads or the gas.
27. **Observation 21 explained (probably): the rate peaks at a channel's own baseline.** The same channel
    rises and falls: VMM 7 ch 23 489 / 332 / 60 / 4.2 / 0.5 / 0.03 kHz from d=34 to d=20 (HV on; 566 / 269
    kHz at d=34 / 30 with HV off); VMM 5 ch 46 0.6 / 7 / 62 / 181 / 416 kHz from d=30 to d=20, then gone at d=18
    (ch 47 takes over at 0.2 kHz). A channel fires most when the threshold sits just above its baseline and
    stops sending hits once the threshold is well below it (always over threshold, no new edges). So
    these are single channels with a higher baseline than their chip, and the scan itself dates their
    baselines. Not tested directly; the monitor-ADC read would confirm it.

### Neighbour read-out (sng 1) baselines, HV off (2026-10-23, runs 28-32)

`sng 1` on all detector chips (`Field sng 1`, register `bank1spi2`), HV declared off, VMM 4/9 fully masked and
VMM 7 ch 24 masked as in the m2 scan. Run 28 = probe (per-chip thresholds guessed from the HV-off table); runs 29-32 =
a ladder at 0 / -3 / -6 / -9 DAC from each chip's HV-off noise onset (VMM 6 at sdt 218/214/210/206), 2 min each.
Analysis: `cosmic_vmm_analysis/neighbor_baseline.py` (first 800 MB of each capture); tables in `analysis_sng1_hvoff/`.

28. **Neighbour read-out works as expected.** About two thirds of all hits (64-67 % on every chip) carry the
    over-threshold flag 0 (hit word bit 14): two neighbours per firing channel. Their ADC is a direct measurement of the
    channel at rest.
29. **Measured baselines are not the shared 38.** Covered channels on the 16 working chips (518 of 1024, after
    100 s of capture in total): median ADC 54; chip medians 47.5 (VMM 10) to 60.5 (VMM 15); channel values 16 to 80
    (plus the outlier of obs. 30); spread inside a chip about 3-9 ADC; only 8 of the 518 channels are at or below 38.
    Coverage per chip 6-46 of 64; VMM 6 only 6, VMM 7 13. It grew at every step of the ladder (335, 337, 352, 426, 518
    channels), so more time at the high-rate steps (only 5-10 s of each 2 min file were analysed) would add more.
30. **One abnormal channel:** VMM 16 ch 33 has a median neighbour ADC of 704 (1192 read-outs), against 31-69 for all
    other channels of that chip. Not explained (stuck channel, damaged input, different mode?).
31. **(superseded by item 35) Baseline vs position of the noise peak: weak positive trend.** Baselines (HV off) against the peak step d of the
    HV-on noise scan (the HV-off scan does not reach its peaks within d = 50..16): Spearman +0.56 (10 channels, at
    least 30 hits at the peak), +0.30 (32 channels, 10 hits), +0.35 (66 channels, 5 hits). Consistent with "a higher
    baseline peaks at a higher threshold" (obs. 27) but not conclusive: few channels, coarse steps, low counts.
32. **Data volume of the ladder:** 1.2 / 1.8 / 4.9 / 4.8 GB per minute at 0 / -3 / -6 / -9 DAC (run_31 and run_32:
    9 GB each), about 25 GB in total with the probe. Hot channels, now with three times more hits each, dominate.

### Whole captures analysed on lxplus (2026-10-23; supersedes the 800 MB heads of items 29-31)

`neighbor_baseline.py` with its own pcapng reader (identical hit counts to the scapy version; 100 MB in 2 s) run on
lxplus over the five whole captures read from the EOS backup (25 GB, about 8 minutes, low priority), so that
vmm_daplxa did none of the work. 599 s of capture in total.

33. **Coverage 627 of 1024 channels** on the 16 working chips (410, 410, 428, 528, 627 after the probe and each step; the
    two lowest thresholds add the most). Per chip 13 (VMM 6) to 54 (VMM 12) of 64; VMM 7 has 18. Median baseline still 54 ADC;
    chip medians 48.0 (VMM 10, 11) to 61.0 (VMM 6, 15); 10 of the 627 channels are at or below 38.
34. **Three channels with a very high baseline:** VMM 16 ch 33 (704 ADC, 13582 read-outs), VMM 6 ch 14 (186, 66 read-outs),
    VMM 14 ch 34 (125, 85 read-outs). VMM 14 ch 34 is also one of the hottest channels in the noise scans. Not explained.
35. **The ADC baseline does not predict where a channel's noise peaks.** With the whole captures: Spearman +0.43 (14 channels,
    at least 30 hits at the peak), +0.32 (40, 10 hits), +0.17 (83, 5 hits); Pearson +0.07 / +0.08 / +0.02. Without the one
    channel above 100 ADC (VMM 16 ch 33, which also stretches the axes): Spearman +0.48 / +0.33 / +0.17 on 13 / 39 / 82
    channels, Pearson +0.11 / +0.21 / +0.04 (the figures in the note use these). The earlier
    "weak positive trend" (item 31) came from too few channels. Possible reasons, none tested: the ADC baseline is not the
    level the threshold comparator acts on (the comparator has its own offset per channel, which the vmmsc
    "threshold above pedestal" measurement gives); coarse threshold steps; HV-on peaks against HV-off baselines. This weakens the
    reading of item 27 that a channel peaks at its ADC baseline.

### Anatomy of the VMM 4 / 7 / 9 bursts (2026-10-23, first 400 MB of runs 1, 2, 24-27, run on lxplus)

`cosmic_vmm_analysis/burst_anatomy.py`; result in `analysis_bursts/burst_anatomy.json`. Runs 1-2: HV on, VMM 4/9 held at sdt 400;
runs 24-27: HV off, nothing masked, VMM 4/9 at d = 50, 40, 34, 30 (sdt 249 ... 229 / 241 ... 221).

36. **The hot channel is stable inside a run and changes between runs.** In the 35-55 s of capture analysed, the share of each hot
    channel is constant from one eighth of the capture to the next (e.g. VMM 4 ch 38/36 = 61/39 %, 63/37 %, ...). What changes is
    the set between configurations: VMM 4: ch 49 (runs 1-2) -> ch 38 + 36 (run 24) -> ch 36 + 38 (runs 25-27); VMM 9: ch 41, 63, 33,
    44, 42 (runs 1-2) -> ch 20 + 21 (run 24) -> ch 19 + 21 (run 25) -> ch 19 (runs 26-27); VMM 7: always ch 24. So the "walk" happens when the
    chip is reconfigured (new apply, new threshold), not during a run. The active channels often sit next to each other
    (36/38 around 37, 19/21 around 20, 20/21).
37. **The burst hits have a narrow ADC and no pulse spectrum.** ADC 5th-95th percentile: VMM 4 ch 49 228-233 (width 5);
    VMM 9 ch 41 232-241; HV-off runs: VMM 4 ch 38 104-111, ch 36 88-96 and 81-95; VMM 9 ch 20 82-92, ch 19 76-87; VMM 7 ch 24 83-101. These
    values are close to a fixed number per run and much higher than the 45-65 ADC baselines of other channels; they
    are ~230 at sdt 400 and 80-110 at sdt 220-250, so they move with the threshold setting.
38. **The hits come at about the fastest rate the channel can fire.** Spacing between consecutive hits of the busiest channel inside a
    packet, in 22.5 ns ticks: the most common value is 26-27 ticks (about 600 ns, 1.65 MHz) in several runs of VMM 4 and 9
    (e.g. VMM 9 ch 20, run 24: 5th/50th/95th percentile 21/46/101 ticks), with secondary values near 50 and 95. Not a sharp oscillation
    (only 1-12 % of the spacings within one tick of the mode). Looks like a channel that re-fires as soon as it can, i.e. an
    input stuck at or above the threshold, not like random noise or pulses. Not tested beyond this.

### Software incidents of 2026-10-23, block A+C start (all fixed, no data lost)

39. **Flask died unnoticed** (last log line 13:53, cause unknown, memory guard shows no kill, nothing in flask.log). The next
    config_scan timed out waiting for the warm reset. Restarted the vmm_flask session only.
40. **Race in the start route (fixed, 55a3773).** `/run_config_py` launched `run_config_beam.py` with Popen + sleep(1) and read the
    json; the generator took ~2 s, so the PREVIOUS run's json (run_32, sng1hvoff_m9) was used and a n479hvoff_d200 capture was written
    into the old run_32 m9 folder (3 GB, plus overwritten run_config.json, vmm_daq.log, run_time.txt, tdk csv). config_scan's label check
    caught it. Capture moved to `orphan_2026-10-23_1621_n479hvoff_d200/`; run_32/run_config.json restored from EOS (original capture intact
    locally and on EOS). `/update_run_config_py` had the same race (Popen + sleep 0.2); both now wait for the child.
41. **The pruner made a run name reusable (fixed).** It deleted old local runs once on EOS, so `iterate_run_num` found `run_1` free and
    started a run there, while EOS `run_1` is yesterday's d=50 run. Stopped after ~1 min, data moved to
    `orphan_2026-10-23_1626_run_1_n479hvoff_d200/`, EOS run_1 checked untouched. `iterate_run_num` now keeps a high-water mark in
    `config/run_highwater.txt` (set to 32; the first good run of block A+C is run_35). After a `git checkout run_config_beam.py`
    (which resets run_name to run_1) the counter steps over all used numbers.
42. **config_scan's `/stop_run` after a failed label check did not stop the run in time:** the mislabelled capture ran its full 2 minutes.

### Blocks A + C: VMM 4, 7, 9 unmasked, HV off, d = 200 ... 14 (runs 35-56, analysed on lxplus 2026-10-23)

Block A (runs 35-47, sng 0): VMM 4/7/9 at d = 200, 100, 60, 50, 40, 34, 30, 27, 24, 22, 20, 18, 16; all other chips at d = 50 (quiet).
Block C (runs 48-56, sng 1): the same chips at d = 100, 60, 50, 40, 34, 27, 22, 18, 14. Rates from the first 300 MB of each capture
(`burst_anatomy.py`, `analysis_n479_hvoff/`); run_56 (d = 14) was not yet on EOS and is not included. Temperatures of all chips 31-38 C.

43. **The bursts depend on the configuration that was applied, not on the threshold.** VMM 4 (kHz at d = 200/100/60/50/40/34/30/27): 12 / 0 / 0 / 27 / 1768 / none / 0 / 0.
    VMM 9: 2727 / 26 / 688 / 105 / 9 / 0 / 0.5 / 6. VMM 7: 820 / none / 15 / 35 / 144 / 547 / 0 / 0. The rate jumps by orders of magnitude between neighbouring
    thresholds and back; the hot channel changes with it (VMM 4: ch 45/43 at d = 200, ch 39 at 100, ch 38/36 at 60-40, ch 37/35 at 30 and below;
    VMM 9: ch 45/33, 25/26, 27/24, 21, 16; VMM 7: ch 47/35, ch 25, ch 24, ch 23, ch 10). Not tested yet whether re-applying the SAME config
    reproduces the same state (block B, the repeat points, was dropped from this session).
44. **Underneath there is a smooth noise onset like the other chips.** Below the burst region the rate rises monotonically with falling d:
    VMM 4 (ch 37/35) 0.8 / 5 / 26 / 112 / 340 kHz at d = 24 / 22 / 20 / 18 / 16, first above 1 kHz at d = 22; VMM 9 (ch 16, with ch 18) 0 / 0.5 / 6 / 50 / 160 / 380 / 740 / 1010 kHz at
    d = 34 / 30 / 27 / 24 / 22 / 20 / 18 / 16, first above 1 kHz at d = 27. VMM 7 shows no smooth onset down to d = 16 (ch 10 at 0.4 kHz).
    (The d values use the same extrapolated baselines as before, 199 / 200 / 191 for VMM 4 / 7 / 9.)
45. **With sng 1 the same chips give their neighbours' baselines, but only for the channels that fire:** VMM 4 16 of 64 channels, median of channel medians 64
    (range 47-153); VMM 7 11 channels, median 53 (46-177); VMM 9 19 channels, median 85 (44-240). The high values belong to the burst channels
    themselves (obs. 37). Coverage is low because very few channels fire at all.
46. **The first point of block A (d = 200, far above any baseline) already shows 2.7 MHz from VMM 9 and 0.8 MHz from VMM 7.** The burst is therefore not a threshold-crossing of
    ordinary noise. Combined with obs. 37 and 38 (fixed ADC, ~600 ns spacing) this looks like a state the channel input gets stuck in after configuration.

### 24 Oct: HV ON measurements analysed (runs 57-75, on lxplus from EOS), and the access incident

47. **Access incident, Fri 23 Oct 20:28 - Sat 24 Oct ~18:30.** SSSD's domain backend was killed by its own watchdog (22 Oct 20:03, 23 Oct 20:28; nss 22 Oct 18:51) and
    `krb5_child: Matching credential not found` appears twice a day. From then on every login of the central account ak271430 failed with
    `pam_sss ... received for user ak271430: 4 (System error)` (key and password), while the local account daqcontrol (key) worked. Machine up 47 days, clock 27 s behind
    and NOT synchronised (timedatectl). Fixed by `sudo systemctl restart sssd` (by the user). The watchdog kills coincide with periods of heavy disk writing (run_75: 4.4 GB/min).
    Related: my ssh used a multiplexed ControlMaster connection that died when the laptop changed network, so no login happened until a password was typed again.
    The sequence launched Friday ended by itself at 20:24 (disk guard refused the 6th neighbour point), nothing was queued behind it, so no run was taken until Sat 18:33.
48. **HV-on health check (run_57, 10 min, safe setting = HV-on onset + 8 DAC):** all 18 chips have hits (6-100 Hz per chip), broad spectra; det3 VMM 2,3,4,6,7 reach the ADC limit
    (p90 = 1023), det2/det1 p90 500-850. Rates per chip are well above the expected cosmic rate (~4 Hz): mostly noise/background at that setting.
49. **Neighbour read-out with cosmics, HV on (runs 71-75, 5 x 10 min, whole files 3000 s): 1123 of 1152 channels have a baseline (97 %), 14 of 18 chips complete** (667 before).
    Median 51 ADC, 85 channels <= 38. On the 15 normal chips, channels measured with HV on and off (609): median difference +1.0, MAD 1.0, 70 % within 3 counts, 82 % within 5: HV does not move the baseline.
    Only one channel of the 15 normal chips is above 80 ADC (VMM 16 ch 33, 704).
50. **VMM 4, 7 and 9 each have a block of channels with a high baseline:** VMM 4 19 of 54 measured unmasked channels > 80 (from ch 40, median 191, max 352), VMM 7 29 of 51 (from ch 26, median 235, max 1023 at ch 50),
    VMM 9 32 of 49 (from ch 23, median 135, max 223). The channels that burst (hot in >= 3 runs: VMM 4 34-39, VMM 7 22-25, VMM 9 15-27 + 33, 44) sit at the edge of the block.
    With HV off the block channels that were measured were also high (6 of 7). Masked channels read 0 as neighbours.
51. **The VMM 4/7/9 ladder with HV on (runs 58-70) vs HV off (runs 35-47), taken about an hour apart on Fri 23 Oct:** same configuration, same hot channel and rate within 2x (or both < 1 kHz) at
    VMM 4: 8 of 13 thresholds (7 same, 1 quiet), VMM 7: 10 of 13 (5 same, 5 quiet), VMM 9: 5 of 13. Examples: VMM 4 d=40 1740 vs 1770 kHz ch 36; VMM 7 d=34 546 vs 547 kHz ch 23.
    Differences at d = 34...27: with HV on the chips are noisier (VMM 4 d=30 85 kHz vs ~0), the chamber adding hits near the onset. So the burst state follows the threshold setting (re-applying a config an hour later
    reproduces it at the high thresholds); direct repeat test with the same HV state still to do.
52. **Rate of the lowest-threshold neighbour points:** run_74 (safe-12 DAC) 955 MB/min, run_75 (safe-16) 4447 MB/min (44 GB in 10 min), so the lab config_scan disk guard refused the next point (would leave 32 GB).

### First look at muon coincidences in the reference run (run_76, 30 min, HV on, safe setting; analysed 24 Oct on lxplus)

`cosmic_vmm_analysis/coincidence_check.py` and `event_multiplicity.py` (own pcapng reader; time = srs_timestamp*25 ns + (offset*4096+bcid+1)*22.5 ns - tdc*60/255, markers applied in stream order); results in `analysis_muons/`.

53. **Over-threshold hit rates:** det3 285 Hz, det2 223 Hz, det1 407 Hz (all chips of the chamber together, 2000 s), 1.83 M hits.
54. **Half of the hits are isolated singles and a quarter sit in huge simultaneous bursts.** Grouping hits with gaps > 100 ns: 1.07 M events, 955 701 of them a single hit (52 % of the hits); 1429 events with more than 36 hits
    (0.71 Hz) carry 26 % of all hits (1038 events with 100-999 hits, 5 with more than 1000). These bursts, spread over many chips and chambers at the same time, give millions of fake pairs in a naive time
    coincidence (3.7-6.3 M pairs per 25 ns bin between chambers, against a 4-10 k flat level), so they must be removed (multiplicity cut) before any track finding. Their origin is not known (discharges with the HV on, or electronic pick-up);
    not yet compared with an HV-off run.
55. **Muon-like events exist:** 1140 events with a hit in all three chambers, 645 of them with at most 12 hits per chamber (0.32 Hz = 19 per minute), median 1 hit per chamber; 6110 events with exactly two chambers
    (at most 12 hits each). The expected 40 tracks per minute per chip is not reached (19 per minute for the whole stack in this first look; not corrected for the masked channels, cluster size or acceptance).
56. **Time offsets between chambers in the muon-like events (first hit of each chamber):** det2-det3 median +0.0 ns, det1-det3 -7 ns, det1-det2 -5 ns; spread (robust) 36-43 ns, 10-90 % range about +-55 ns.
    So no large constant offset between hybrids, and the time resolution (uncalibrated TDC) is ~40 ns: a coincidence window of +-100 ns is enough, and a narrower one would lose tracks.

### Weekend runs (24-25 Oct, HV on, gain 4.5): reference run, threshold scan, interleaved long run (analysed on lxplus from EOS; `event_multiplicity.py`, per-run json in `analysis_muons/per_run/`)

57. **Reference run complete and stable.** 16 x 30 min (`run_76`-`run_91`, 8 h), safe setting; all complete, 152-158 MB each (5 MB/min), no guard trip. Muon-like events (hit in all three chambers, at most 12 per chamber)
    20.8 +- 1.1 per minute for the stack, first four runs 20.4, last four 20.4: flat. But the total hit rate rose from 914 to 1431 hits/s over the 8 h (+57 %) and the large bursts (> 36 hits) from 0.71 to 1.24 Hz
    (burst share of the hits 26-31 %), while the muon-like rate did not move. Cause unknown (chamber conditions, temperature, gas?); this is why the long run is interleaved.
58. **Threshold scan with tracks, proxy yield (muon-like events per minute, stack):** safe+16 13.0, safe+8 15.8, safe 20.8, safe-4 22.1, safe-8 468.6. Burst share of the hits 27, 27, ~29, 13, 1 %. The yield rises 22 %, 27 %, 6 % per step from safe+16 down to safe-4, then explodes
    at safe-8 (46 500 hits/s, 7.8 Hz of "muon-like" events), where noise coincidences dominate. This is a proxy, not an efficiency (it mixes muons and chance coincidences); the usable range ends between safe-4 and safe-8.
    Data volume: 315, 315, 343, 1360 MB per hour (the safe-8 point 23 MB/min).
59. **Timing between chambers is stable over 8 h:** medians of the first hit per chamber within -11 and +1 ns, spread 35-48 ns in every reference run.
60. **Interleaved long run started Sun 25 Oct 13:22** (up to 48 x 30 min): safe+8, safe, safe-4, safe-6 in rotation (configs `_thrhvon_p8`, `_safehvon_v1`, `_thrhvon_m4`, `_thrhvon_m6`), user stops it. Safe-8 left out (noise coincidences);
    safe-6 added to find where the noise starts between -4 and -8.
