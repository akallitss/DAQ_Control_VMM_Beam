# Handoff — fast noise rate scan of all chips (state at the end of 16 Nov 2026)

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
5. **Nothing is running.** The stand is on `…_adwk2_Km18_i5.txt` (GUI apply rc 0, armed),
   HV off (< 0.5 V on all six channels), LV on. The user decides when the scan runs.

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
