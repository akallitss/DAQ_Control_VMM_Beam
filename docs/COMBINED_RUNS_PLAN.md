# Combined VMM + Dream runs at SPS H4 — implementation plan

Final agreed design, 2026-07-28. Status: **planned, not yet implemented** —
waiting for the go. Companion to `TB_July26_H4_INTEGRATION_NOTES.md` (which
logs what is already built).

## The problem

- The CAEN HV crate (192.168.10.199) hangs off **banco**'s private DAQ LAN
  (banco = dedippcq196, `ssh banco_cern`) and is unreachable from the VMM DAQ
  machine (dedippce185).
- The uRWELLs must be recorded by the **Dream DAQ** (live on banco) at the
  same time as VMM runs, with run/time synchronization.
- **HV scans across subruns** will be run: each subrun can have different P2
  mesh/drift setpoints, and the VMM capture must not start until the real HV
  has ramped.
- Dream's `hv_control` cannot simply be shared: its `Server.py` is
  single-client (`listen(1)`) and Dream's own `daq_control` holds the
  connection for a whole run.

## The design in one paragraph

The VMM GUI's Start Run (with a "+ Dream" toggle) sends one HTTP trigger to
Dream's Flask on banco carrying the **run name** and the **full sub-run
schedule** (durations + per-subrun P2 mesh/drift setpoints) taken from the VMM
run config — the single physics edit point. Dream then executes a completely
normal, independent run under the same name: all detectors' HV (uRWELL **and**
P2), monitoring, uRWELL recording, processing. On the VMM side, the site's HV
server is a small **remote-HV shim** that speaks our existing hv_control wire
protocol but, instead of driving CAEN, waits per subrun until Dream reports
that subrun's HV ramped — so subrun boundaries genuinely align, gated by the
real ramp. Stopping the VMM run (button, natural end, or error) fires a
best-effort stop to Dream. Dream running standalone is untouched by all of
this.

## Decisions (agreed)

1. **Stop together**: any VMM run end propagates a stop to Dream (hooked in
   VMM `daq_control` teardown so all end paths are covered). Best-effort with
   retries; failure is loudly reported, never silent.
2. **Run identity**: combined runs use the VMM-generated run name on both
   machines. Dream's independent runs keep their native numbering; no
   collisions (names carry timestamps).
3. **HV ownership**: Dream's HV map contains **all detectors, always** —
   uRWELL and P2 alike, in independent and combined runs. The VMM stack never
   talks to the crate (its `hv_ip` is the shim).
4. **Single edit point**: P2 setpoints (per subrun, for scans) live in the VMM
   run config and travel in the trigger payload. Dream **bound-checks** them
   against its configured limits; out-of-bounds → run refused → the combined
   start **aborts loudly** (VMM does not silently run alone — unchecking the
   toggle is the explicit way to run VMM-only).
5. **Wiring stays put**: P2 card/channel mapping and safety limits live in
   Dream's config, set once. Only voltages travel.
6. **Failure asymmetry accepted**: a stop from the Dream side does not stop
   VMM (visible via a Dream status chip in the VMM GUI; reverse coupling
   later only if needed).

## Components to build

### Dream side (banco — live machine, additive only, deployed between runs)

- One new Flask route (token- and IP-guarded, passive): accept run name +
  sub_runs schedule + P2 setpoints; validate against limits; generate the run
  config (structures are identical between the two DAQs — direct mapping) and
  start the run tagged "triggered by VMM". A matching guarded stop route.
- Config, once: P2 mesh/drift entry in the HV map (with default setpoints for
  independent runs) + limits.
- If the existing status endpoints don't already expose "current subrun + HV
  ramped" (they likely do — the VMM Flask is a clone of Dream's), add one
  read-only status field for the shim to poll.

### VMM side

- `run_config_beam.py` sps: HV points at the shim; trigger settings (banco
  URL + token).
- **Remote-HV shim** (~100 lines, reuses our `Server.py`): implements the
  hv_control wire protocol; on each subrun `Start` polls Dream's status until
  that subrun's HV is ramped, then answers `'HV Set'`. `Begin/End Monitoring`
  are no-ops (Dream does the real monitoring). Runs as the `vmm_hv_control`
  tmux session — the GUI status card keeps working unchanged.
- `daq_control` teardown hook: combined-run flag file at start → stop call to
  Dream on any run end.
- GUI: "+ Dream" toggle on Start Run; Dream status chip (run name, state,
  reachable/not); HV curves displayed by proxying Dream's `/hv_data` for the
  matching run (display only — `hv_monitor.csv` lives in Dream's run tree,
  matched by run name).

## What explicitly does NOT change

- Dream standalone operation: byte-for-byte as today (the trigger route is
  passive; P2 in its HV map just means its independent runs also power/log P2
  at defaults — requested behavior).
- VMM capture/QA/backup chain, chip config, LV monitoring, p2basket-sc
  acquisition arming — all as already built.
- The colleagues' TestBenchCosmics code: untouched, still not called.

## Build order / test ladder

1. Dream-side route + P2 config entry on banco (between Dream runs; verify an
   independent Dream run still works exactly as before).
2. VMM shim + config + teardown hook + GUI toggle.
3. Test 1 — trigger only: combined start/stop with HV at current defaults,
   verify same run name both sides, stop-together, Dream unaffected.
4. Test 2 — scan sync: a short 2–3 subrun scan with small, safe voltage
   steps; verify VMM subrun N starts only after Dream ramped subrun N, and
   the timestamps/boundaries align.
5. First real combined beam run.

## Open items (unrelated to this plan, still pending)

- Aim-TTi LV IPs in the sps site are placeholders (LV panels show connection
  errors; TDK monitoring already covers the real supplies).
- `/mnt/p2backup` fstab entry on the DAQ machine (reboot persistence).
- The shared-repo changes are uncommitted; commit + push once the user says.
- First press of the GUI "Configure chip" button on real hardware.
