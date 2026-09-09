# Dream-side deployment for combined VMM+Dream runs — STAGED, NOT DEPLOYED

Everything in this directory is prepared but must NOT touch banco while the
Dream DAQ is in active use. Deploy between runs, total downtime = one Flask
restart (the DAQ processes are untouched).

## Steps (on banco, ~10 minutes)

1. **Copy the route module** (additive, nothing existing is modified):
   ```
   cp vmm_trigger.py ~/DAQ_Control_Dream_Beam/flask_app/vmm_trigger.py
   ```

2. **Register it** — add at the END of `flask_app/app.py` (after the last
   route):
   ```python
   import vmm_trigger
   vmm_trigger.register(app, BASE_DIR, CONFIG_RUN_DIR, BASH_DIR,
                        VENV_PYTHON, _save_current_run)
   ```
   (All five names already exist in Dream's app.py.)

3. **P2 basket HV channels into Dream's config** — add the basket detector
   (mesh + drift card/channel on the crate) to `run_config_beam.py`'s detector
   HV map with its default operating point, like the P2 station entries.
   *This is the step that needs the physical channel numbers — check the crate
   cabling.* After this, independent Dream runs also power/log the basket
   (requested behavior).

4. **Trigger config** — create gitignored `config/vmm_trigger.json`:
   ```json
   {
     "token": "<same secret as VMM's config/dream_bridge.json>",
     "allow_ips": ["128.141.21.163"],
     "hv_limits": {"<slot>:<ch>": 600, "<slot>:<ch>": 750}
   }
   ```
   `hv_limits` keys are the basket channels as `"slot:ch"`; a triggered run
   asking for more than the limit is refused whole.

5. **Restart the Dream Flask only** (between runs):
   ```
   tmux kill-session -t flask_server && <their usual flask start>
   ```

6. **Tell the VMM side the trace labels**: whatever detector name step 3 used
   determines the `/hv_data` labels (e.g. `B_Mesh`, `B_Drift`). Put those in
   VMM's `config/dream_bridge.json` under `hv_gate.channel_labels`
   (`"slot:ch" -> label`), and the same slot/ch into VMM's `P2_HV` map in
   `run_config_beam.py` so the schedule targets the right channels.

## Verify after deploying (before any combined beam run)

1. `curl -s http://localhost:5001/vmm_trigger/start -X POST -H 'Content-Type: application/json' -d '{"token":"BAD"}'`
   → 403 bad token (route alive, auth works).
2. An **independent Dream run** started from the Dream GUI behaves exactly as
   before (now also logging basket HV).
3. From the VMM GUI: combined test run with the toggle on, HV at current
   defaults → same run name on both machines, both stop when VMM stops.
4. Short 2–3 subrun scan with small safe voltage steps → VMM subrun N starts
   only after Dream's readback reached subrun N's targets (watch the
   `vmm_hv_control` tmux on the VMM machine — the shim logs every gate poll).

## Failure semantics (by design)

- Trigger refused/unreachable → VMM aborts its start, nothing runs.
- Dream already mid-run → trigger refused (409), VMM aborts.
- VMM run ends any way at all → best-effort stop to Dream (3 retries), else a
  loud "STOP IT FROM THE DREAM GUI" in the vmm_daq_control log.
- Shim gate timeout (Dream never reaches targets) → that VMM subrun is
  skipped, run continues; check the Dream HV panel.

---

# Lab runs: the read-only HV service (`hv_live.py`) — STAGED

For **lab** measurements (Fe55, cosmics) the VMM machine takes the run and
banco takes no run at all, so Dream's `/hv_data` — which reads a *run's*
`hv_monitor.csv` — has nothing to serve. Without this service a lab run keeps
no record of the voltage it was taken at.

`hv_live.py` adds one read-only route that reports the crate's present
readback. `hv_dream_shim.py` on the VMM machine polls it during every sub-run
and writes `<run>/<subrun>/hv_monitor.csv` in exactly the format
`hv_control.py` would have written, so the VMM GUI's HV panel, the per-run
record and `vmm_qa/vmm_scan_curves.py` all work with no further changes.

It sets nothing. There is no route in it that can ramp, set or power anything.

## Steps (on banco, ~10 minutes)

1. **Copy the route module** (additive):
   ```
   cp hv_live.py ~/DAQ_Control_Dream_Beam/flask_app/hv_live.py
   ```

2. **Register it** — next to the `vmm_trigger` registration in
   `flask_app/app.py`:
   ```python
   import hv_live
   hv_live.register(app, BASE_DIR)
   ```

3. **Create gitignored `config/hv_live.json`** (channels are the ones the lab
   detectors actually hang off — take them from Dream's own run config, this
   is the step that needs the crate cabling):
   ```json
   {
     "token": "<same secret as the VMM's config/dream_bridge.json hv_live_token>",
     "allow_ips": ["<VMM machine IP>"],
     "poll_s": 5,
     "idle_release_s": 120,
     "hv": {"ip": "192.168.10.81", "username": "admin", "password": "admin"},
     "channels": ["8:0", "8:1"],
     "labels": {"8:0": "P2_Drift", "8:1": "P2_Mesh"}
   }
   ```
   `"ip": "sim"` runs it against the fake crate — useful to check the route
   before pointing it at hardware.

4. **Restart the Dream Flask only** (the DAQ processes are untouched).

5. **Point the VMM side at it**: in the VMM machine's
   `config/dream_bridge.json` set `hv_live_url` to
   `http://<banco>:5001/hv_live` and `hv_live_token` to the same secret
   (see `config/dream_bridge.json.example`).

## Verify

1. `curl -s 'http://localhost:5001/hv_live?token=BAD'` → 403.
2. `curl -s 'http://localhost:5001/hv_live?token=<secret>' | head -c 300` →
   `success: true` and one entry per configured channel, `vmon`/`imon`
   matching the crate's front panel, `v0` the setpoint.
3. From the VMM machine: same curl over the network (this also proves
   `allow_ips` and any firewall).
4. Start a short VMM run: `tmux attach -t vmm_hv_control` shows
   `[shim] HV log: .../hv_monitor.csv — N channels`, and the GUI's HV panel
   plots the sub-run.
5. **Contention check, once**: with the service up (poll it so it holds a
   session), start an ordinary Dream run and confirm Dream's own HV control
   connects to the crate as usual. If it ever does not, `touch
   config/hv_live_pause` on banco releases the session immediately and the
   VMM side simply logs no HV until the file is removed.

## How it behaves

- The CAEN session is taken lazily and released `idle_release_s` after the
  last request, so between runs banco holds nothing.
- Crate unreachable / session refused → 503 with the reason; the VMM shim
  keeps retrying and says so in the `vmm_hv_control` pane.
- A reading older than `stale_s` is never served, and the VMM side never
  writes a row from a snapshot older than `hv_log.max_age_s` — a frozen
  service leaves a gap in the CSV instead of a flat line that was never
  measured.
