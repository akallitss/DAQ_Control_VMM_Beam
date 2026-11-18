# Handoff: weekend data taking on the lab cosmic stand, Thu 19 Nov -> Mon 23 Nov 2026

**For the session that starts the runs.** You are attended (the user can answer, Claude stays connected) until
**Thu 19 Nov 08:00**; after that everything must run **unattended until Monday 23 Nov 07:00** with nobody connected,
as on 16-18 Nov. Nothing of this plan has been started: the stand was left with HV OFF (guard ramp-down 18 Nov 23:15),
LV on, the last chip configuration of run_267 applied, no script running (checked 19 Nov 00:26).

Why these data: read `P2_basket_analysis/cosmic_vmm_analysis/FINDINGS_diag_20261118.md`. In one paragraph: with HV on,
the single-channel VMM efficiency is ~0.85-0.90 and moves with neither the threshold nor the gain (4.5 / 6 / 9 mV/fC);
sparks do not bias it; about 10 % of the muons cross a chamber that records nothing at all (24-26 % on chips 2 and 14).
The weekend has three goals:

1. **Pulser runs**: one channel per chip pulsed at 10.85 kHz during cosmics -> read-out efficiency measured directly,
   and whether the pulses go missing when a chamber is "silent" for a muon.
2. **2D gain x threshold scan with high statistics**: gains 4.5 / 6 / 9 mV/fC x thresholds at the per-channel HV-on
   noise rate R = 2 / 10 / 50 Hz. The deliverable (user): at high gain the low tail of the muon ADC distribution sits
   above the threshold, while the efficiency does not change -> gain is not what limits the efficiency.
3. **High-statistics blocks** on the reference (trimmed_Km18) and on the high-gain point (9 mV/fC, R = 10 Hz).

The data are taken in **overlapping passes**: every pass covers the whole grid once, in a different order, so if the
weekend ends early (gas, a crash, a trip) every point still has equal statistics. Analysis runs **in parallel on
lxplus** as the runs are copied to EOS (never on the DAQ box).

---------------------------------------------------------------------------------------------------------------

## 0. Rules (from memory; they cost us before)

- The user decides the measurement; this plan is approved in outline (19 Nov 00:40). Ask before deviating.
- **Never start a lab run without checking HV**; HV changes only through `hv_lab.py` (banco API).
  Limits: Drift <= 1000 V, Mesh <= 500 V. Operating point: IN 420/720, MID 430/730, OUT 430/730 (mesh/drift).
- Keep all chips in the study (no masking or skipping a chip without asking). Hot channels are masked only by
  cosmics_prep's rate check, as on 17-18 Nov.
- Every test command on the box under a memory cap (`systemd-run --user --scope -p MemoryMax=...`); the box has 7 GB.
- Chain unattended steps in one script (a lost overnight run cost ~22 h on 23 Oct); write logbook notes with
  `lab_logbook.py note`.
- `/proc` is `hidepid`: use sudo to see root/boon processes. Nothing on the box autostarts.
- `config_scan.py --until` takes **HH:MM within the next 24 h only** -> one config_scan call per pass (below).
- config_scan stops when free disk < 60 GB (`MIN_FREE_GB`); the pruner must stay ABOVE that: it runs as
  `prune_backed_up.py --low 70 --target 90` (since 18 Nov 08:00). This stopped the cosmics for 2 h on 18 Nov.
- Commit scripts to `DAQ_Control_VMM_Beam` main (vmm_monitor_tool/fastscan/), deploy by copying to
  `~/trim_work/vmm_monitor_tool/...`; one commit per file.

## 1. Decisions to get from the user before 08:00 (ask at the start)

1. **Gas**: is the bottle good until Monday 07:00 (~4 days of flow)? (wk1 ran out of gas on 1 Nov.) If not: end date.
2. **HV trip policy over 4 days**: the user's rule so far is "trip -> that detector off" (`--on-trip detector-off`).
   Over 4 days that can lose a chamber for days. Option: `--on-trip retry --retries 3` (re-ramp up to 3 times, then
   off). Default if no answer: keep `detector-off`.
3. **Gain 3.0 mV/fC** (optional, adds a low-gain contrast point where the threshold should cut the spectrum): only if
   the prerequisites below are done by ~05:30. Default if no answer: skip.

## 2. Data transfer and lxplus: verify, do not assume

- **Box -> EOS**: `backup_watcher.py` (running, PID was 30009) copies `/local/p2/p2data/cosmics_3det_202610/` to
  `/eos/experiment/ntof/data/x17/p2_sps_july/vmm_cosmic_run_oct26/` with xrdcp; it renews Kerberos itself
  (`kinit -R` every hour, GPG password fallback). The box ticket is renewable until **25 Nov 21:22** (past Monday).
  Check: `klist` on the box, the tail of the backup log (`logs/`), and that run_267 is complete on EOS. If the backup
  is not running, everything else fails after ~4 h (disk). Do not start the runs until this is verified.
- **Pruner**: `prune_backed_up.py --low 70 --target 90 --interval 600` (PID was 30030); free disk was 73 GB.
- **EOS space**: ~289 GB used so far by `runs/`; the weekend adds ~250-350 GB. The user / group quota nodes show
  "ignored" (project space); check with `eos quota` on lxplus for the project, and tell the user if in doubt.
- **lxplus**: `ssh lxplus` works while the user's ControlMaster is open (ask for `! ssh -fN lxplus` if denied).
  `condor_q -name bigbird25.cern.ch` (the node-local schedd hides jobs submitted elsewhere).

## 3. Configurations

`config_ext` = `/local/p2/p2testbench/TestBenchCERN/config_ext/`, `P=p2b-config-cern-ext_gain`, `S=_peaktime200_opt_cosmic_`.

| short | file | exists? |
|---|---|---|
| REF | `${P}4.5${S}trimmed_Km18_v1_hvm.txt` | yes (runs 248, 267) |
| A50, A10, A2 | `${P}4.5${S}bscan_hvon{50,10,2}_v1_hvm.txt` | yes (17 Nov) |
| B50, B10 | `${P}6.0${S}gscan_hvon{50,10}_hvm.txt` | yes (18 Nov) |
| **B2** | `${P}6.0${S}gscan_hvon2_hvm.txt` | **to make** |
| C50, C10 | `${P}9.0${S}gscan_hvon{50,10}_hvm.txt` | yes (18 Nov) |
| **C2** | `${P}9.0${S}gscan_hvon2_hvm.txt` | **to make** |
| **PRa / PRb** | REF + pulser on chip set A / B | **to make** |
| **PCa / PCb** | C10 + pulser on chip set A / B | **to make** |
| D50, D10 (optional) | `${P}3.0${S}gscan_hvon{50,10}_hvm.txt` | to make (needs a gain-3 scan) |

### 3.1 B2 and C2 (no new data needed)

From the HV-on scans of 18 Nov (`~/trim_work/out/gain_20261118/hvon_g6.0`, `hvon_g9.0`), as in
`fastscan/gain_chain_20261118.sh`:

```bash
cd ~/trim_work; O=out/gain_20261118; BURSTS=2:33,3:14,4:46,8:26,10:22,10:45,13:25,17:32
for g in 6.0 9.0; do
  python3 vmm_monitor_tool/fastscan/bscan_target.py --scan $O/hvon_g$g \
     --eq-file p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_eq.txt --map-eq $O/hvon_g$g/map_eq.json \
     --rate 2 --margin 0.5 --mask-extra $BURSTS --skip-raise 150 \
     --out-ext p2b-config-cern-ext_gain${g}_peaktime200_opt_cosmic_gscan_hvon2.txt \
     --report $O/hvon_g${g}_R2_report.json
done
```
Check the report: R = 2 Hz must lie inside each channel's scanned range (points 9.0: 110..22 mV, 6.0: 48..11 mV);
channels that never get that quiet are raised (`--skip-raise`), count them. Then the 60 s HV-on rate check
(cosmics_prep, section 5) writes the `_hvm` versions.

### 3.2 Pulser files

- One pulsed channel per chip on six chips per file. **Set A: chips 2, 5, 8, 11, 14, 17** (includes the two "silent"
  chips 2 and 14); **set B: chips 3, 6, 9, 12, 15, 18**. Six chips keep the extra data at ~6 x 10.85 kHz (~+25-30
  MB/min); all 18 at once would add ~80 MB/min, over the 70 MB/min guideline.
- Channel: an unmasked channel in the middle of each chip (e.g. 31, or the nearest unmasked one), the same for REF
  and C10 files of a set; write the list into the logbook and the file header.
- Amplitude `sdp10` 40 (100 % pulse efficiency at Km18 on 16 Nov). Neighbour read-out OFF (`sng 0`): this is not the
  pedestal method. `pulser_files.py` forces `sng 1`: add an option `--sng {0,1}` (default 1, unchanged behaviour),
  commit, deploy; use `--stride 64 --phase <ch>` for one channel per chip. Start from the `_hvm` files (masks kept),
  name the outputs `${P}4.5${S}trimmed_Km18_v1_hvm_tpA.txt`, `..._tpB.txt`, `${P}9.0${S}gscan_hvon10_hvm_tpA.txt`,
  `..._tpB.txt`. Do NOT pass them through cosmics_prep's masking: it masks channels > 5 kHz, i.e. the pulsed ones.
- Rate: the pulse rate is set by the FEC `tp_number` in the base YAML, not the ext file; with the standard base YAML
  it gave 10 851 Hz on 16 Nov (`base_tp2.yaml` = 2 pulses/frame). Verify in section 5.

### 3.3 Gain 3.0 (optional)

Same procedure as 4.5 -> 6/9 on 17/18 Nov (`fastscan/gain_maps_20261118.sh`, `gain_coarse_20261118.sh`, then
`baseline_scan.py` HV-on at gain 3 and `bscan_target.py` for R = 10, 50): a gtest start file with `sg` for 3 mV/fC,
an HV-off monitor map, an HV-on scan (~30-40 min), the targets, the rate check. Only if done by ~05:30; then D10 and
D50 join every pass (passes become 14 runs, 12.8 h).

## 4. Run plan: overlapping passes

Each run: 49 min of data (`--minutes 49`), ~6 min overhead -> 55 min. ~720 scintillator-confirmed muons per chamber
per run (18 Nov: 79.6 k over 36 runs).

| block | order (each item one run) | runs | hours |
|---|---|---|---|
| **P1** | REF, PRa, A50, A10, A2, B50, B10, B2, C50, C10, C2, PCa | 12 | 11.0 |
| **P2** | REF, PCb, C2, C10, C50, B2, B10, B50, A2, A10, A50, PRb | 12 | 11.0 |
| **HS** | REF, C10, REF, C10, REF, C10 | 6 | 5.5 |
| **P3** | REF, A50, B50, C50, PRa, A10, B10, C10, PCa, A2, B2, C2 | 12 | 11.0 |
| **P4** | REF, C2, B2, A2, PCb, C10, B10, A10, PRb, C50, B50, A50 | 12 | 11.0 |

Sequence: **P1, P2, HS, P3, P4, HS, P1, P2, HS, P3, P4, ...** until Mon 23 Nov 07:00. Expected, starting ~05:00 Thu:

| end of | approx. | every grid point | REF / C10 | pulser (PR, PC) |
|---|---|---|---|---|
| P1 | Thu 16:00 | 1 run | 1 / 1 | 1 / 1 |
| P2 | Fri 03:00 | 2 | 2 / 2 | 2 / 2 |
| HS | Fri 08:30 | 2 | 5 / 5 | 2 / 2 |
| P4 | Sat 06:30 | 4 | 7 / 7 | 4 / 4 |
| P2 | Sun 10:00 | 6 | 12 / 12 | 6 / 6 |
| P3 | Mon 02:30 | 7 | 16 / 16 | 7 / 7 |
| Mon 07:00 | (P4 partial) | 7-8 | ~17 / ~17 | ~8 / ~8 |

At the end: ~5 000-5 800 muons per chamber per grid point (+-0.005 per chamber, ~+-0.012 per chip), ~12 000 on REF
and C10. If the weekend stops early, every completed pass is a balanced grid.

## 5. Preparation, in order (attended, before 08:00)

1. Read this file and the DAQ project memory (`status-p2-lab-cosmics`, `vmm-gain-scan-nov18`, `vmm-daq-lab-mode`,
   `lab-hv-serial-supplies`). Check nothing is running: `pgrep -af "config_scan|hv_lab|baseline_scan"`, the GUI.
2. Ask the user the three questions of section 1.
3. Verify transfer and disk (section 2). `run_config_beam.py` must have `RUN_PLAN='config_scan'` (config_scan refuses
   otherwise).
4. Make B2, C2 (3.1) and the pulser files (3.2). Dry-run every file with the existing ext-file summary / datagram
   check used on 17-18 Nov.
5. **HV on**: as `fastscan/gain_hvup_20261118.sh` (write `out/hv_lab/HV_GO` with the user's words, reset
   `GIVEN_UP.json`, `hv_lab.py ramp-up --rup 5 --hold 120 --i-max 1.0`), then `hv_lab.py check-on`. Allow the chambers
   ~30 min to settle before the rate checks.
6. **Rate checks, HV on** (60 s each): `fastscan/cosmics_prep.py --out-dir out/weekend_20261119/prep --mask $BURSTS`
   on B2 and C2 (writes `_hvm`, masks channels > 5 kHz); and on the four pulser files a rate measurement WITHOUT the
   masking step (e.g. `--hot-hz 1e9` into a separate out-dir, keep the original tp files): the pulsed channels must
   show 10.85 kHz each (+-1 %), the file total must stay below ~90 MB/min. **If the pulsed channels show no pulses**,
   the base YAML has `tp_number` 0 or the test pulse is disabled in the FEC registers: then the pulser runs need
   `config-fec-regs` with `~/trim_work/pulser/base_tp2.yaml` after each configure (as `noise_probe.py --fec-yaml`),
   which config_scan cannot do -> drop PRa/PRb/PCa/PCb from the passes (replace by REF and C10) and tell the user.
7. **HV guard until the end**: `hv_lab.py guard --until 2026-11-23T07:30 --on-trip <policy from 1.2> --end down`,
   in its own `setsid nohup` (see gain_hvup_20261118.sh). It ramps everything down at 07:30 Monday.
8. **Supervisor** (section 6): write, `bash -n`, run once with `config_scan.py --dry-run` for each pass, then start
   with `setsid nohup ... < /dev/null &`. Logbook note with the plan.
9. **lxplus pipeline** (section 7): set up and submit before the first run ends; verify that run 268 is on EOS,
   dumped and in `results/current` before 08:00.
10. Before 08:00: write the state into the DAQ memory `status-p2-lab-cosmics.md` (what runs, PIDs, how to stop,
    expected end) and report to the user.

## 6. Supervisor script (to write: `vmm_monitor_tool/fastscan/weekend_supervisor_20261119.sh`)

What it must do (all lessons of 16-18 Nov):

```bash
# setsid nohup bash ~/trim_work/weekend_supervisor_20261119.sh >> ~/trim_work/out/weekend_20261119/supervisor.log 2>&1 < /dev/null &
# STOP between runs: touch ~/trim_work/out/weekend_20261119/STOP   (checked before every pass and after every config_scan exit)
DEADLINE="2026-11-23 07:00"
SEQ="P1 P2 HS P3 P4 HS P1 P2 HS P3 P4 HS P1 P2"     # longer than needed; the deadline cuts it
for each pass in SEQ:
    stop flag?  -> exit
    build the pass's config list from the table in section 4 (short -> file name); a missing file -> log + skip it
    before the pass: hv_lab.py check-on (if not at target: log, wait up to 30 min, then run anyway and log it:
                     the guard decides about HV, not the supervisor); free disk >= 75 GB (else wait for the pruner,
                     max 2 h, log); logbook note "weekend pass <name> <k>: <list>"
    until = min(now + n_runs * 56 min + 20 min, DEADLINE) written as HH:MM (config_scan takes HH:MM < 24 h)
    if until - now < 56 min: exit (no time for one more run)
    run: .venv/bin/python config_scan.py --until HH:MM --minutes 49 --configs <list>   (from /local/p2/DAQ_Control_VMM_Beam)
    after it returns: parse config_scan.log for the configs of this pass that "finished"; if config_scan stopped early
        (disk floor, warm reset failure, overran window, crash), wait 15 min and run the UNFINISHED configs of this
        pass once more (same rules); if it stops again within 30 min, log and continue with the next pass;
        three consecutive failed passes -> write ALERT, stop (leave HV to the guard)
at the end: logbook note, "ALLDONE weekend" in the log
```

Notes: config_scan writes `config/scan_subrun_min`; it needs `RUN_PLAN='config_scan'`; it applies each ext file with a
warm reset itself; it refuses below 60 GB free. Keep run naming automatic (run counter); record the pass and the
short name of every run in a CSV `out/weekend_20261119/runs.csv` (run, short, file, pass, start, end) by parsing the
"started run_N" / "finished" lines: the lxplus analysis uses it.

## 7. lxplus analysis in parallel (to set up from the data session; runs alone afterwards)

Goal: every finished run is on EOS within minutes (backup_watcher), dumped with `--diag`, and the efficiency /
diagnostics are updated every 30 min, until Monday, with nobody connected.

1. `A=/eos/experiment/ntof/data/x17/p2_sps_july/vmm_cosmic_run_oct26/analysis_lxplus`; `cd $A && mkdir weekend_20261119`
   (no `mkdir -p` on EOS), copy `eff_pipeline/` into `weekend_20261119/pipeline/` (scripts, env.sh, refs, maps,
   vmm_qa, vmm_monitor_tool), and replace `pipeline/cva/` by P2_basket_analysis `cosmic_vmm_analysis/*.py` from
   **main** (has `--dump-diag`, `--bad-times`, `probe_diagnostics.py`).
2. `env.sh`: `EFF_ANA=$A/weekend_20261119`, `EFF_PIPE=$EFF_ANA/pipeline`, `EFF_MIN_NEW=268`, `EFF_REF_RUNS=""`.
3. `run_new.sh` changes: dump with `--diag`; after the dumps run `spark_veto.py` on the new runs into `veto/`;
   measure twice (without / with `--bad-times veto`), regroup and checks for both, then
   `probe_diagnostics.py tags pseudo veto results/<ts>/diag`; keep the result-history logic.
4. Threshold maps for the gain files: copy from the box `~/trim_work/out/gain_20261118/hvon_g6.0/map_eq.json`,
   `hvon_g9.0/map_eq.json` (and g3.0 if made) into `pipeline/maps/` (as `map_eq_g6.0.json`, `map_eq_g9.0.json`) and add
   rules to `maps/config_maps.json` for `gain6.0_.*gscan_hvon(2|10|50)`, `gain9.0_...`, the `_tpA/_tpB` files (same
   map as their parent). If the rule format is unclear, leave them out: dist_mV = NaN is handled, the analysis session
   adds them (HANDOFF_analysis_20261119.md task 3).
5. The pulser files: the pulsed channels fire at 10.85 kHz; fine for the dump; list them in
   `pipeline/maps/pulsed_channels.json` ({file: [[vmm, ch], ...]}) for the analysis.
6. Loop job as ~/effloop on 18 Nov, but `+JobFlavour = "nextweek"` and the end time `2026-11-23 18:00`:
   `loop.sh` runs `bash $EFF_PIPE/run_new.sh` every 30 min (acron is denied for this account). Submit from
   `~/weekend_20261119/` on lxplus; note the cluster id and the schedd in the DAQ memory.
7. Verify before 08:00: run_268 appears in `$A/weekend_20261119/state/dumped.txt` and `results/current/runs.txt`.
   The job log should show EOS readable on every pass ("EOS not readable" = credential problem: tell the user).

## 8. How to stop / what to check later (write this into the status memory)

- Stop the runs between runs: `touch ~/trim_work/out/weekend_20261119/STOP` (the current run finishes).
  Immediate stop: the usual `stop_run.sh` / GUI (the sub-run is then not marked complete).
- HV: `touch ~/trim_work/out/hv_lab/GUARD_STOP` stops the guard (HV stays as it is!); ramp down with
  `hv_lab.py ramp-down`.
- Daily check (anyone): `tail ~/trim_work/out/weekend_20261119/supervisor.log`, `hv_lab.py check-on`, `df -h`,
  `tail` of the backup log; lxplus `condor_q -name bigbird25.cern.ch`, `results/current/runs.txt`.
