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
`hv_on_off_channels.csv` from `quick_scripts/compare_hv_noise.py`).

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
Analysis: `quick_scripts/neighbor_baseline.py` (first 800 MB of each capture); tables in `analysis_sng1_hvoff/`.

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

`quick_scripts/burst_anatomy.py`; result in `analysis_bursts/burst_anatomy.json`. Runs 1-2: HV on, VMM 4/9 held at sdt 400;
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
