# VMM DAQ — external code & configuration guide (for the p2basket colleagues)

What our DAQ/GUI runs on, exactly which **external scripts** it calls (and the
contract each must keep), where **external configuration** enters the
information flow, and which **scans** the setup can run. Written 2026-07-29 for
the TB_July26_H4 beam test; machine paths are the shared deployment
`/local/p2/DAQ_Control_VMM_Beam` on dedippce185 (GUI: http://128.141.21.163:5002).

---

## 1. The information flow in one picture

```
                 GUI (Flask :5002)
                       │
        ┌──────────────┼─────────────────────────────┐
        │              │                             │
  [Configure chip]  [Start Run]                 [LV panel]
        │              │                             │
        ▼              ▼                             ▼
 p2basket_sc_config  run_config_beam.py       pilot_tdkl.py /
 (base yaml + ext txt)  → frozen JSON         switchOn/Off_tdk.sh
        │              │  (run_config.json          (TDK supplies)
        │              ▼   + chip ext txt copied
        │          daq_control                to the run directory)
        │              │  per sub-run:
        │              ├─► HV gate (hv_dream_shim → Dream DAQ readback)
        │              ├─► vmm_daq_control
        │              │     ├─ p2basket-sc --acq-on      ◄── external
        │              │     ├─ dumpcap ring-buffer capture (ours)
        │              │     └─ p2basket-sc --acq-off     ◄── external
        │              └─► next sub-run …
        ▼              
   VMM registers    finalized .pcapng → QA (vmm_pcapng_qa) → plots in GUI
                                     → backup watcher (EOS / mirror)
```

Rule of thumb: **external code is always called as a subprocess, never
imported and never copied** into our repo. If your script keeps its CLI and
exit-code contract, you can develop it freely and the GUI follows.

---

## 2. The basis code (ours) — what actually runs

| Process (tmux)        | File                    | Role |
|-----------------------|-------------------------|------|
| `flask` / `vmm_flask` | `flask_app/app.py`      | GUI: run control, chip config, LV power, QA browser, status |
| `vmm_daq_control`     | `daq_control.py`        | Run orchestrator: loops sub-runs, HV gate, provenance copies |
| `vmm_daq`             | `vmm_daq_control.py`    | Capture server: acq-on/off around dumpcap ring buffer |
| `vmm_hv_control`      | `hv_dream_shim.py` (sps)| Per-sub-run HV gate against the Dream DAQ (crate is on banco) |
| `vmm_lv_control`      | `lv_control.py`         | TTi monitoring (inactive here — TDK panel covers LV) |
| watchers              | `qa_watcher.py`, `backup_watcher.py`, `mem_guardian.py` | online QA, EOS/mirror backup, OOM protection |

You do not need to touch any of these. You plug in at the call-outs below.

---

## 3. External scripts we call — and the contract each must keep

### 3.1 Chip configuration (GUI "Configure chip" button)

```
/local/p2/p2testbench/TestBenchCERN/p2basket_sc_config.py \
    -c /local/p2/p2testbench/TestBenchCERN \
    -b config_base/<the single .yaml> \
    -e config_ext/<file chosen in the GUI dropdown>
```

Contract:
- **CLI stays `-c/-b/-e`**; exit code **0 = configured**, non-zero = failure
  (the GUI shows your stdout/stderr to the operator either way).
- `config_base/` contains **exactly one** `*.yaml` (we auto-detect it; rename
  freely). `config_ext/*.txt` **is** the GUI dropdown — drop a new exceptions
  file there and it appears within seconds, nothing to redeploy.
- Runs under its own `p2basket-python` shebang — keep that.
- We refuse to run it while a capture is in progress; one apply at a time.

### 3.2 Acquisition arming (around every sub-run, automatic)

```
cd /local/p2/p2testbench/TestBenchCERN
/local/p2/deploy/bin/p2basket-sc --config-file config_base/<base>.yaml --read-link-status
/local/p2/deploy/bin/p2basket-sc --config-file config_base/<base>.yaml --acq-on    (3 retries)
... capture ...
/local/p2/deploy/bin/p2basket-sc --config-file config_base/<base>.yaml --acq-off
```

Contract:
- Exit code 0 = success (we retry acq-on on non-zero, then abort the sub-run).
- We pass `--config-file` **explicitly**: the bare `p2basket-sc --acq-on` from
  the README relies on a per-user default config, which the DAQ account does
  not have.

### 3.3 LV power (GUI Hybrids LV Power panel)

- `pilot_tdkl.py -c config/tdkl_*.json --action measure`, and
  `switchOn_tdk.sh` / `switchOff_tdk.sh`, run from TestBenchCERN.
- **The measure output line format is parsed by the GUI**:
  `... TDK: <ip> <volts> V ; <amps> A` — one line per supply. Keep that shape
  (or tell us) — it feeds the readings display and the LV monitoring curves
  (auto-measured every 60 s into `logs/tdk_lv_history.csv`).

### 3.4 HV — deliberately NOT an external script here

The CAEN crate hangs off banco; **all HV (uRWELL + P2 basket) is ramped,
monitored and logged by the Dream DAQ**. Our side only *gates* each sub-run on
Dream's voltage readback (`hv_dream_shim.py`). See `COMBINED_RUNS_PLAN.md` and
`dream_side/README_DEPLOY.md`.

### 3.5 What we'd like you to provide next (the wishlist)

1. **Frozen `p2basket_sc_config.py` + `p2basket_sc_warm_reset.py`** with
   stable CLIs — the warm-reset exit code (= number of non-ready hybrids) is
   already a great contract, we will build a readiness panel + pre-run gate
   on it.
2. A **`--status-only` mode with JSON output** on the warm-reset script, so
   the GUI can show *which* hybrid is non-ready without scraping pretty text.
3. Please **don't** build acquisition/tshark/dumpcap into the final utilities —
   capture, rotation, QA and provenance are already our DAQ's job
   (that part of `p2basket_sc_ExtCfg_work.py` would be duplicate machinery).

---

## 4. External configuration — every entry point in the flow

| Configuration | Owned by | Read at | Where it lands (provenance) |
|---|---|---|---|
| `config_base/*.yaml` (register base) | you | chip apply + every acq arming | referenced by name in run notes; the applied ext file records the delta |
| `config_ext/*.txt` (exceptions = gain/peaktime/pulse grid) | you | chip apply (GUI choice, persisted) | **copied into the run directory** next to `run_config.json` at every run start |
| `run_config_beam.py` (schedule, P2 setpoints, sites) | us | Start Run → frozen to JSON | `run_config.json` in the run directory |
| `config/site.txt` (`local`/`bench`/`sps`) | machine | process start | picks data dir, backends, sim/real |
| `config/chip_config.json` | machine (gitignored) | GUI | points at your `-c/-b/-e` paths |
| `config/power_config.json` | machine (gitignored) | GUI | your TDK script commands + supply names |
| `config/dream_bridge.json` | machine (gitignored) | combined runs | banco URL, token, HV gate channel→label map |
| Dream side: `config/vmm_trigger.json` + its own run config | Dream/us | triggered runs | Dream's `run_config.json` + `hv_monitor.csv` in **its** run tree (same run name) |

The principle: *physics you change often* lives in `run_config_beam.py` and
`config_ext/` (one edit point each); *machine wiring* lives in gitignored
`config/*.json`; *everything a run used* is copied into that run's directory.

---

## 5. Scans this setup can run

| Scan | How | State |
|---|---|---|
| **N identical sub-runs** (physics/commissioning) | `N_SUBRUNS` × `SUBRUN_MIN` in `run_config_beam.py` | working today |
| **HV scan across sub-runs** (mesh and/or drift stepped per sub-run) | per-sub-run `hvs` in the run config; combined run sends the schedule to Dream, which ramps; our shim holds each sub-run's capture until the readback is at target | VMM side built + tested; waits for the Dream-side deploy |
| **Chip-setting scan** (gain / peaktime) | today: one run per `config_ext` file — Configure chip, then Start Run, repeat | manual; automating "one run per ext file" is a natural next step — needs nothing from your side beyond the frozen config utility |
| **Pulser runs** (internal test pulses) | via a `*_pulse.txt` ext file (channel 35 style) + normal run | works through the same chip-config path |
| Dream-side plans (`drift_then_mesh`, 2D maps, latency scans) | native to the Dream DAQ, independent of us | Dream's own |

Sub-run mechanics underneath any scan: dumpcap rotates files every 44 s, QA
runs per finalized file, `Stop Sub-Run` / `Stop Run` / `Pause After Subrun`
work at boundaries, and a cut-short sub-run is left unmarked so a resume
re-runs it.

---

## 6. Where to read more

- `TB_July26_H4_INTEGRATION_NOTES.md` — the step-by-step log of everything
  set up at CERN (chip config, LV curves, p2basket-sc switch, combined runs).
- `COMBINED_RUNS_PLAN.md` — the VMM+Dream combined-run architecture.
- `dream_side/README_DEPLOY.md` — the pending 10-minute Dream-side deploy.
- `ARCHITECTURE.md` — the full framework walkthrough (processes, wire
  protocol, filesystem-as-data-model).
