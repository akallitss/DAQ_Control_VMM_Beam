# Handoff — per-channel VMM thresholds (state on 2026-11-13, 06:00)

For the next session. Read this first, then `PER_CHANNEL_THRESHOLDS.md`
(results + how-to) and `CODE_MAP.md` (where everything lives). The memory note
`vmm-slow-control-code-map` holds the same facts in short form.

## 1. What the user wants

Lower the chip thresholds on the Saclay cosmic stand and keep noisy channels
alive by **raising them individually** (per-channel trim) instead of masking
them. "This is very important." Constraints the user set:

- **Do not touch the slow-control developer's code or installation**
  (`/local/p2/deploy`, `/local/p2/p2soft/p2basket-sc`). He is absent and will
  not help. We only link to his library and call his tools.
- Work autonomously; go to the stand in person only when really needed.
- Trims live **inside** each exception file (one file = one chip state).
- Standing rules from memory still apply: keep all chips in the study (never
  drop VMM 4/9), memory-cap every decode on the 8 GB box, chain unattended
  work, docs written for non-experts, the user decides runs.

## 2. What exists and is verified

| Thing | State |
|---|---|
| Trim sets a channel threshold | **verified** by analog measurement: −0.97…−1.25 mV/step per chip, linear, 0..31 |
| Bit order | build `2026-11-05T165959Z-d2cb9ef-dirty-main` sends `sd` un-reversed, the chip needs it reversed. `vmm_trim.py` writes `bit_reverse_5(trim)` (`SD_BIT_REVERSED = True`) |
| `sdt` DAC | 0.73…0.81 mV/count per chip (`~/trim_work/out/sdtcal.json`) |
| Read-back | our `vmm_monitor` (C++, linked to `libp2b-sc.so`): `MeasureVmmChannel` with channel `smx=0` → baseline, `smx=1` → trimmed threshold, mV. 4 s per channel for all 20 chips; full map 9 min |
| Equalisation | `equalize` then `refine`: spread 3.8 → 0.7 mV rms, re-measured |
| Raise instead of mask | data: 244 unmasked channels, 75 hot at +0 steps, 3 at +6, 0 at +12 |
| Tuner | `trim_tune.py` converges in 5–9 captures of 60 s |
| Group script writes the same registers as our program | dry-run packets identical (1420 words, 0 differences) |
| Blind blocks on VMM 4/7/9 (114 channels, baseline above threshold) | cured by per-channel `sl 1`: test pulses seen 0 % → 100 %. VMM 16 ch 33 not cured (masked) |

**Everything above was done with LV on and HV OFF, with no DAQ run.**
Nothing has been checked with HV, gas or particles.

## 3. Where things are

**Git** — repo `DAQ_Control_VMM_Beam`, branch `worktree-code-map-channel-thresholds`
(pushed to GitHub, head `a5cd9a3` + this file), branched from `112519e` = what
the DAQ machine runs (`main`). **Not merged, not deployed.**
Push needs HTTPS: `git push https://github.com/akallitss/DAQ_Control_VMM_Beam.git <branch>`
(no SSH key in the agent; the `gh` credential works).

```
vmm_monitor_tool/
  vmm_monitor.cpp, build.sh     our measurement program; rebuild after every new /local/p2/deploy/install_*
  vmm_trim.py                   ext-file editing: summary trims set shift raise pedestal bit sdt apply-table
  vmm_monitor_scan.py           map trimcurve sdtcal equalize refine report   (runs on the DAQ machine)
  noise_probe.py                60 s capture with a file: GUI apply, acq-on, dumpcap, acq-off, count. NO run, NO HV
  trim_tune.py                  loop of noise_probe: raise channels above 50 Hz until clean
  tests/                        16 tests; run with /local/p2/p2-readout/venv/bin/python -m pytest (no pytest on the workstation)
quick_scripts/adaptive_thr_scan.py   new option --raise-steps N (raise before masking); unit-tested, NEVER run on the stand
docs/PER_CHANNEL_THRESHOLDS.md  results + how-to        docs/CODE_MAP.md  layers, grammar, facts
docs/PLAN_PER_CHANNEL_THRESHOLDS.md  the original plan (historical; steps 1-4 done)
```

**DAQ machine** (`ssh vmm_daplxa`, user ak271430):

- `~/trim_work/vmm_monitor_tool/` — copy of the tools actually used (scp'd, keep in sync by hand).
- `~/trim_work/build/vmm_monitor` (+ `BUILT_AGAINST`) — the binary.
- `~/trim_work/out/` — every result: `map_*.json`, `trimcurve_ch5_40.json`, `sdtcal.json`,
  `eq_*.json`, `probe1/`, `probe_pulse*/`, `tune/*.json`, `displaced_channels.json`, `map_trimmed_v1.json`.
- `/local/p2/p2testbench/TestBenchCERN/config_ext/`:
  - **`p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt`** — the deliverable
    (wk2 K=−18 thresholds, equalised, `sl 1` on 114 channels, 135 channels raised, 1 mask).
  - 52 test files `*_cosmic_trimtest_*` and `*_cosmic_trimA_*` (kept; they clutter the GUI drop-down).

**Site** (`~/Documents/PostDocSaclay/cern-site`, deployed, committed locally, not pushed to GitHub):
`notes/2026-11-13-vmm-thresholds-per-channel-measured-equalised-raised-instead-of-masked.html`
and the updated `notes/2026-11-12-vmm-daq-code-map-from-the-browser-to-the-chip.html`.

## 4. State of the stand as left

- GUI selection and chips: the original `…_adwk2_Km18_i5.txt`, applied 05:48, run gate armed.
- HV: never touched (was off). LV on. No run started, run counter unchanged.
- `/run/p2basket` exists (user started `p2basket.service`; it is still **disabled**, so it
  disappears at the next reboot → `sudo systemctl start p2basket.service`).
- Disk 103 GB free; all probe pcaps were deleted after counting.

## 5. Traps (each one cost time or could cost a detector)

1. **A lab DAQ run sets the Run HV** (420/720 V…, GUI Setup tab) on the banco supplies before
   each sub-run. Never start a run, `config_scan.py` or `adaptive_thr_scan.py` for a test without
   the user's go on HV and gas. `noise_probe.py` / `trim_tune.py` avoid runs for this reason.
2. Any **measurement rewrites the VMM registers** and leaves the monitor selection changed.
   `vmm_monitor_scan.py` re-applies through the GUI at the end unless `--no-reapply`; after a
   `--no-reapply` session, do a Configure chip yourself.
3. A **masked channel has no baseline** (reads ~45 mV): always `map --unmask`.
4. The developer's `MeasureChannelThreshold` returns 0. Do not use it.
5. The binding accepts `sd = 32` silently (stores 0). Only write trims through `vmm_trim.py`.
6. Slow control is single-user (session lock): never run two of our tools at once; wait for
   background jobs (`until grep -q … ; do sleep 5; done`).
7. In this harness: a Bash call containing a complex heredoc is refused inside a worktree
   session (write the file with Write/Edit, then run it), and `sleep N; cmd` is blocked.
8. Tuned file names grow with each chained step; copy the result to a short name.
9. The 8 GB box: decodes go through `systemd-run --user --scope -p MemoryMax=2G` (the tools do).

## 6. Open items, in the order I would take them

1. **User decisions pending** (asked, not answered yet):
   a. take a cosmic run with `trimmed_Km18_v1` and HV on;
   b. merge the branch and deploy (Git Reset on the DAQ machine), so `vmm_monitor_tool/` and
      `--raise-steps` are in `/local/p2/DAQ_Control_VMM_Beam`;
   c. move the 52 test files out of `config_ext/`.
2. **First HV-on check** (needs the user: HV + gas): do the 114 recovered channels (`sl 1`)
   behave like their neighbours with real signals — rates, ADC spectra, efficiency if the
   scintillator trigger is on. Compare per-channel rates and ADC edge with
   `cosmic_vmm_analysis/adc_edge_vs_sdt.py`, `baseline_per_vmm.py`. Until then `sl 1` is a
   test-pulse result only.
3. **Run `adaptive_thr_scan.py --raise-steps 2 --base …trimmed_Km18_v1.txt`** on the stand
   (first `--dry-run`, then a 2-point scan). It is the in-run version of the tuner and handles
   noise that only appears with HV. After deploy, its `vmm_trim` import path is
   `../vmm_monitor_tool` relative to `quick_scripts/`.
4. **Understand the blind blocks**: which pads/connector pins are VMM 4 ch 36-63, VMM 7 ch 23-63,
   VMM 9 ch 19-63 (`p2-mapping`, `/local/p2/p2-setups/lab_saclay_2026.yaml`); does the baseline
   shift change with HV on; is it the same with the detector cable unplugged (needs hands).
   Known: scales with gain, gone with `sfm 0`, `slg 1`, `sl 1`, `sp 1`, `sg 0`; chips 2 and 13
   at `sfm 1` are fine. `sfm 0` on the chip floods (not retuned) — an alternative not explored.
5. **Smaller things**: VMM 10 ch 59 has a low baseline (104 mV → 76 mV below threshold; could be
   lowered by trim only 33 mV); tuner with `--steps 1` for a finer result; equalise at other
   gain/peaking-time settings (the trim curve and maps were taken at sg 4.5 mV/fC, 200 ns);
   H0 (trigger hybrid, chips 0-1) was never trimmed, on purpose.
6. **GUI panel** (old placeholder in memory `vmm-daq-lab-mode`): summary of the selected file
   (`vmm_trim.py summary --json`), map plot, start a map/tune as a background job sharing
   `ChipConfig`'s lock. Only after the terminal workflow has been used with HV.
7. **Robustness**: a check at tool start that `BUILT_AGAINST` equals `p2basket-sc --version`
   (refuse otherwise), and a `trimcurve` on 2 channels as a bit-order self-test after any new
   slow-control install.

## 7. Quick start for the next session

```
ssh vmm_daplxa
cd ~/trim_work
cat vmm_monitor_tool/../build/BUILT_AGAINST; /local/p2/deploy/bin/p2basket-sc --version   # must match, else vmm_monitor_tool/build.sh ~/trim_work/build
curl -s localhost:5002/chip_config/status | python3 -m json.tool | head -20                # what is selected / applied / armed
pgrep -x dumpcap; pgrep -af daq_control.py                                                 # nothing may be running
python3 vmm_monitor_tool/vmm_trim.py summary /local/p2/p2testbench/TestBenchCERN/config_ext/p2b-config-cern-ext_gain4.5_peaktime200_opt_cosmic_trimmed_Km18_v1.txt
python3 vmm_monitor_tool/vmm_monitor_scan.py report out/map_trimmed_v1.json
```
