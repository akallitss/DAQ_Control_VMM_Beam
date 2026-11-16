# Handoff — fast noise rate scan of all chips (state at the end of 16 Nov 2026)

> **Update 16 Nov, 20:17–21:51: the fast scan has been run on chips 2–19 (user's go).**
> Result in §9 below. Short version:
> - σ per channel for **1105 of 1152 channels** (≥ 4 points, centre fixed at the monitor
>   baseline): `~/trim_work/out/fastscan_20261116/full2/fit_full2.json` (copy in
>   `vmm_monitor_tool/fastscan/results_20261116/`). Chip medians 2.5–3.7 mV.
> - Reproducible: a second full scan agrees per channel to 1–4 % rms on most chips (≤ 11 %),
>   chip medians within ±4 %, r = 0.85–1.00.
> - Two hardware effects found: (1) after a VMM-only write the read-out sometimes sends
>   **nothing** (links still 4) until a full configure; (2) channels held at/below their
>   baseline for minutes go **silent on noise**, survive warm reset + configure, and are
>   **brought back by test pulses**. §9.3 of PLAN (the 12 chip-3 channels) was effect (2).
> - **Target mode done (22:20–22:54, §10)**: `config_ext/…_cosmic_target50_p2_rt3.txt` puts
>   every channel at ~50 Hz of its own noise (HV off): measured per-channel median 30 Hz,
>   p90 70 Hz, p99 ~100 Hz; thresholds 4.7 mV lower than `trimmed_Km18_v1` (median).
>   Not yet tried with HV or in a cosmic run.

For the next session. Read this first, then `PLAN_PEDESTAL_NOISE.md` (the full lab
notebook of 16 Nov, §6–9 are the measurements), `PER_CHANNEL_THRESHOLDS.md` (the trim
work of 13 Nov) and `CODE_MAP.md` (GUI → bench scripts → p2basket-sc → chip).
Memory note: `vmm-slow-control-code-map`.

## 1. Where we are, in five lines

1. Goal (user): every channel's threshold at **baseline + n·σ** (its own noise), using the
   per-channel trims, so quiet channels can go lower and noisy ones are raised instead of masked.
2. **Baseline** = monitor read-back (`MeasureVmmChannel`, `smx 0`, mV, at the discriminator).
   **σ** = noise rate scan, fit with the centre fixed at that baseline. Both validated on chips 2–3.
3. The internal-pulser (neighbour ADC) method is **on the back burner** (user decision): its σ
   does not follow the discriminator noise per channel.
4. Next: a **fast scan of chips 2–19 together** (~35 min, design in §4), then the `target`
   mode that writes the n·σ trim file, then a check of the predicted noise rates.
5. **Nothing is running.** The stand is on `…_adwk2_Km18_i5.txt` (GUI apply rc 0, armed,
   restored 21:51), HV off (< 0.5 V on all six channels), LV on.

## 2. What is established (numbers you can rely on)

| fact | value | where |
|---|---|---|
| per-channel σ, rate scan, centre fixed at monitor baseline | chip 2 median 3.5 mV [2.8–4.6], chip 3 3.2 mV [2.3–4.3] | PLAN §8 |
| reproducibility of that σ, same scan 75 min later | 0.5–0.7 % per channel (r 0.99–1.00) | PLAN §8 A |
| σ from a trim scan of 8 channels (thresholds re-measured at each point) vs sdt scan | within 0–15 %, typically ~3 % | PLAN §8 B |
| noise centre (free fit) − monitor baseline | median within ±2 mV; free fit itself too loose (8 %) with 3–6 points | PLAN §8 |
| f0 (Rice) measured | 0.4–0.7 MHz chip 2, 1.2–1.4 MHz chip 3; transfer function: 1.0 (current noise) … 2.6 MHz (voltage noise) at 200 ns | PLAN §3, §8 |
| n for a target noise rate R per channel | n = √(2 ln(f0/R)); R = 50 Hz → n ≈ 4.0–4.6 | PLAN §3 |
| monitor resolution | 1 mV steps | PLAN §8 |
| sdt DAC | 0.73–0.81 mV/count per chip (`~/trim_work/out/sdtcal.json`) | 13 Nov |
| trim | 0.97–1.25 mV/step per chip, written bit-reversed (vmm_trim does it) | 13 Nov |
| VMM-only register write between points | 3.8 s vs 12 s GUI apply; links stay 4; rates equal within 5–10 % | PLAN §9.2, `vmm_monitor_tool/fastscan/` |
| a channel within ~2 mV of its baseline | count falls back to 0 (comparator stays high) | PLAN §8 |
| deep noise rates seen | up to 640 kHz per channel; chip totals 1.7–5.5 MHz fill a 300 MB capture in 7–27 s | PLAN §8, §9 |
| whole-chip bursts | ~once per scan, ~1 Hz on every channel of a chip during one capture | PLAN §8 |
| **noise is not stable in time** | 12 channels of chip 3 (2, 14, 16, 24, 25, 26, 29, 42, 54, 57, 59, 63) went from 0.2–11 kHz to 0 Hz at sdt −6 between 19:21 and 19:46; baseline/threshold unchanged, all respond to test pulses | PLAN §9.3 |
| pulser rate | `tp_number` × 10.85 kHz (FEC register, base YAML only); min 10.85 kHz | PLAN §1–2 |
| chip 2 ch 59 | its data-path ADC always reads 0 (still counts and fires) | PLAN §2 |

## 3. Tools (branch `worktree-code-map-channel-thresholds`, pushed; copies in `~/trim_work/vmm_monitor_tool/` on the box)

Run on the DAQ machine (`ssh vmm_daplxa`), from `~/trim_work`. Keep the box copy in
sync by `scp` after every change (the box runs `main`, not this branch).

| tool | use |
|---|---|
| `vmm_monitor_scan.py map --ext FULLPATH --unmask [--channels L] [--no-reapply] --bin ~/trim_work/build/vmm_monitor --workdir DIR --out MAP.json` | monitor map: baseline + threshold per channel (mV), 9 min for everything, ~4 s per channel for all 20 chips. Rewrites the chips: re-applies the GUI file at the end unless `--no-reapply` |
| `noise_probe.py --ext NAME [--ext …] --seconds S --out-dir D [--keep-pcap] [--fec-yaml Y] [--restore]` | GUI apply + acq-on + dumpcap + acq-off + count, **no DAQ run, no HV**; writes `NAME.rates.json` (`hits[chip][ch]`, `live_s`) |
| `vmm_trim.py sdt FILE --out NEW --chips 2,3 --by -6` | move chip thresholds by DAC counts |
| `trimscan_files.py` | files for a single-channel trim scan |
| `pulser_files.py`, `pulser_pedestal.py`, `pulser_commonmode.py` | pulser method (back burner) |
| `noise_ratefit.py fit --map MAP --sdtcal out/sdtcal.json --rates DIR --chips … --out FIT.json` | per-channel σ fit (both centre-fixed and free) from a directory of `*.rates.json`; reads each capture's sdt from its exception file. Robust rules: only points above the channel's highest-rate point; walking up, stop at the first point < 3 Hz or < 30 hits; skip captures with a chip above 1 MHz |
| `noise_ratefit.py fitmaps --pairs RATES:MAP …` | same with a monitor map per point (no DAC calibration) |
| `noise_ratefit.py repeat FIT1 FIT2` | per-channel reproducibility |
| `vmm_monitor_scan.py target …` | thresholds at a noise rate per channel from a rate-scan fit (§10) |
| `rate_tune.py --ext F --map MAP --fit FIT --out-dir D` | closed-loop trim tuning on the measured rate (§10) |
| `fastscan/vmm_only_config.py` | `p2basket_sc_config.py` without warm reset and FEC/hybrid write (run with `/local/p2/deploy/bin/p2basket-python`, cwd `TestBenchCERN`) |
| `step1_c2c3.sh`, `step1b_c2c3.sh`, `step1c_c2c3.sh` | the chains run on 16 Nov (examples of a safe chain: HV check, restore at the end) |

Tests: `cd ~/trim_work/vmm_monitor_tool && /local/p2/p2-readout/venv/bin/python -m pytest -q tests/` (21 pass).

## 4. The fast scan to build and run (agreed with the session `vmm-threshold-efficiency-options`; **user's go needed before running it on all chips**)

**Conditions**: LV on, **HV off** (check `curl -s localhost:5002/hv_data_live`, all < 5 V),
no DAQ run (`pgrep -af 'daq_control.py .*json'` empty), no capture (`pgrep -x dumpcap` empty).
Chips 2–19; H0 (chips 0–1, trigger) left as in the file.

**0. First, a 1-minute check** (answers PLAN §9.3): GUI apply + 10 s capture of
`…step1_c2c3_ratescan_sdtm6.txt` (`noise_probe.py --ext … --seconds 10 --out-dir out/fastscan_DATE/pre --restore`).
Are the 12 chip-3 channels counting again (step 1 values at 18:00:
`out/step1/ratescan/*sdtm6.rates.json`)? Yes → external/environmental; still 0 → a chip
state (then try a LV power cycle only with the user).

**1. Start**
- GUI apply of `p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt`
  (warm reset + full configure; this is the only full apply until the end), link status all 4.
- Monitor map, all chips, `--unmask` (9 min) → `map_start.json` (the baselines of the fit).
- Reference captures (30 s each, GUI apply): `…step1_c2c3_ratescan_sdtm6.txt` (ties to 16 Nov)
  and a new `trimmed_v1` with chips 2–19 at sdt −6 (`vmm_trim.py sdt … --chips 2-19 --by -6`).

**2. Sweep** — one `p2basket-python` process holds the configuration in memory:
- `config-file` base YAML once, apply the `trimmed_v1` exception lines once (code of
  `vmm_only_config.ext_config`).
- for offset in +4, +3, …, −24 (1-count steps, ≈ 0.8 mV), relative to each chip's
  `trimmed_v1` sdt (sdt = 64·`bank1spi2 sdt` + `bank1spi1 sdt`):
  1. set both sdt fields of every active chip, and `sm 1` on every channel masked so far;
  2. `config-vmm-regs` (acquisition off);
  3. `read-link-status`; if any hybrid ≠ 4 → warm reset + full config of this point, log it;
  4. acq-on, capture, acq-off; count with the memory-capped counter
     (`noise_probe.count` → `adaptive_thr_scan.py _count`, `MemoryMax=2G`);
  5. write `point_NN.json`: offset, **sdt per chip, masked channels per chip**, `hits`, `live_s`, links, times;
  6. mask for all later points (never unmask): channels with rate > 10 kHz; channels whose
     rate fell by > ×10 while the threshold went down (comparator stuck); discard the point
     for a chip whose total > 1 MHz;
  7. a chip whose channels are all masked leaves the sweep (sdt back to `trimmed_v1` + 40).
- Capture length: start 5–10 s; extend (up to 30 s) only while some unmasked channel that was
  at 3–100 Hz at the previous point has < 30 hits. Repeated short dumpcaps with acq left on are fine.
- Open points to settle while coding:
  - can `acq-on`/`acq-off`/`read-link-status` be called through `p2basket.exec_cmd` in the
    same process (saves ~5 s per point)? If not, use `p2basket-sc` subprocesses, but **the
    session lock** (`/run/p2basket/session.lock`) is per process: never two slow-control
    processes at once;
  - `noise_ratefit.py fit` reads sdt from exception files: add a path that takes sdt and
    masks from `point_NN.json`, and **skip a channel's points after it was masked** (a masked
    channel counts 0 — that is not "quiet").

**3. End**
- Repeat both reference captures (same files, GUI apply) → drift / noise changes during the scan.
- Monitor map → `map_end.json` (baseline drift; compare with `map_start.json`).
- GUI apply of `Km18_i5` (or the file the user wants), links all 4.

**4. Analysis**
`noise_ratefit` with the centre fixed at `map_start` baselines → σ per channel (mV) with the
number of points and a quality flag; table + per-chip summary; channels whose reference
rates changed between start and end flagged.

**Time**: ~28 points × (4 s write + ~3 s acq/count + 5–20 s capture) ≈ 10–15 min, + 2 maps
(18 min) + references (~3 min) ≈ **35 min**, unattended. Data: a few GB of pcaps at most —
count and delete them point by point (keep only the deepest two for checks). Disk: 102 GB free.

**Before the full run**: dry run (no hardware: print the per-point field changes for two
points), then a 2-chip test (chips 2, 3; compare σ with `out/step1/ratefit3.json` and
`out/step1c/ratefit_repeat3.json`), then ask the user.

## 5. After the scan

1. `target` mode in `vmm_monitor_scan.py`: target_ch = baseline + n·σ; chip sdt at the highest
   target of its normal channels + a few trim steps of headroom; trim = (sdt level − target)/step,
   0..31; outliers (dead, blind, σ > median + 3 MAD) left at the chip level and listed. Then a
   monitor map with the trims in and `refine` towards target_ch.
2. 60 s `noise_probe` of the new file: measured rate per channel vs predicted f0·exp(−n²/2);
   channels far above → `trim_tune.py`; masks only at trim 0. Compare with `trimmed_Km18_v1`.
3. With the user (HV + gas): the same σ scan with HV on (detector noise), then a cosmic run
   with the scintillator trigger (H0 ch 40) against `trimmed_Km18_v1`.
4. Back burner: origin of the ~3 ADC common shift of the pulser method (test: neighbour
   read-out without the pulser, one channel at its noise edge); chip 2 ch 59 ADC = 0.

## 6. Traps

1. **A lab DAQ run sets the Run HV** on the banco supplies before each sub-run: never use a DAQ
   run, `config_scan.py` or `adaptive_thr_scan.py` for these tests. `noise_probe.py` and the
   chains above do acq-on/dumpcap/acq-off without a run.
2. Monitor maps rewrite the chips: configure again afterwards (default) or use `--no-reapply`
   and a GUI apply yourself.
3. A masked channel has no baseline in the monitor (reads ~45 mV): always `map --unmask`.
4. The VMM-only write is **not** a substitute for the first configure (power cycle, reboot, other
   base YAML): start every scan with a GUI apply.
5. One slow-control process at a time (session lock). In this harness: `sleep N; cmd` is
   blocked (use `until …; do sleep 5; done` inside ssh); complex heredocs in Bash are refused
   inside a worktree session (write files with Write, then `scp`); `git` must be run as
   plain commands from the worktree directory.
6. `p2basket.service` is disabled: after a reboot `/run/p2basket` is missing →
   `sudo systemctl start p2basket.service` (user) before any slow control.
7. The 8 GB box: every decode under `systemd-run --user --scope -p MemoryMax=2G`.
8. Push: `git push https://github.com/akallitss/DAQ_Control_VMM_Beam.git worktree-code-map-channel-thresholds`.

## 7. Files left on the box (16 Nov)

- `~/trim_work/out/`: `pulsercheck/` (1.1 GB pcaps), `step1/` (243 MB: map, rate scan, pulser
  pcaps, fits), `step1b/` (127 MB pcaps), `step1c/` (rates, maps, fits), `diag/`.
  The pcaps can be deleted once nobody needs the pulser analysis again (ask the user).
- `~/trim_work/pulser/`: the chain scripts and `base_tp2.yaml` (base YAML with `tp_number 2`).
- `~/trim_work/fastscan/`: the other session's test (copied into the branch).
- `config_ext/`: 34 test files from 16 Nov (`*_cosmic_pulsercheck_*` 4, `*_step1_c2c3_*` 17,
  `*_step1b_c2c3_*` 4, `*_step1c_c3_trimscan_*` 8, `*_diag_c2c3_*` 1) on top of 59 from 13 Nov
  (`*_trimtest_*`, `*_trimA_*`). They clutter the GUI drop-down; moving them needs the user's OK.

## 8. Quick start

```
ssh vmm_daplxa
cd ~/trim_work
curl -s localhost:5002/hv_data_live | python3 -c "import sys,json;d=json.load(sys.stdin);print({k:a[-1] for k,a in d['voltage'].items()})"
curl -s localhost:5002/chip_config/status | python3 -m json.tool | grep -E '"selected"|"rc"|"armed"'
pgrep -x dumpcap; pgrep -af 'daq_control.py .*json'          # both must be empty
cat build/BUILT_AGAINST; /local/p2/deploy/bin/p2basket-sc --version | tail -1   # must match
/local/p2/deploy/bin/p2basket-sc --config-file /local/p2/p2testbench/TestBenchCERN/config_base/p2b-config-cern-base.yaml --read-link-status | grep linkStatus
python3 vmm_monitor_tool/noise_ratefit.py repeat out/step1/ratefit3.json out/step1c/ratefit_repeat3.json   # today's reference result
```

## 9. Result of the fast scan (16 Nov 2026, 20:17–21:51, HV off, no DAQ run)

Driver: `vmm_monitor_tool/fastscan/fast_ratescan.py` (on the box in
`~/trim_work/vmm_monitor_tool/fastscan/`). Fit: `noise_ratefit.py fitpoints` (sdt and masks of
every point from `point_NN.json`; a channel's points while masked are skipped) and `refdiff`
(start vs end reference). Tests: `tests/test_fast_ratescan.py` (24 pass on the box).
Per point: VMM-only write 3.8 s, 15 s live capture (20.3 s with acq on/off), 27–32 s in all.

| run | dir (`~/trim_work/out/fastscan_20261116/`) | what |
|---|---|---|
| test | `test_c2c3/` | chips 2,3, +4…−14, map of 17:48. σ vs step 1: +2.3 % mean, 1.3–2.5 % rms, r 0.95–0.99. The 12 chip-3 channels of PLAN §9.3 count again (8.2 → 8.4 kHz etc.) |
| full (run 1 + resume) | `full/` | chips 2–19, `map_start.json` / `map_end.json`, refs. Points 12–28 of run 1 empty (stall, below) → moved to `stalled_run1/`, resumed from point 11 (`--resume`). 351 channels silent from point 12 on (below) → only +4…−7 for them |
| **full2** | `full2/` | **the result**: same sweep repeated after the channels were brought back; fit with `full/map_start.json` |

**σ (full2, centre fixed at `full/map_start.json`)**: 1105 ok, 13 few, 34 none (never above 3 Hz,
dead or masked from the start). Chip medians [p10..p90], mV:
2: 3.34 [2.37–3.87], 3: 3.23, 4: 3.36, 5: 2.79, 6: 3.41, 7: 2.52, 8: 3.70, 9: 2.78, 10: 2.92,
11: 3.04, 12: 3.31, 13: 3.22, 14: 2.90, 15: 2.57, 16: 3.17, 17: 2.74, 18: 3.23, 19: 3.12.
The p10 of several chips is 1.4–1.8 mV: look at those channels before trusting them in `target`.
**full vs full2** (`noise_ratefit.py repeat`, channels with ≥ 5 points in both): per-channel rms 1.2–6 %
on 14 chips, 10–11 % on chips 8, 9, 16; mean within ±3.5 %; r 0.85 (chip 6) – 1.00.
**Baselines**: `map_end` − `map_start` (24 min apart) = 0 ± 1 mV on every chip (±2 on chips 8, 16).
**References full2** (REF1 start vs end): median ratio 0.90–1.15 per chip, 1 channel changed by > ×2.

**9.1 Read-out stall after a VMM-only write.** At 4 of ~75 VMM-only writes (run 1 point 12,
resume points 15, 26, full2 point 19) the capture had **0 bytes, no packet at all, also not
from H0**, while all ten links read 4. Further VMM-only writes do not clear it (run 1: 17 empty
points, 8 min); a full configure (warm reset + FEC + hybrid + VMM, GUI apply) does, every
time. Not tied to rate: before the stall the total was 2.1, 1.2, 0.65 and 2.0 MHz.
`fast_ratescan.py` now treats a capture < 0.3 MB as a stall: full configure of the point
(file copied to `config_ext/…_fastscan_recover.txt`), capture again, abort if still empty
(+40 s per stall). **For the developers**: what can stop the FEC sending without a link
change, and is there a lighter reset than the warm reset?

**9.2 Channels silent on noise after sitting at/below their baseline.** In run 1 the stall
left ~350 unmasked channels (all of those below 1 kHz at −7) at offsets −8…−24, i.e. at or
below their baseline with the comparator high, for 8 min. From then on they counted
**exactly 0** at thresholds where they had counted 1 Hz–10 kHz, while every other channel
agreed within ~10 % and baselines/thresholds read the same (±1 mV). It survived GUI apply
(warm reset), a monitor map and 30 min. **Test pulses on every channel bring them back**
(diag in `full/diag/`): chips 2–3 pulsed (`…_diag_c2c3_allpulsed_a40.txt`, 10 s) → 42/42 back,
chips 4–19 in the same capture 0/309; then chips 4–19 pulsed in four groups of four
(`…_fastscan_heal_g1..4.txt`, `pulser_files.py --stride 1 --sdp10 40 --unmask`) → 349/351
back (the other 2 had ~0.3 Hz). This is what happened in PLAN §9.3 (deep points / trim scan
→ 12 silent channels; they came back after the all-pulsed test of 19:53).
Rules from it: never leave unmasked channels near/below their baseline (the scan masks at
> 10 kHz and on a ×10 drop, so this only happens when data stop); after any deep test, pulse
all channels once (or check against a reference) before physics data.

**9.3 Burst channels after configures** (developer summary item 1): single channels at
0.3–0.8 MHz appeared after some configures, e.g. chip 4 ch46 526 kHz and **chip 17 ch32
326 kHz** (after the pulse groups), chip 10 at 798 kHz at full2 point 0, chip 10 ch22 ~300 kHz
in both full2 references. The scan masks them at the first point (> 10 kHz); they inflate the
references. Chip 17 had not been seen bursting before.

**9.4 Traps added**: (a) `pgrep -f 'daq_control.py .*json'` or `pgrep -f noise_probe.py`
in a remote shell matches **its own command line** (noise_probe then refuses: "a DAQ run or a
capture is active"); check with a pattern that is not in the command, or in a script.
(b) Give `fast_ratescan.py` the out dir as you like: it is made absolute (the slow-control
scripts run in `TestBenchCERN/`). (c) Files added to `config_ext/`: `…_fastscan_trimmed_v1_sdtm6.txt`
(REF2), `…_fastscan_recover.txt`, `…_fastscan_heal_g1..4.txt` (6). The two deepest pcaps of
each run are kept (`full2/` 97 MB).

**9.5 Next** (§5): `target` mode from `full2/fit_full2.json` + `full/map_start.json`;
channels with quality ≠ ok, or σ < 2 mV, listed and left at the chip level.

## 10. Target mode: every channel at 50 Hz of its own noise (16 Nov 2026, 22:20–22:54, HV off)

Files in `~/trim_work/out/fastscan_20261116/target/`; exception files in `config_ext/`
(`…_cosmic_target50_p1.txt`, `_p2`, `_p2_rt1..3`). **Result: `…_cosmic_target50_p2_rt3.txt`.**

1. **`vmm_monitor_scan.py target`** (from `full/map_start.json` = map of `trimmed_Km18_v1`, and
   `full2/fit_full2.json`). Target per channel = where its **measured** scan curve crosses R = 50 Hz
   (log-linear between the two points around it), not n·σ of the centre-fixed fit: ~150 channels
   have their noise centre 4–9 mV below the monitor baseline, where that fit gives σ 1–2 mV and
   f0 1–25 kHz (wrong model), while the measured crossing is right. Model n·σ only if not
   bracketed (10 channels). Floor 6 mV above the monitor baseline (136 channels). Chip sdt set so
   that the highest-target channel has trim 2; trims rounded towards the higher threshold.
   Targets: chip medians 9.5–14.4 mV above baseline (n ≈ 3.9–4.1 σ); sdt −7…−26 counts.
2. **Monitor map of pass 1** (`map_p1.json`): measured − target = +0.6 mV median, 1.0 mV rms.
   **Pass 2** (`target --keep-sdt` on that map) did not improve it (1.3 mV rms in `map_p2.json`):
   the monitor reads in 1 mV steps, so maps cannot place channels better than ~1 mV, i.e. a
   factor ~5 in rate at 50 Hz. 60 s capture of pass 2 (`noise_ratefit.py checktarget`):
   per-channel median 38 Hz but p90 253 Hz, p99 1.3 kHz, sum 134 kHz vs 41 kHz predicted.
3. **`rate_tune.py`** (closed loop on the measured rate; step per channel from the local slope of
   its own scan curve: raise above 2R, lower below R/4 by ≤ 2 steps, never closer than 6 mV;
   channels > 10 kHz = bursts, not tuned). Converged in one iteration:

   | file | median | p75 | p90 | p99 | channels > 100 Hz | sum (no bursts) |
   |---|---|---|---|---|---|---|
   | `target50_p2` | 38 Hz | 102 | 255 | 1365 | 297 | 133 kHz |
   | `_rt1` | 29 | 49 | 72 | 103 | 19 | 40 kHz |
   | `_rt2` | 30 | 48 | 71 | 99 | 11 | 42 kHz |
   | **`_rt3`** | **30** | **49** | **70** | **102** | 14 | 41 kHz |

   The 11–19 channels above 100 Hz change from capture to capture (noise fluctuation and
   1-step quantisation); further iterations only move them back and forth.
4. **Against `trimmed_Km18_v1`** (same 60 s, `probe/`): its channels are quiet (4.5 kHz in total
   without bursts). `rt3` thresholds are **4.7 mV lower** (median; p10 −10.8, p90 −0.5 mV), at
   12.5 mV above baseline (median; p10 7.1, p90 17.7). Trims 0…31 (2 channels at 0, 2 at 31).
   Masked: chip 16 ch33 only (as in `trimmed_v1`).
5. **Burst channels**, not tuned: chip 10 ch22 (38 kHz in `rt3`; 302 kHz in `trimmed_v1`; seen in
   every capture since 21:33, so this one is persistent) and chip 13 ch4 (109 kHz in the last
   capture). At 300 kHz, chip 10 ch22 alone would come close to the rate guard's
   100 MB/min: mask it for a run if it stays there.

**Next**: with the user, HV on + gas: `rate_tune.py` again with HV (detector noise is added on
top; same tool, start from `rt3`), then a cosmic run with the scintillator trigger against
`trimmed_Km18_v1` (efficiency vs threshold). Before physics data: pulse all channels once (§9.2)
if any deep test was done.

**Stand at handover (16 Nov 2026, ~23:00)**, checked read-only: GUI on
`…_safe_m9c16to21_m4c36to41_h0open_adwk2_Km18_i5.txt` (last apply 22:53:55, rc 0, armed,
not running), no `/run/p2basket/session.lock` (session free), HV 0.5 V max, no
fast_ratescan / rate_tune / noise_probe / vmm_monitor / dumpcap process of ours. Handed over
to session d0ca01 (overnight threshold-above-baseline scan); this session does no more
hardware actions. Files added to `config_ext/` tonight (all `p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_` +):
`fastscan_trimmed_v1_sdtm6`, `fastscan_recover`, `fastscan_heal_g1..4`, `target50_p1`,
`target50_p2`, `target50_p2_rt1..3` (11 files, `.txt`).
Note for the 60 s check of `target50_p2` (§10.2): per-channel median 38 Hz, 54 channels
> 500 Hz, chip 10 ch22 40 kHz (burst); measured/predicted per chip median 1.2–2.7
(`results_20261116/target/check_p2.json`).
