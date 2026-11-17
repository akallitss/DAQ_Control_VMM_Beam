# Efficiency check of the 16/17 Nov night runs (231-235), 17 Nov 2026

Question: can the single-channel efficiency (`cosmic_vmm_analysis/channel_efficiency.py`, method in
`CHANNEL_EFFICIENCY.md`, P2_basket_analysis branch `eff-vs-threshold-distance`) be run on the HV-on cosmics runs
of the night? Answer: yes, with the preparation below. **The codified algorithm has NOT been run yet.** All
numbers here come from a quick chamber-level check (`cosmic_vmm_analysis/scint_chamber_check.py`, same branch),
which uses no pad map and no tracks.

## Runs

| run | config | length | note |
|---|---|---|---|
| 231 | `bscan_hvon50_v1_hvm` | 30 min | good |
| 232 | `trimmed_Km18_v1_hvm` | 30 min | good |
| 233 | `bscan_hvon50_v1_hvm` | 30 min | read-out fault from 06:25:30, **not usable** |
| 234 | `trimmed_Km18_v1_hvm` | 30 min | good |
| 235 | `bscan_hvon50_v1_hvm` | 20 min | good |

The day ladder (`HANDOFF_FAST_RATESCAN.md` section 12, run_236 on: R = 2 ... 50 Hz files plus the `trimmed_Km18_v1_hvm`
reference, 50 min each) is the data set for efficiency against threshold. The night runs give a per-chamber
number for each of two files.

## Conditions compared with wk2 (3-5 Nov, runs 181-225)

- **Chamber HV: identical** (`hv_monitor.csv` of runs 225, 231, 232, 234, 235): IN 720 drift / 420 mesh, MID and OUT
  730 / 430. Only difference: the OUT mesh current reached 0.2-0.6 uA in parts of runs 232/234 (0.04 uA in wk2).
- **Scintillator HV raised** (user, to get a higher scintillator efficiency). The H0 lines of the ext files are
  byte-identical to wk2's, so `--scint-ch 0:40` and the 140-310 ns window still apply. ch40 rate 0.25 -> 1.0 Hz,
  ch41 12 -> 320 Hz. This changes **which** muons are triggered, as well as how many.
- **Thresholds**: wk2 = untrimmed K scan; tonight = two trimmed files. `bscan_hvon50_v1_hvm` against
  `trimmed_Km18_v1_hvm`: chip sdt 6-21 counts lower (about 4.5-16 mV at 0.75 mV/count), but the trims differ on
  56-64 channels of every chip and partly compensate. Per channel above its own baseline: bscan chip medians
  10.6-19.1 mV (handoff section 11), trimmed_Km18_v1 about 17 mV median (rt3 is 4.7 mV below it at 12.5 mV, section 10).
  The two files therefore overlap a lot. Most of bscan's 3x higher hit rate (165 against 52 kHz) comes from a tail
  of noisy channels. Masks: 16 (bscan) against 9 (trimmed) `sm 1` lines.

## Quick chamber-level check

`scint_chamber_check.py`, run on the DAQ box: for each ch40 hit, is there any hit in each chamber 140-310 ns later
(on-time), or 4140-4310 ns later (off-time = random)? The table gives P(probe chamber fired | scint and the other
two chambers fired), with binomial errors of about +-0.02 (tonight) and +-0.025-0.03 (wk2).

| run | config | files (min) | ch40 | triples/min | IN | MID | OUT |
|---|---|---|---|---|---|---|---|
| 196 (wk2) | K0 | 60 (67) | 0.25 Hz | 3.4 | 0.68 | 0.75 | 0.67 |
| 225 (wk2) | Km12 | 60 (67) | 0.27 Hz | 2.9 | 0.71 | 0.69 | 0.62 |
| 231 | bscan_hvon50_v1 | 15 (17) | 1.01 Hz | 18.3 | 0.75 | 0.89 | 0.73 |
| 232 | trimmed_Km18_v1 | 15 (17) | 1.04 Hz | 19.5 | 0.76 | 0.90 | 0.74 |
| 234 | trimmed_Km18_v1 | 15 (17) | 1.04 Hz | 19.6 | 0.75 | 0.90 | 0.75 |
| 235 | bscan_hvon50_v1 | 15 (17) | 1.04 Hz | 19.7 | 0.77 | 0.90 | 0.72 |

The off-time window gives about 0 everywhere, so random coincidences don't matter here.

Correction: the first quick look (one file per run) printed the IN and OUT columns swapped, and its single wk2 file
had only ~17 scintillator hits. Its numbers are superseded by this table.

### What this check can and cannot tell

1. **It is not the efficiency.** "Any hit in the chamber" saturates (a muon fires several pads), so a few mV of
   threshold does not show. That is why runs 231/235 and 232/234 agree.
2. **The outer chambers contain the read-out acceptance (extrapolation).** For MID, a muon through IN and OUT is
   bracketed and nearly always crosses MID's read-out region (interpolation). For IN and OUT, a muon through the
   other two is extrapolated by 310 mm and can leave the read-out footprint (only connectors c3-c5 are read out),
   so IN/OUT = efficiency x acceptance < efficiency. The codified algorithm does not have this problem: it only
   uses tags predicted on live pads (density counted in pad cells, pads near masked/dead ones dropped). With it,
   wk2 gave IN 0.75 / MID 0.75 / OUT 0.79, i.e. no outer deficit.
3. **The MID uplift (about 0.72 -> 0.90) is not explained yet.** Candidate causes, which the chamber-level check
   cannot separate:
   - the trimmed/equalised files against the untrimmed wk2 K files (the VMM 4/7/9 high-baseline "blind" blocks;
     VMM 9 is on MID, VMM 4/7 on IN);
   - the new scintillator HV changing the triggered population (angles, paddle edges);
   - since the outer/MID gap grew (wk2 about 0.05, now about 0.15), a larger acceptance loss in the outer chambers
     with the new trigger.

## Preparation for `channel_efficiency.py` on these runs (and the day ladder)

1. **Threshold table**: the default `thr-table` (reference map shifted by sdt) is wrong for trimmed files. Give
   `--run-map` per run: `trimmed_Km18_v1` -> `~/trim_work/out/fastscan_20261116/full/map_start.json` (HV off, 16 Nov);
   the bscan files -> per-channel mV above baseline from `~/trim_work/out/bscan_20261116/hvon{R}_v1_report.json`
   (needs a conversion to the map format), or a measured monitor map.
2. **Memory**: `efficiency_tp.py` loads all hits of a run. A bscan run of 30 min is about 300M hits (wk2 runs about
   15M), over the `--mem 10G` cap. Dump in chunks (pseudo-run directories of about 5 capture files).
3. **K label**: `run_info` parses `_K..` from the file name, so these files give `K=None`. Check that `measure`
   handles it, or label runs by R.
4. **Data location**: on the DAQ box only (`/local/p2/p2data/cosmics_3det_202610/runs/`), not on EOS.
5. Optional: `spark_veto.py` for the two one-sample OUT discharges (05:11, 05:55).
6. Statistics: about 19 scintillator triples/min. The night gives about 2k tags (10 % of wk2's 26.7k); the day ladder
   gives about 1k per 50-min run.

Expected from wk2 (pad level): flat at about 0.75-0.8 from 20 to 45 mV above baseline; nothing measured below 18 mV.
Both night files sit around 11-19 mV, so only small differences between them are expected even with the codified
algorithm. The ladder (R = 2 ... 50) spans a wider range.
