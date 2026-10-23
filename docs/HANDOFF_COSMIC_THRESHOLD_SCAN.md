# Handoff: cosmic-muon VMM threshold scan on the three-chamber stand (vmm_daplxa)

Written 2026-10-22. Read with `TODO_LAB.txt` (machine checklist for `SITE='lab'`), which this builds on.

## 1. Why this measurement

The July beam analysis (P2_basket_analysis `sps_beam_analysis/vmm_threshold_tradeoff/`, note
https://alexandra-kallitsopoulou.web.cern.ch/notes/unlisted/2026-10-21-testing-the-vmm-threshold-theory-step-by-step.html)
concluded:

- VMM efficiency (~85 %) is below DREAM (95.6 %) because the VMM threshold is too high for the signal,
  not because of the chambers.
- **The beam efficiency was never measured as a function of threshold.** It was predicted from DREAM spectra.
  The noise side came from a 2 s/point bench scan (29–30 Jul, `TestBenchCERN/config_scan/`, sdt 210–300).
- **Lowering the global sdt with a few masked channels predicts P2_OUT ~91 % (model).**
  Between-spill hits in run_46 show the same noisy channels, but ~100× hotter in the beam hall.
  The noise onset there is 5–10 DAC higher, so ~85–88 % is realistic. 4.5 mV/fC beats 3.0 everywhere.

The cosmic scan measures what the beam could only model:

1. **Efficiency vs sdt per chip**, directly, by tag-and-probe between the three chambers.
2. **Per-channel noise vs sdt, at HV**, from the same runs (hits not on a track), with long enough runs to see ~1 Hz.
3. **The chip's own signal spectrum** at the lowest thresholds, so efficiency(sdt) = fraction above threshold can be checked
   without DREAM.

It **cannot** give the beam-hall noise (environment-dependent, see above) or an absolute number comparable to the DREAM
95.6 % (cosmic angles and charge sharing make a different spectrum). The goal is the *shape*: where the efficiency
plateaus, and where the noise starts, per chip.

## 2. The stand as set up (2026-10-22, from Alexandra)

| position | chamber | hybrids / VMMs | mesh / drift | HV supply |
|---|---|---|---|---|
| P2_IN  | **det3** | H1 H2 H3 → VMM 2–7   | 410 / 710 V | CAEN 4-ch module: ch0 drift, ch1 mesh |
| P2_MID | **det2** | H4 H5 H6 → VMM 8–13  | 410 / 710 V | rack HV module: ch0 mesh, ch1 drift |
| P2_OUT | **det1** | H7 H8 H9 → VMM 14–19 | 410 / 710 V | CAEN 4-ch module: ch3 drift, ch4 mesh |

- **HV is fixed and not under DAQ control or logging.** Write V and I for every channel in the logbook at the start and
  end of each run. At this site `hv_targets: False`, so the run configs carry no voltages. Put the values in the run
  notes, or in `LAB_SOURCE` / the run comment.
- **Gas: Ar/isobutane 95/5.** Recorded as `'Ar/Iso 95/5'`, which is already the `self.gas` default in
  `run_config_beam.py` on main. Alexandra corrects it there if the bottle turns out different. The beam runs up to run_58 (including run_46) were on Ar/CO2/iC4H10 93/5/2;
  from 2 Aug, Ar/CF4/iC4H10 88/10/2. So the gain at 410 V is not the beam gain. Compare with the beam through the
  measured spectra, never through HV.
- **The chambers moved, the electronics did not.** In beam MID was det1, OUT det3 (IN det2/det4/det5 by run). H1–H9 keep
  their positions.
  - **Free test:** if the same VMM channels are noisy again (VMM 19 ch 20, VMM 14 ch 34, VMM 11 ch 10, …), the noise
    belongs to the hybrid, not the chamber.
- **Check det3 first.** It had the July gain collapse and the drift-foil HV decoupling on the bench. Look at its ADC
  spectrum in the first cosmic run before trusting P2_IN numbers.
- **Cables: record them per connector.** The beam reference (run_46) was all HITACHI 2 m. Only Samtec is available now,
  and the cable study showed cable length changes the noise. Update `P2_VMM_CABLES` / `CABLING_NOTE` in
  `run_config_beam.py` so it ends up in every run_config.json.
- **The trigger hybrid H0 is not needed.** Cosmics are self-triggered; H0 stays at sdt 1023 as in the configs.

## 3. Repo and machine state found 2026-10-22 (read-only check)

- **The lab-site code is only on the laptop.** Branch `lab-site` (3 commits: `ec3fcfc` SITE='lab', `add936f`,
  `e40a4dc`) is on Alexandra's laptop and **not pushed**. GitHub main = `bd2beb3`.
- **The machine checkout is behind.** `/local/p2/DAQ_Control_VMM_Beam` (= `~/DAQ_Control_VMM_Beam`, symlink) is at
  `d2a58f3`, 3 commits behind main, `config/site.txt = sps`.
- **The machine checkout has uncommitted beam-time edits to `run_config_beam.py`** (+112/−8: the `GAS_MIXTURE`
  constant = 'Ar/CF4/Iso 88/10/2', and `RUN_PLAN='config_scan'`), plus untracked `beam_run_log.py` and two
  `processor_*_backfill.json`.
  - **Commit them on a branch as the beam-test record before pulling.** They contain the only written statement
    that runs 1–58 are mislabelled 'Ar/Iso 95/5' in their run_config.json.
- **An idle `sps` stack has been running since 14 Sep:** tmux `vmm_*` sessions, nothing capturing.
- **Disk: `/local/p2` 85 % full, 138 GB free.** At low thresholds the noise alone can reach several GB/h, see §6.
- **The lab site points at the wrong slow-control config.** `SITES['lab']` uses TestBenchCosmics with
  `config_alinx_noThresholds_hyb2.json`. For this stand use **the same chain as the beam test** (same hybrids, same
  ALINX):
  - `sc_cwd = /local/p2/p2testbench/TestBenchCERN`
  - `sc_config = config_base/p2b-config-cern-base.yaml`
  - the CERN `config/chip_config.json` (base `config_base/`, ext `config_ext/`, apply script
    `TestBenchCERN/p2basket_sc_config.py`)

  Then the chip configs are byte-for-byte the beam ones apart from what we change. Verify first with
  `p2basket-sc ... --read-link-status`: H1–H9 must all be ready.

**Done 2026-10-22:** `lab-site` pushed and merged into main; the machine's beam-time edits committed on the branch
`beam-tb-july26-final` on the machine (see §4.1); gas = Ar/Iso 95/5.

## 4. Setup steps (in order)

1. **Update the code.**
   - Laptop: `lab-site` pushed and merged into main (done 2026-10-22).
   - Machine: the beam-time edits are committed on `beam-tb-july26-final` (`a46c1cf`, `942e31f`, pushed to GitHub), and
     the machine is still on that branch. The two `config/processor_*_backfill.json` stay untracked (machine state).
   - Still to do: `git checkout main && git pull` on the machine, **after** stopping the idle sps stack.
2. **Edit `run_config_beam.py` (lab site).**
   - Gas: leave `self.gas = 'Ar/Iso 95/5'`. Do **not** carry over the machine branch's `GAS_MIXTURE = 'Ar/CF4/Iso 88/10/2'`.
   - `LAB_MEASUREMENT = 'cosmics'`.
   - `base_data_dir` = a new campaign dir, e.g. `/local/p2/p2data/cosmics_3det_202610/`.
   - The interface `sc_cwd`/`sc_config` as in §3.
   - Cables per connector.
   - The chamber↔station mapping in the detector list: IN = det3, MID = det2, OUT = det1.
   - Commit.
3. **Chip config: keep the CERN one.** Point `config/chip_config.json` (or a `chip_config.lab.json`) at TestBenchCERN.
4. **Switch the site.**
   - `echo lab > config/site.txt`
   - `.venv/bin/python run_config_beam.py`: read the summary.
   - Then `qa_config.py` and `vmm_processor_config.py`, per `TODO_LAB.txt` step 7.
5. **Restart the stack.** Stop the idle sps stack, then run `./start_servers.sh`.
   - GUI: `ssh -f -N -L 15002:localhost:5002 vmm_daplxa`, then open `localhost:15002`. The badge must read LAB.
6. **Smoke test** (`TODO_LAB.txt` step 9). Warm reset → apply `p2b-config-cern-ext_gain4.5_peaktime200_opt.txt` → 2 min run.
   - Files must not be 272 bytes.
   - All of VMM 2–19 must appear in the QA.
7. **EOS backup.** Configure `backup_config.py` for the campaign dir, plus `prune_backed_up.py`. Without it, 138 GB can
   go in one or two nights at low thresholds.

## 5. Measurement plan

### 5.0 vmmsc calibration first (~1 h)

Measure the per-channel pedestals and threshold dispersion with vmmsc on H1–H9 at 4.5 mV/fC, 200 ns, with the chambers
connected and at HV. Then compute the trim values (target ~ the trimmed σ 0.3 mV of the Nov 2025 calibration).

- **This replaces the inferred chip baselines** in §5.2 (VMM 4/7/9 extrapolated, VMM 6 unknown).
- The only existing calibration (`p2soft/_install/data/20251103/3mVfC_200ns_60ns_negative/`) is on other chambers, at 3 mV/fC.

### 5.1 Noise scan (~1–2 h)

| setting | value |
|---|---|
| chip config | the July `_opt` file with only the sdt lines changed (§5.3) |
| HV | on |
| run length | 2 min per point (not 200 packets: that is what limited the July scan to ≥ ~0.5 Hz) |
| sdt per chip | baseline + d, for d = 50, 40, 34, 30, 27, 24, 22, 20, 18, 16 DAC |

Why baseline-relative steps and not a common sdt:

- **A common sdt can flood the readout.** The chip baselines span sdt 180–212, so a common sdt of 200 is *below* the
  baseline of VMM 2, 5, 7, 10, 11, 12, 14 and 19. Those chips then fire continuously, which floods the link and fills the disk.
- **The analysis uses d anyway**: the efficiency model is in DAC above baseline.
- **Stop a chip at the first d where it turns noisy as a whole.** The July chip-wide noise floor was 17–20 VMM ADC above
  baseline at 4.5 mV/fC, i.e. ~19–22 DAC. Do not go further.
- **This scan sets each chip's lowest d for the cosmic scan.** It also gives the channel-level onset map to compare with
  the July bench scan, the "electronics or chamber" test.

### 5.2 Chip baselines to start from (sdt DAC, from July beam ADC edges)

```
VMM  2:208  3:182  4:199*  5:212  6:???  7:200*  8:197  9:191*  10:210  11:204
VMM 12:210 13:188 14:201  15:180 16:199 17:187 18:182  19:204
* extrapolated from sdt 285-300 runs; VMM 6 had an ADC edge ~10, baseline unknown (start it at sdt 230 and step down
  in 5s while watching its rate). Replace all of these with the vmmsc pedestals of 5.0.
  Systematic uncertainty ±6-8 DAC per chip (shared 38 ADC baseline and 0.9 slope); see Part 4 of the note.
```

July settings for reference: chips 2, 3, 6, 8, 10, 11 and 13–18 at 230; 4 at 300; 5 at 245; 7 at 285; 9 at 300;
12 at 255; 19 at 265.

### 5.3 Config files

Start from `TestBenchCERN/config_ext/p2b-config-cern-ext_gain4.5_peaktime200_opt.txt` (the run_52 config), and change
**only** the sdt lines for Hyb 1–9.

- Chip v → `Hyb v//2 Vmm v%2`.
- Write `Reg bank1spi1 Field sdt (sdt % 64)` and `Reg bank1spi2 Field sdt (sdt // 64)`, so sdt = spi2·64 + spi1. Never
  trust the `#` comments.
- Name them `p2b-config-cern-ext_gain4.5_peaktime200_cosmic_d<d>.txt` and put them in `config_ext/`. The run is named
  after the file applied, so the name is the label.
- Add `sm 1` mask lines **only** for channels that the noise scan shows above ~1 kHz at that d. VMM 4/7/9 have a
  "walking burst" (the hot channel hops), so masks may not hold there.
- **A small generator script belongs in `quick_scripts/`.** It reads the baselines, the d list and the mask list and
  writes the files. Check one output against the `_opt` file with `diff`: only sdt/sm lines may differ.

### 5.4 Cosmic reference run (overnight)

`_opt` config (July settings), `RUN_PLAN='lab_source'`, e.g. 16 × 30 min. It gives:

- the actual triple-coincidence rate per chip (expected order: ~40 tracks/min per chip; the acceptance depends on the
  stack spacing and how the VMM-read regions overlap);
- alignment and time windows from the tracks themselves;
- a per-pad efficiency map;
- det3 health;
- the data volume per hour at a safe threshold.

**Measure and write down the layer spacings and lateral offsets of the stack before this run.**

### 5.5 Cosmic threshold scan, untrimmed (~1 night + 1 day)

- `RUN_PLAN='config_scan'`, driven by `config_scan.py`, one run per `_cosmic_d<d>` file.
- **~1 h per point** gives roughly ±1 % per chip (refine from 5.4).
- Points: d = 50, 40, 32, 27, 24, 22, 20, (18): 7–8 points, each chip stopping at its noise limit from 5.1.
- All three chambers are scanned together. Tag-and-probe stays unbiased as long as the tags are real muons, which a
  time + geometry coincidence in two chambers guarantees.
- Add **one cross-check point**: the probe chamber at low d while the other two stay at d = 40.

### 5.6 The same scan with trims (~1 night)

Same points with the vmmsc trims loaded. This measures the ~+2 pts the July model gives trimming.

### 5.7 Optional

One point each at 3.0 and 9 mV/fC at the best d. This tests that the noise does not scale with gain (the argument for
4.5 or higher).

## 6. Code change needed before 5.5: `config_scan.py` is beam-coupled

As it stands it:

- waits for Dream to be idle (`dream_idle`, `DREAM_FLASK`);
- starts **combined** runs (`req('/run_config_py', {'dream': True})`);
- samples the SPS beam (`beam_sample()` via the Dream flask);
- writes `BEAM_QUALITY.json` from Dream trigger counts.

At a lab site, read `IS_BEAM_SITE` from `run_config_beam` and:

1. skip the Dream-idle wait;
2. start with `{'dream': False}`;
3. skip `beam_sample()`;
4. write a `RUN_QUALITY.json` from the VMM's own counters instead (hits per VMM from `get_run_events.py`, capture
   bytes, files);
5. keep the free-disk refusal (`MIN_FREE_GB`) and the `--until` logic as they are.

The HV hold (`config/hv_hold`) is irrelevant here (no HV under control) and must not block.

Test with `--dry-run`, then with two configs × 5 min, before an overnight scan.

**Disk guard.** Data volume ≈ 6.2 bytes per hit (run_46: 9.4 GB for 1.52 × 10⁹ hits), so 150 kHz of hits ≈ 3.3 GB/h.
That is the July bench rate at sdt 210, all chips together; below that it climbs fast. With 138 GB free, set `MIN_FREE_GB` so a point that cannot finish is not started, and have the EOS backup + prune running.

## 7. Analysis (lives in P2_basket_analysis, not here)

- **Decode:** `sps_beam_analysis/vmm_dream_matching/extract_vmm_triggers.py` `iter_columns(sub_dir, source='pcapng')`
  with `P2_basket_online_analysis/vmm_decode.py`. Time = `srs_timestamp·22.5 + (offset·4096 + bcid)·22.5 + TDC`
  (22.5 ns tick, not 25).
- **Noise per channel per point:** `vmm_threshold_tradeoff/oos/oos_bin.py` bins hits per (vmm, ch) in 0.1 s. Without
  spills, every bin is "out of spill"; subtract the track-associated hits.
- **Tag-and-probe (to write):**
  - clusters per station within ±100 ns;
  - a tag = clusters in two stations consistent in time and position;
  - the probe window = a 3×3 pads box around the line through the tags (smaller for the middle station);
  - efficiency per chip and per pad vs d.
  - Pad geometry: `pad_cx/cy` of the P2 BASKET fan (1280 pads, 12.07 × 11.61 mm); VMM → pad via the reverse ordering
    of `vmm_detector_efficiency.py`.
  - The beam 3D-coincidence code (FEU 3/4/5 = IN/MID/OUT, handedness trap) is the starting point.
- **Then** redo the Part 3 trade-off with the measured efficiency and noise curves (`vmm_threshold_tradeoff/tradeoff.py`
  takes onsets per channel and an efficiency model; replace the model by the measured curve).

## 8. Record for every run (logbook)

Date/time, config file applied, V and I of all six HV channels (start/end), gas + bottle, stack spacing, cables per
connector, anything touched on the stand. A field that is written and wrong is worse than one that is missing; see the
run 1–58 gas label.
