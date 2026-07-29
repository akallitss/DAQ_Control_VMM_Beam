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
