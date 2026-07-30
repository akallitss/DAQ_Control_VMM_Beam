#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dream-side trigger routes for combined VMM + Dream runs (DEPLOYED 2026-07-30).

Adds the /vmm_trigger/start and /vmm_trigger/stop routes that let the VMM DAQ
fire a paired Dream run with the same run name and inject per-subrun P2-basket
HV targets (HV scans). Everything else about the triggered run is a completely
normal Dream run: all detectors' HV, monitoring, recording, processing.

Deployment (see README_DEPLOY.md next to this file):
  1. copy this file to ~/DAQ_Control_Dream_Beam/flask_app/vmm_trigger.py
  2. in flask_app/app.py add, BEFORE the `if __name__ == "__main__"` block:
         import vmm_trigger
         vmm_trigger.register(app, BASE_DIR, CONFIG_RUN_DIR, BASH_DIR,
                              VENV_PYTHON, _save_current_run)
  3. create gitignored config/vmm_trigger.json (token, allow_ips, hv_limits)
  4. restart the Dream flask (NOT the DAQ) between runs.

This file is the source of truth for the deployed copy: edit it here and
re-copy, so the VMM repo keeps the record of what runs on banco.

Design notes:
  - Passive: nothing happens unless the route is called with the right token.
  - The route generates a normal Dream run config (run_config_beam.py), then
    retargets it at the VMM's run name and rebuilds sub_runs from the VMM
    payload: each subrun keeps Dream's own template hvs (uRWELLs at operating
    point) and deep-merges the incoming basket targets on top.
  - Every incoming voltage is bound-checked against BOTH config/vmm_trigger.json
    hv_limits and Dream's own MAX_HV, whichever is lower — out of range refuses
    the whole run (the VMM side then aborts its start too; nothing runs at a
    wrong voltage). Reading MAX_HV back from run_config_beam.py means lowering a
    ceiling there cannot be silently outvoted by a stale hv_limits entry.
  - Refuses if a Dream run is already in progress (start_run.sh types into the
    daq_control tmux — starting over a live run must never happen).
  - The run config json is written as <run_name>.json so the VMM side can point
    /hv_data at it (run_param in the response).
"""

import json
import os
import subprocess
import time


def _load_trigger_cfg(base_dir):
    with open(os.path.join(base_dir, 'config', 'vmm_trigger.json')) as f:
        return json.load(f)


def _check_limits(sub_runs, limits):
    """Every incoming basket target within its configured limit.
    Returns None if OK, else a refusal message. Unknown channels refuse too —
    the VMM side must only send channels Dream knows the limits of (limits come
    from Dream's MAX_HV and/or vmm_trigger.json; see _effective_limits)."""
    for sr in sub_runs:
        for slot, channels in (sr.get('hvs') or {}).items():
            for ch, v0 in channels.items():
                if v0 is None:
                    continue
                key = f'{slot}:{ch}'
                if key not in limits:
                    return (f'channel {key} has no configured limit — add it to '
                            f'vmm_trigger.json hv_limits (or to Dream MAX_HV/DET_HV)')
                if float(v0) > float(limits[key]):
                    return f'channel {key}: {v0} V exceeds limit {limits[key]} V'
    return None


def _dream_max_hv(venv_python, base_dir):
    """{'slot:ch': max_volts} from Dream's own MAX_HV x DET_HV, or {} if it
    can't be read.

    Read in a subprocess rather than imported: importing run_config_beam into
    the live Flask process would run its setpoint asserts and its DAQ_MESH_*
    os.environ.setdefault calls inside the GUI. Last stdout line only, so a
    stray print from the import (e.g. an active DAQ_HV_OVERRIDE) can't break
    the parse."""
    code = ('import json, run_config_beam as r; '
            'print(json.dumps({f"{c}:{ch}": r.MAX_HV[d][role] '
            'for d, m in r.DET_HV.items() for role, (c, ch) in m.items() '
            'if role in r.MAX_HV.get(d, {})}))')
    try:
        out = subprocess.run([venv_python, '-c', code], cwd=base_dir,
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f'[vmm_trigger] MAX_HV unreadable from run_config_beam.py ({e}) '
              f'— falling back to the vmm_trigger.json hv_limits alone')
        return {}


def _effective_limits(cfg_limits, dream_limits):
    """Per-channel ceiling = the LOWER of the trigger config's limit and Dream's
    own MAX_HV, so a ceiling cannot be raised by editing only one of the two."""
    limits = {}
    for key in set(cfg_limits) | set(dream_limits):
        candidates = [float(v) for v in (cfg_limits.get(key), dream_limits.get(key))
                      if v is not None]
        if candidates:
            limits[key] = min(candidates)
    return limits


def _retarget_run_name(run_cfg, run_name):
    """Point run_name AND every path derived from it at the triggered run.

    run_config_beam.py builds five paths from the run name at generation time
    (and config/gui_run_config.json, when enabled, overrides run_name after
    that), so setting run_cfg['run_name'] alone would leave the paired run
    writing into the template run's directories — and the VMM shim's /hv_data
    gate polling that other run's hv_monitor.csv. Same re-derivation
    run_config_beam.py does for a GUI config."""
    run_cfg['run_name'] = run_name
    base_dir = run_cfg.get('base_out_dir', '')
    run_out_dir = f"{run_cfg.get('data_out_dir') or f'{base_dir}runs/'}{run_name}/"
    run_cfg['run_out_dir'] = run_out_dir
    for section, key in (('dream_daq_info', 'data_out_dir'),
                         ('processor_info', 'run_dir'),
                         ('hv_info', 'run_out_dir')):
        if isinstance(run_cfg.get(section), dict):
            run_cfg[section][key] = run_out_dir
    if isinstance(run_cfg.get('dream_daq_info'), dict):
        run_cfg['dream_daq_info']['run_directory'] = f'{base_dir}dream_run/{run_name}/'


def _merge_hvs(template_hvs, basket_hvs):
    """Dream's template hvs (uRWELL operating point + basket defaults) with the
    incoming basket targets merged on top."""
    merged = {slot: dict(chs) for slot, chs in (template_hvs or {}).items()}
    for slot, channels in (basket_hvs or {}).items():
        merged.setdefault(slot, {}).update(channels)
    return merged


def register(app, base_dir, config_run_dir, bash_dir, venv_python,
             save_current_run):
    from flask import request, jsonify

    def _authorized(data):
        try:
            cfg = _load_trigger_cfg(base_dir)
        except Exception:
            return None, 'vmm_trigger not configured on the Dream side'
        if data.get('token') != cfg.get('token') or not cfg.get('token'):
            return None, 'bad token'
        allow = cfg.get('allow_ips')
        if allow and request.remote_addr not in allow:
            return None, f'ip {request.remote_addr} not allowed'
        return cfg, None

    def _dream_run_active():
        """True if the daq_control tmux pane is mid-run (foreground python)."""
        try:
            out = subprocess.run(
                ['tmux', 'display-message', '-p', '-t', 'daq_control',
                 '#{pane_current_command}'],
                capture_output=True, text=True, timeout=5).stdout.strip()
            return out.startswith('python')
        except Exception:
            return True  # can't tell -> refuse, never start over a live run

    @app.route('/vmm_trigger/start', methods=['POST'])
    def vmm_trigger_start():
        data = request.get_json(silent=True) or {}
        cfg, err = _authorized(data)
        if err:
            return jsonify({'success': False, 'message': err}), 403
        run_name = data.get('run_name', '').strip()
        sub_runs_in = data.get('sub_runs') or []
        if not run_name or not sub_runs_in:
            return jsonify({'success': False,
                            'message': 'run_name and sub_runs required'}), 400
        if _dream_run_active():
            return jsonify({'success': False,
                            'message': 'a Dream run is already in progress'}), 409
        limits = _effective_limits(cfg.get('hv_limits', {}),
                                   _dream_max_hv(venv_python, base_dir))
        refusal = _check_limits(sub_runs_in, limits)
        if refusal:
            return jsonify({'success': False,
                            'message': f'HV refused: {refusal}'}), 422

        # Normal Dream run config as the template (commissioning plan — the
        # schedule comes from the VMM payload, not from a Dream scan plan).
        # DAQ_RUN_NAME so the generator itself derives the run's paths; they are
        # re-derived below anyway, for the case where an enabled GUI run config
        # overrides run_name after that.
        env = dict(os.environ, DAQ_RUN_PLAN='commissioning',
                   DAQ_RUN_NAME=run_name)
        gen = subprocess.run([venv_python, f'{base_dir}/run_config_beam.py'],
                             cwd=base_dir, env=env, capture_output=True,
                             text=True, timeout=120)
        if gen.returncode != 0:
            # Never fall through to a stale run_config_beam.json: its setpoints
            # and FEU set would be some earlier run's, not the current config's.
            return jsonify({'success': False,
                            'message': 'run_config_beam.py failed on the Dream '
                                       f'side: {gen.stderr.strip()[-500:]}'}), 500
        template_path = os.path.join(config_run_dir, 'run_config_beam.json')
        with open(template_path) as f:
            run_cfg = json.load(f)
        template_sr = (run_cfg.get('sub_runs') or [{}])[0]

        _retarget_run_name(run_cfg, run_name)
        run_cfg['triggered_by'] = f'vmm_daq@{request.remote_addr}'

        # Optional HV hold across a SEQUENCE of runs. A chip-configuration scan
        # changes only the config file, so powering the crate down and ramping
        # back up between every run costs ~4 min each and cycles the detectors
        # needlessly. The VMM side can ask for the crate to stay biased.
        #
        # Default UNCHANGED (Dream own config, normally True): only an explicit
        # false holds HV on, and the caller is then responsible for the LAST run
        # of its sequence powering off. Logged either way, so a crate left
        # biased is always traceable to a request.
        if 'power_off_hv_at_end' in data:
            run_cfg['power_off_hv_at_end'] = bool(data['power_off_hv_at_end'])
            if not run_cfg['power_off_hv_at_end']:
                print(f'[vmm_trigger] {run_name}: HV HOLD requested - crate '
                      f'stays biased at the end of this run')
        run_cfg['sub_runs'] = [
            {**template_sr,
             'sub_run_name': sr.get('sub_run_name', f'sub_run_{i}'),
             **({'run_time': sr['run_time']} if 'run_time' in sr else {}),
             **({'post_pause_s': sr['post_pause_s']} if 'post_pause_s' in sr else {}),
             'hvs': _merge_hvs(template_sr.get('hvs'), sr.get('hvs'))}
            for i, sr in enumerate(sub_runs_in)
        ]
        out_path = os.path.join(config_run_dir, f'{run_name}.json')
        with open(out_path, 'w') as f:
            json.dump(run_cfg, f, indent=2)

        result = subprocess.run([f'{bash_dir}/start_run.sh', out_path],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({'success': False,
                            'message': f'start_run.sh failed: {result.stderr}'}), 500
        save_current_run(run_name)
        return jsonify({'success': True, 'run_param': f'{run_name}.json',
                        'message': f'Dream run {run_name} started '
                                   f'({len(run_cfg["sub_runs"])} sub-runs)'})

    @app.route('/vmm_trigger/stop', methods=['POST'])
    def vmm_trigger_stop():
        data = request.get_json(silent=True) or {}
        cfg, err = _authorized(data)
        if err:
            return jsonify({'success': False, 'message': err}), 403
        result = subprocess.run([f'{bash_dir}/stop_run.sh'],
                                capture_output=True, text=True, timeout=30)
        ok = result.returncode == 0
        return jsonify({'success': ok,
                        'message': 'Dream stop requested' if ok
                                   else f'stop_run.sh failed: {result.stderr}'})
