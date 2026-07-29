# VMM DAQ — SPS H4 test beam (TB_July26_H4) integration notes

Running log of the changes made to set up the shared DAQ + GUI for the July 2026
SPS H4 beam test. Kept step by step so the reasoning can be replayed later.

Machine: `dedippce185.extra.cea.fr` (128.141.21.163, CERN network).
GUI: http://dedippce185.extra.cea.fr:5002 (Flask, tmux session `flask`).

---

## Step 0 — shared deployment under /local/p2 (2026-07-28)

- The DAQ repo (`DAQ_Control_VMM_Beam`, Dream-port framework) was cloned from
  `~ak271430/DAQ_Control_VMM_Beam` to **`/local/p2/DAQ_Control_VMM_Beam`** at
  commit `11593a7`. `origin` points at the home copy, so fixes committed there
  arrive here with `git pull`.
- Git-ignored runtime state was copied over (`config/*.json`, run counter,
  `json_run_configs/`); the Python venv was rebuilt in place from
  `requirements.txt` (venvs are not relocatable).
- Permissions: everything `ak271430:basketp2`, group **rwX**, setgid dirs,
  plus default ACLs — files created by *any* basketp2 member stay readable and
  writable by the whole group, independent of that person's umask.
- The GUI now runs **from this shared copy** (not from the home directory).

## Step 1 — beam data destination (2026-07-28)

- Runs are written to **`/local/p2/p2data/TB_July26_H4/`**.
- Directory set to `2770` (setgid) with default ACLs `u::rwX g::rwX o::-` —
  every run/subrun directory and pcapng the DAQ creates is automatically
  basketp2 group read/write. No chmod needed afterwards, ever.

## Step 2 — run configuration for the sps site (2026-07-28)

`run_config_beam.py`, `sps` site block:

- `base_data_dir` → `/local/p2/p2data/TB_July26_H4/` (was a TODO-SPS
  placeholder).
- `alinx_config` → `/local/p2/p2testbench/TestBenchCERN/config/config_alinx_noThresholds.json`
  — the **TestBenchCERN toolchain (alinx-sc)**, i.e. exactly what the Saclay
  bench site already used. This is deliberate, see "Decisions" below.

## Step 3 — site switch (2026-07-28)

- `config/site.txt` (gitignored, per-machine) flipped `bench` → **`sps`**:
  real dumpcap capture on `enp4s0f1`, alinx-sc around each sub-run, no
  simulation. Flask restarted to pick it up.

## Step 5 — VMM chip configuration from the GUI (2026-07-28)

New "VMM Chip Config" panel on the main page (below Hybrids LV Power):

- Dropdown listing the exception files in
  `/local/p2/p2testbench/TestBenchCERN/config_ext/*.txt` (the
  gain × peaktime [× pulse] grid), applied on top of the base yaml
  auto-detected in `config_base/` (currently `p2b-config-cern-base.yaml`).
- **Configure chip** button runs the p2basket utility
  `TestBenchCosmics/p2basket_sc_config.py -c <dir> -b <base> -e <ext>` as a
  subprocess — the colleagues' script is *called, never modified*. Output,
  exit code and timestamp are shown in the panel; every apply is written to
  the DAQ event log.
- Safety: apply refuses while a dumpcap/tcpdump capture is running (never
  reconfigure the chip mid-acquisition); one configure at a time; file names
  are validated against the real directory listing.
- **Provenance**: the selected .txt persists in
  `config/chip_config_state.json`; at every run start `daq_control.py` copies
  it into the run directory next to `run_config.json` (best-effort — an
  unconfigured chip config never blocks a run).
- Decisions taken (2026-07-28): apply is manual-button only (no auto-apply at
  run start); provenance copy is the ext txt only, at run level.
- Machine wiring in `config/chip_config.json` (gitignored; tracked example
  `config/chip_config.json.example`). New code: `flask_app/chip_config.py`,
  three `/chip_config/*` routes, panel + JS in `index.html`, provenance hook
  in `daq_control.py`.
- Tested: listing/selection/state/render + bad-input rejection. The apply
  button has deliberately NOT been pressed yet — first click on real hardware
  is the operators' call.

## Step 6 — LV power panel cleanup + real LV monitoring curves (2026-07-28)

The Hybrids LV Power panel no longer dumps raw terminal output:

- **Power On / Off** now give a clean signal — green "ok ✓" / red "FAILED ✗"
  with a timestamp. The raw command output appears only when an action fails
  (as debugging aid).
- **Measure** shows parsed values instead of terminal text:
  `ALINX 12V: 11.998 V · 1.100 A | Hybrids 3.3V: … | Hybrids 2.2V: …`.
  Supply names come from config (IP → name map in power_config.json).
- **Auto-measure**: the GUI now measures the TDK supplies every 60 s in the
  background (config `auto_measure_s`; skipped while a manual action runs).
  Every measure — manual or automatic — is appended to
  `logs/tdk_lv_history.csv`.
- **LV Monitor plots now show real curves at this site**: the TDK-Lambda
  supply history (voltage solid, current dotted, one color per supply) is
  drawn in the existing LV Monitor panel. The TTi per-subrun traces still
  overlay when such data exists (this site has no TTi units — that was why
  the LV plots were empty). Backend: new `/power/lv_history` route.
- Verified live: measure parsed correctly (12 V / 3.3 V / 2.2 V supplies all
  reporting), CSV growing, auto-measure produced points without any button
  press, plots fed. On/Off untested (would cut front-end power — operators'
  call, as always).

## Step 7 — acquisition arming switched to p2basket-sc (2026-07-28)

The TestBenchCERN/README (config example + `p2basket-sc --acq-on/--acq-off`)
triggered two changes:

- **Critical fix**: `vmm_daq_control.py` armed acquisition with `alinx-sc`,
  which only exists in the old December install and is not on PATH in the
  current deploy — the first sps run would have failed at acq-on. The
  slow-control layer is now a per-interface backend dispatch:
  `slow_control: 'alinx'` (legacy, Saclay bench site unchanged) or
  `'p2basket'` (sps site) running
  `/local/p2/deploy/bin/p2basket-sc --config-file config_base/<base>.yaml
  --acq-on|--acq-off|--read-link-status` from the TestBenchCERN directory.
  The config file is passed explicitly because bare `p2basket-sc --acq-on`
  (as in the README) depends on a per-user default config ("No configuration
  file defined for user ..."), which our DAQ user does not have — explicit
  beats invisible session state. Same retry logic as before around acq-on.
- **Chip config apply script repointed** to the identical, README-blessed copy
  `TestBenchCERN/p2basket_sc_config.py` — the GUI no longer touches the
  TestBenchCosmics directory at all; full decoupling from in-development code.
- Verified with `p2basket-sc --dry-run` (no UDP sent): invocation + yaml load
  OK; exit codes are meaningful (2 on failure → retry loop works). The live
  test failed honestly with "Fec 192.168.0.12 does not respond to ping" —
  front-end not up at test time; to be exercised with the crate on.

## Step 8 — combined-runs VMM side BUILT; Dream side staged (2026-07-28)

Implemented per `COMBINED_RUNS_PLAN.md`, **without touching the running Dream
DAQ** (only read-only recon on banco — file reads + GETs to its Flask):

- **`hv_dream_shim.py`** (new, runs as the `vmm_hv_control` tmux at sps via
  the site switch in `start_servers.sh`): speaks the exact hv_control wire
  protocol — full choreography verified with a live protocol test. No combined
  run active → instant 'HV Set' (VMM-only runs need no HV hardware). Combined
  run → each subrun's 'HV Set' waits until Dream's /hv_data readback reaches
  that subrun's targets (targets come from our own sub_run['hvs']); timeout →
  reply without 'HV Set' so daq_control skips the subrun instead of taking
  data at the wrong voltage.
- **Trigger + toggle**: "+ Dream" checkbox next to Start Run (visible when
  config/dream_bridge.json exists) → /run_config_py fires
  /vmm_trigger/start on banco with run name + full sub_runs schedule; refusal
  or unreachable → nothing starts, loud error. Combined-state file → shim
  gates; daq_control teardown (`finally`) fires /vmm_trigger/stop with retries
  on ANY run end and clears the state. Dream status chip live in the GUI
  (verified: shows Dream's actual current run from across the network).
- **Staged, NOT deployed**: `docs/dream_side/vmm_trigger.py` (additive route
  module for Dream's flask: token+IP auth, refuses if Dream mid-run, HV
  bound-checks, builds the run config from Dream's own template + merged
  basket targets, starts via Dream's start_run.sh) + `README_DEPLOY.md` (10-min
  deploy steps between runs + verification ladder). Deploy needs: basket
  card/channel numbers into Dream's HV config, shared token, then the trace
  labels back into VMM's config/dream_bridge.json (hv_gate.channel_labels)
  and P2_HV slot/ch in VMM's run_config_beam.py.
- Shared token generated into VMM's gitignored config/dream_bridge.json.

## Planned — combined VMM + Dream runs (design agreed 2026-07-28, NOT yet implemented)

> **Final plan: see `COMBINED_RUNS_PLAN.md`** (same directory) — includes the
> HV-scan-across-subruns requirement decided later the same day: the trigger
> payload carries the full sub-run schedule, and a remote-HV shim on the VMM
> side gates each subrun on Dream's real ramp. The section below is the
> earlier state of the discussion, kept for the paper trail.

Problem: the CAEN HV crate (192.168.10.199) hangs off banco's private DAQ LAN
(banco = dedippcq196, runs the live DAQ_Control_Dream_Beam stack) and is not
reachable from the VMM machine; and the uRWELLs (Dream) should record together
with VMM runs, with run/time sync.

Agreed design:

- **VMM GUI triggers Dream**: "Start Run" with a "+ Dream" toggle makes one
  HTTP call to Dream's Flask on banco → Dream starts its own normal run
  (uRWELLs + HV + processing, fully independent) with the SAME run name.
  Stop fires together too, hooked in VMM daq_control's teardown so natural
  end, manual stop and errors all propagate (best-effort, loudly reported).
- **HV ownership**: Dream's hv_control handles ALL detectors' HV, always —
  uRWELL and P2 mesh/drift alike, in independent Dream runs and combined runs
  both. The VMM stack sets hv_ip='sim' at sps (no real HV connection).
- **Single edit point for P2 physics**: MESH_V/DRIFT_V stay in the VMM
  run config; the trigger payload carries them and Dream overrides its P2
  defaults for that run (bound-checked against Dream's limits; out-of-bounds
  → run refused → combined start aborts loudly, VMM does not start alone).
- **Wiring stays put**: P2 card/channel mapping + safety limits live in
  Dream's config (set once). Run counters stay native on each side; combined
  runs share the VMM-generated name. Trigger route is passive + token/IP
  guarded — Dream standalone operation is completely unaffected.
- Failure asymmetry accepted: a Dream-side stop does not stop VMM (VMM GUI
  will show a Dream status chip instead; reverse coupling maybe later).
- Supersedes the earlier idea of a second hv_control instance on banco
  (single-client Server.py made concurrent sharing impossible anyway; that
  fallback remains documented for VMM-only periods without Dream).

## Step 9 — dry-run prep + beam2/Disk Space mirror (2026-07-29)

Preparing the first GUI dry run (NOT yet launched):

- **Dry-run schedule**: 1 sub-run × 10 s in `run_config_beam.py` (restore beam
  values after). No HV is ramped — the shim passes VMM-only runs instantly.
- **Per-run LV record**: at run teardown, `daq_control` snapshots the TDK
  auto-measure rows covering the run into `<run>/tdk_lv_monitor.csv`.
- **Disk Space tab live**: regenerated `config/backup_config.json` (it was
  never generated on the shared copy) — tab now shows the TB_July26_H4 disk.
- **Beam + Beam2 mirrored from the Dream DAQ** (its beam stack evolved after
  our July port): `beam_bridge.py` (lxplus watcher publishes to EOS
  `/eos/user/a/akallits/beam_monitor`; the bridge xrdcp-pulls state + CSVs —
  same files serve both DAQs), new `sps_monitor/` (spill structure, H4 line,
  TAX barrier proxied from the mx17 DAQ), rewritten Beam tab + new Beam2 tab
  + Overview beam tile, payload-timestamp staleness logic. Start Beam Watcher
  now launches the bridge (`SPS_BEAM_MODE=direct` would use NXCALS directly).
- **Missing runtime prerequisites on this machine** (bridge AND the EOS
  backup watcher need them): xrootd client tools (`xrdcp`/`xrdfs`, e.g.
  conda-forge xrootd linked into `~/bin`) and CERN.CH Kerberos
  (`kinit akallits@CERN.CH`, plus `~/.cern_pass.gpg` for unattended renewal).
  Until then the Beam/Beam2 tabs honestly report "watcher not running".

## Git workflow at the beam (decided 2026-07-29)

**The shared repo on the DAQ machine is the source of truth during the beam
test — commit and push from there**, not from personal laptops with rsync.
Everything is committed as of 18e43c0 and the shared repo pushes straight to
GitHub (`git push github main`).

Per person, one-time setup:
1. Get collaborator (write) access to `github.com/akallitss/DAQ_Control_VMM_Beam`.
2. ssh into the DAQ machine with **your own account and agent forwarding**
   (`ssh -A`), so pushes use your own GitHub key.
3. Set your identity once in your account there:
   `git config --global user.name "..." && git config --global user.email "..."`
   (identity is deliberately NOT set in the shared repo's config — otherwise
   everyone's commits would be attributed to one person).

Rules of the road:
- Commit at least at the end of your shift / when a feature works. The repo
  is group-writable — uncommitted work is unprotected.
- `git push github main` after committing; `git pull github main` before
  starting changes.
- **Careful with the GUI "Git Reset" button** — it hard-resets and wipes
  uncommitted machine edits. Use it only deliberately, never as "sync".
- Off-beam development on personal machines goes through GitHub (push there,
  pull on the DAQ machine) — never by copying files onto the shared tree.

---

## Decisions

- **The GUI drives the TestBenchCERN toolchain for this beam test.** The new
  p2basket slow-control utilities in `TestBenchCosmics/`
  (`p2basket_sc_config.py`, `p2basket_sc_warm_reset.py`,
  `p2basket_sc_ExtCfg_*.py`) are **not called and not modified** — they are
  still under active development. The DAQ config keeps a single, clearly
  marked redirect point (the `slow_control` / `alinx_config` fields of the
  interface entry) so switching to the p2basket utilities later is a config
  change, not a code rewrite.

## Planned next (from the TestBenchCosmics review, to discuss)

Once the p2basket utilities are frozen by their authors:

1. Hybrid readiness panel in the GUI (`p2basket_sc_warm_reset.py` detect-only;
   its exit code = number of non-ready hybrids).
2. Pre-run readiness gate: refuse acquisition start until warm-reset loop
   returns 0 failed hybrids.
3. Freeze watchdog: detect the known 10 GE output freeze (rate → 0 during a
   run), auto warm-reset + restart sub-run, and log every occurrence with
   context to help find the root cause.
4. Configure-from-GUI (base YAML + exceptions file) with per-run provenance
   copies, as already done for the alinx-sc config.
5. Ask to the authors: a `--status-only` JSON output mode, so the GUI can show
   *which* hybrid is non-ready without parsing console text.

## Open TODOs

- `TODO-SPS` in `run_config_beam.py`: CAEN HV mainframe IP (placeholder
  `192.168.10.81`), number of HV cards, Aim-TTi LV IPs. **Until these are set,
  the GUI's HV/LV panels will show connection errors on the sps site** — that
  is expected and honest, not a malfunction.
- cron / mem_guardian / backup-mirror on the machine still reference the old
  home-directory copy; switch them to `/local/p2` once this deployment is
  declared the live one.
- The old copy under `~ak271430/` is untouched and remains the fallback.
