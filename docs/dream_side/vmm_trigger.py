#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STAGED PATCH for the Dream DAQ (banco) — NOT YET DEPLOYED.

Adds the /vmm_trigger/start and /vmm_trigger/stop routes that let the VMM DAQ
fire a paired Dream run with the same run name and inject per-subrun P2-basket
HV targets (HV scans). Everything else about the triggered run is a completely
normal Dream run: all detectors' HV, monitoring, recording, processing.

Deployment (see README_DEPLOY.md next to this file):
  1. copy this file to ~/DAQ_Control_Dream_Beam/flask_app/vmm_trigger.py
  2. in flask_app/app.py add, after the other route definitions:
         import vmm_trigger
         vmm_trigger.register(app, BASE_DIR, CONFIG_RUN_DIR, BASH_DIR,
                              VENV_PYTHON, _save_current_run)
  3. create gitignored config/vmm_trigger.json (token, hv_limits)
  4. restart the Dream flask (NOT the DAQ) between runs.

Design notes:
  - Passive: nothing happens unless the route is called with the right token.
  - The route generates a normal Dream run config (run_config_beam.py), then
    overrides run_name and rebuilds sub_runs from the VMM payload: each subrun
    keeps Dream's own template hvs (uRWELLs at operating point) and deep-merges
    the incoming basket targets on top.
  - Every incoming voltage is bound-checked against config/vmm_trigger.json
    hv_limits — out of range refuses the whole run (the VMM side then aborts
    its start too; nothing runs at a wrong voltage).
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
    the VMM side must only send channels Dream knows the limits of."""
    for sr in sub_runs:
        for slot, channels in (sr.get('hvs') or {}).items():
            for ch, v0 in channels.items():
                if v0 is None:
                    continue
                key = f'{slot}:{ch}'
                if key not in limits:
                    return f'channel {key} has no configured limit in vmm_trigger.json'
                if float(v0) > float(limits[key]):
                    return f'channel {key}: {v0} V exceeds limit {limits[key]} V'
    return None


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
        refusal = _check_limits(sub_runs_in, cfg.get('hv_limits', {}))
        if refusal:
            return jsonify({'success': False,
                            'message': f'HV refused: {refusal}'}), 422

        # Normal Dream run config as the template (commissioning plan — the
        # schedule comes from the VMM payload, not from a Dream scan plan).
        env = dict(os.environ, DAQ_RUN_PLAN='commissioning')
        subprocess.run([venv_python, f'{base_dir}/run_config_beam.py'],
                       env=env, timeout=60)
        template_path = os.path.join(config_run_dir, 'run_config_beam.json')
        with open(template_path) as f:
            run_cfg = json.load(f)
        template_sr = (run_cfg.get('sub_runs') or [{}])[0]

        run_cfg['run_name'] = run_name
        run_cfg['triggered_by'] = f'vmm_daq@{request.remote_addr}'
        run_cfg['sub_runs'] = [
            {**template_sr,
             'sub_run_name': sr.get('sub_run_name', f'sub_run_{i}'),
             **({'run_time': sr['run_time']} if 'run_time' in sr else {}),
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
