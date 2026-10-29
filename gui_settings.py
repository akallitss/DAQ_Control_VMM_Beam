#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Settings the operator changes from the GUI's Settings tab, without editing code.

The values written in the code stay the DEFAULTS. config/gui_settings.json
holds only what someone changed in the tab (plus who and when), and
config/gui_settings_history.jsonl keeps every change. Each place that reads a
setting asks this module:

    RUN_PLAN = gui_settings.value('run_plan', RUN_PLAN)

value() returns the GUI's value if one is set, else the code default, and
remembers the default so the tab can show it. Nothing here is read only once
at import by a long-lived process that should follow changes: the run config,
processor and QA configs are generated fresh at each start, the live monitor's
command is built at each Start, and mem_guardian reads them when restarted.
When each setting takes effect is in its 'applies' field and shown in the tab.

@author: Alexandra Kallitsopoulou
"""

import json
import os
import re
import tempfile
from datetime import datetime

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(REPO_DIR, 'config', 'gui_settings.json')
HISTORY_PATH = os.path.join(REPO_DIR, 'config', 'gui_settings_history.jsonl')
# config_scan.py has always written this file and run_config_beam.py has always
# let it override CONFIG_SCAN_SUBRUN_MIN; the tab edits that same file, so the
# two can never disagree.
SCAN_MIN_PATH = os.path.join(REPO_DIR, 'config', 'scan_subrun_min')

# applies: when a change takes effect
NEXT_RUN = 'next Start Run'
PROCESSOR = 'next Start Processor'
QA = 'next Start QA'
LIVE = 'next Start of the Live monitor'
GUARDIAN = 'restart of the memory guardian'

PLANS = ['config_scan', 'lab_source', 'trigger_test', 'operating_long', 'hv_scan',
         'mesh_then_drift', 'mesh_then_drift_common', 'retake_run25']
HV_PLANS = ('mesh_then_drift', 'mesh_then_drift_common', 'retake_run25', 'hv_scan')


def _i(lo, hi):
    return {'type': 'int', 'min': lo, 'max': hi}


def _f(lo, hi):
    return {'type': 'float', 'min': lo, 'max': hi}


def _c(*choices):
    return {'type': 'choice', 'choices': list(choices)}


# key -> definition. group/label/help are shown in the tab.
SCHEMA = {
    # --- run ------------------------------------------------------------------
    'run_plan': {'group': 'run', 'label': 'Run type', 'applies': NEXT_RUN, **_c(*PLANS),
                 'help': "What a Start Run records: 'lab_source' N identical sub-runs "
                         "(cosmics, source, pulser); 'config_scan' one sub-run per chip "
                         "configuration at fixed HV; 'hv_scan' a voltage scan defined "
                         "below; 'trigger_test' and 'operating_long' as at the beam. "
                         "Voltage scans only where this DAQ sets the HV."},
    'config_scan_subrun_min': {'group': 'run', 'label': 'config_scan: minutes per run',
                               'applies': NEXT_RUN, **_i(1, 600), 'file': SCAN_MIN_PATH,
                               'help': "Stored in config/scan_subrun_min, which config_scan.py "
                                       "also writes when it schedules a night.",
                               'plans': ['config_scan']},
    'lab_measurement': {'group': 'run', 'label': 'lab_source: what is measured',
                        'applies': NEXT_RUN,
                        **_c('cosmics', 'fe55', 'am241', 'pulser', 'noise'),
                        'help': 'Names the sub-runs (cosmics_00, ...) and is recorded in '
                                'every run config as what produced the hits.'},
    'lab_n_subruns': {'group': 'run', 'label': 'lab_source: number of sub-runs',
                      'applies': NEXT_RUN, **_i(1, 100), 'help': '', 'plans': ['lab_source']},
    'lab_subrun_min': {'group': 'run', 'label': 'lab_source: minutes per sub-run',
                       'applies': NEXT_RUN, **_i(1, 600),
                       'help': 'Each sub-run boundary is an acquisition stop/start, the known '
                               'non-ready-hybrid risk: 30-60 min is sensible overnight.',
                       'plans': ['lab_source']},
    'trigger_test_min': {'group': 'run', 'label': 'trigger_test: minutes', 'applies': NEXT_RUN,
                         **_i(1, 120), 'help': '', 'plans': ['trigger_test']},
    'operating_long_n_subruns': {'group': 'run', 'label': 'operating_long: number of sub-runs',
                                 'applies': NEXT_RUN, **_i(1, 50), 'help': '',
                                 'plans': ['operating_long']},
    'operating_long_subrun_min': {'group': 'run', 'label': 'operating_long: minutes per sub-run',
                                  'applies': NEXT_RUN, **_i(1, 600), 'help': '',
                                  'plans': ['operating_long']},
    'scan_subrun_min': {'group': 'run', 'label': 'Beam-test HV scans: minutes per point',
                        'applies': NEXT_RUN, **_i(1, 600), 'help': '',
                        'plans': ['mesh_then_drift', 'mesh_then_drift_common', 'retake_run25']},
    'hv_scan_detectors': {'group': 'run', 'label': 'Voltage scan: detectors stepped',
                          'applies': NEXT_RUN, 'type': 'list', 'plans': ['hv_scan'],
                          'help': 'The others stay at their operating point.'},
    'hv_scan_mode': {'group': 'run', 'label': 'Voltage scan: what moves', 'applies': NEXT_RUN,
                     **_c('mesh_and_drift', 'mesh', 'drift'), 'plans': ['hv_scan'],
                     'help': "'mesh_and_drift' steps both by the same amount (each detector "
                             "keeps its operating gap); 'mesh' or 'drift' steps one only."},
    'hv_scan_start_v': {'group': 'run', 'label': 'Voltage scan: first point (V)',
                        'applies': NEXT_RUN, **_i(0, 900), 'plans': ['hv_scan'],
                        'help': 'Of the mesh (or of the drift in drift mode).'},
    'hv_scan_step_v': {'group': 'run', 'label': 'Voltage scan: step (V, negative = down)',
                       'applies': NEXT_RUN, **_i(-200, 200), 'plans': ['hv_scan'], 'help': ''},
    'hv_scan_points': {'group': 'run', 'label': 'Voltage scan: number of points',
                       'applies': NEXT_RUN, **_i(1, 50), 'plans': ['hv_scan'], 'help': ''},
    'hv_scan_min_per_point': {'group': 'run', 'label': 'Voltage scan: minutes per point',
                              'applies': NEXT_RUN, **_i(1, 600), 'plans': ['hv_scan'],
                              'help': 'Every point the same length, so points compare.'},
    'capture_duration_s': {'group': 'run', 'label': 'Seconds per capture file',
                           'applies': NEXT_RUN, **_f(10, 3600),
                           'help': 'dumpcap starts a new pcapng this often. QA latency and '
                                   'the capture guard trip time scale with it.'},

    # --- readout and monitoring ------------------------------------------------
    'processor_engine': {'group': 'readout', 'label': 'Processor engine', 'applies': PROCESSOR,
                         **_c('p2', 'legacy'),
                         'help': "'p2' = p2-monitor-reduce and QA plots from histograms; "
                                 "'legacy' = vmm_qa/vmm_reduce.py and vmm_pcapng_qa.py."},
    'p2_keep_columns': {'group': 'readout', 'label': 'Keep per-hit columns (p2 engine)',
                        'applies': PROCESSOR, 'type': 'bool',
                        'help': 'Costs ~3.4x the capture on disk and ~3x the reduce time; '
                                'nothing online needs them with the p2 engine.'},
    'qa_plot_policy': {'group': 'readout', 'label': 'QA plots (p2 engine)', 'applies': QA,
                       **_c('always', 'subrun', 'never'),
                       'help': "'always' every capture (~25 s each on this box), 'subrun' "
                               "the first capture of each sub-run, 'never' trend only."},
    'live_source': {'group': 'readout', 'label': 'Live monitor: default source',
                    'applies': LIVE, **_c('file', 'udp'),
                    'help': 'What the Live tab selects by default: the capture files or the '
                            'network port.'},
    'live_udp_address': {'group': 'readout', 'label': 'Live monitor: network address',
                         'applies': LIVE, 'type': 'hostport',
                         'help': "The FEC's destination, HOST:PORT on the data NIC."},
    'live_window_s': {'group': 'readout', 'label': 'Live: seconds of data per rate',
                      'applies': LIVE, **_i(5, 3600), 'help': ''},
    'live_interval_s': {'group': 'readout', 'label': 'Live (files): seconds between polls',
                        'applies': LIVE, **_i(1, 60), 'help': ''},
    'live_max_read_mb': {'group': 'readout', 'label': 'Live (files): MB read per poll at most',
                         'applies': LIVE, **_i(1, 256),
                         'help': 'Bounds the CPU the monitor takes; under a flood it samples.'},
    'live_max_behind_mb': {'group': 'readout', 'label': 'Live (files): skip ahead beyond MB',
                           'applies': LIVE, **_i(8, 4096), 'help': ''},
    'live_udp_interval_s': {'group': 'readout', 'label': 'Live (network): seconds between polls',
                            'applies': LIVE, **_i(1, 60),
                            'help': 'The socket buffer holds ~4 MB: poll often.'},
    'live_udp_max_read_mb': {'group': 'readout', 'label': 'Live (network): MB decoded per poll',
                             'applies': LIVE, **_i(1, 64),
                             'help': 'The kernel drops (and counts) what is not read; rates '
                                     'are scaled by the fraction decoded.'},
    'live_rss_limit_mb': {'group': 'readout', 'label': 'Live: own memory cap (MB)',
                          'applies': LIVE, **_i(200, 4000), 'help': ''},
    'live_plot_every_s': {'group': 'readout', 'label': 'Live: seconds between plot redraws',
                          'applies': LIVE, **_i(10, 3600), 'help': ''},

    # --- guards ---------------------------------------------------------------
    'proc_memory_kill_pct': {'group': 'guards', 'label': 'Processor: kill a decode above RAM %',
                             'applies': PROCESSOR, **_i(50, 95), 'help': ''},
    'proc_stall_timeout_s': {'group': 'guards', 'label': 'Processor: kill a stalled decode after s',
                             'applies': PROCESSOR, **_i(60, 7200), 'help': ''},
    'proc_hard_timeout_s': {'group': 'guards', 'label': 'Processor: hard limit per capture (s)',
                            'applies': PROCESSOR, **_i(300, 86400), 'help': ''},
    'proc_max_attempts': {'group': 'guards', 'label': 'Processor: attempts per capture',
                          'applies': PROCESSOR, **_i(1, 10), 'help': ''},
    'proc_cpu_nice': {'group': 'guards', 'label': 'Processor: nice level', 'applies': PROCESSOR,
                      **_i(0, 19), 'help': '19 = lowest priority.'},
    'proc_threads': {'group': 'guards', 'label': 'Processor: thread cap', 'applies': PROCESSOR,
                     **_i(1, 64), 'help': ''},
    'qa_memory_kill_pct': {'group': 'guards', 'label': 'QA: kill a plot job above RAM %',
                           'applies': QA, **_i(50, 95), 'help': ''},
    'qa_max_attempts': {'group': 'guards', 'label': 'QA: attempts per capture', 'applies': QA,
                        **_i(1, 10), 'help': ''},
    'qa_cpu_nice': {'group': 'guards', 'label': 'QA: nice level', 'applies': QA,
                    **_i(0, 19), 'help': ''},
    'qa_threads': {'group': 'guards', 'label': 'QA: thread cap', 'applies': QA,
                   **_i(1, 64), 'help': ''},
    'guardian_kill_avail_mb': {'group': 'guards',
                               'label': 'Memory guardian: kill a job below MB available',
                               'applies': GUARDIAN, **_i(300, 4000),
                               'help': 'Kills the largest restartable job (reduce, QA, live) '
                                       'when free memory drops below this.'},
    'guardian_warn_avail_mb': {'group': 'guards',
                               'label': 'Memory guardian: warn below MB available',
                               'applies': GUARDIAN, **_i(400, 8000),
                               'help': 'Must be above the kill threshold.'},
}

GROUPS = {'run': 'Run', 'readout': 'Readout and monitoring', 'guards': 'Processor and QA guards'}

_DEFAULTS = {}      # key -> code default, recorded by value()


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _read_file_value(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def overrides():
    """{key: value} set from the GUI (only valid keys, values as stored)."""
    out = {k: v for k, v in _read_json(SETTINGS_PATH).get('values', {}).items() if k in SCHEMA}
    for k, d in SCHEMA.items():
        if 'file' in d:
            raw = _read_file_value(d['file'])
            if raw:
                try:
                    out[k] = coerce(k, raw)
                except ValueError:
                    pass
    return out


def value(key, default):
    """The GUI's value for key if set (and valid), else the code default."""
    _DEFAULTS[key] = default
    ov = overrides()
    if key not in ov:
        return default
    try:
        return coerce(key, ov[key])
    except ValueError:
        return default


def coerce(key, v):
    """Check one value against SCHEMA; return it typed, or raise ValueError."""
    d = SCHEMA[key]
    t = d['type']
    try:
        if t == 'int':
            if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                raise ValueError
            v = int(str(v).strip())
        elif t == 'float':
            v = float(str(v).strip())
        elif t == 'bool':
            if isinstance(v, str):
                if v.lower() not in ('true', 'false', '1', '0'):
                    raise ValueError
                v = v.lower() in ('true', '1')
            v = bool(v)
        elif t == 'choice':
            v = str(v)
            if v not in d['choices']:
                raise ValueError
        elif t == 'list':
            if isinstance(v, str):
                v = [x.strip() for x in v.split(',') if x.strip()]
            if not isinstance(v, (list, tuple)) or not v or \
                    not all(isinstance(x, str) and x for x in v) or len(set(v)) != len(v):
                raise ValueError
            v = list(v)
        elif t == 'hostport':
            v = str(v).strip()
            m = re.fullmatch(r'(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})', v)
            if not m or any(int(x) > 255 for x in m.groups()[:4]) or \
                    not 1 <= int(m.group(5)) <= 65535:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError(f"{d['label']}: {v!r} is not a valid "
                         + (f"choice ({', '.join(d['choices'])})" if t == 'choice'
                            else 'non-empty list without repeats' if t == 'list'
                            else 'HOST:PORT (e.g. 192.168.1.16:6006)' if t == 'hostport'
                            else t))
    if t in ('int', 'float') and not d['min'] <= v <= d['max']:
        raise ValueError(f"{d['label']}: {v} is outside {d['min']}-{d['max']}")
    return v


def check_together(vals, hv_targets=True, stations=None, ceilings=None):
    """Checks across settings, on the values that would be in force. List of errors.

    stations: the detectors of the stand now (for the voltage-scan list);
    ceilings: {'mesh': V, 'drift': V} from the setup file.
    """
    errs = []
    if vals.get('run_plan') == 'hv_scan':
        dets = vals.get('hv_scan_detectors') or []
        bad = [d for d in dets if stations is not None and d not in stations]
        if bad:
            errs.append(f"Voltage scan: {', '.join(bad)} is not a detector of the setup "
                        f"({', '.join(stations)}).")
        step, n, v0 = (vals.get('hv_scan_step_v'), vals.get('hv_scan_points'),
                       vals.get('hv_scan_start_v'))
        if step == 0 and (n or 0) > 1:
            errs.append('Voltage scan: the step is 0, so every point would be the same.')
        if None not in (step, n, v0):
            last = v0 + (n - 1) * step
            role = 'drift' if vals.get('hv_scan_mode') == 'drift' else 'mesh'
            top = (ceilings or {}).get(role)
            if last < 0:
                errs.append(f'Voltage scan: the last point would be {last} V.')
            if top is not None and max(v0, last) > top:
                errs.append(f'Voltage scan: {max(v0, last)} V is above the {role} ceiling '
                            f'({top:g} V).')
    kill, warn = vals.get('guardian_kill_avail_mb'), vals.get('guardian_warn_avail_mb')
    if kill is not None and warn is not None and warn <= kill:
        errs.append(f'Memory guardian: the warn threshold ({warn} MB) must be above the '
                    f'kill threshold ({kill} MB).')
    if not hv_targets and vals.get('run_plan') in HV_PLANS:
        errs.append(f"Run plan {vals['run_plan']!r} steps the HV, but this site does not set "
                    "the HV (it is held on banco). Use 'config_scan' or 'lab_source'.")
    if vals.get('processor_engine') == 'legacy' and vals.get('qa_plot_policy') == 'always':
        errs.append("QA plots 'always' needs the p2 engine: the legacy QA script takes "
                    "~45 s and GBs of memory per capture.")
    return errs


def _atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.' + os.path.basename(path))
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def save(changes, who='gui', effective=None, hv_targets=True, stations=None, ceilings=None):
    """Apply {key: value or None}; None resets the key to the code default.

    Validates every value and the combination first; writes nothing on any
    error. effective: {key: value in force now}, for the combined checks.
    Returns (ok, errors, applied changes as {key: (old, new)}).
    """
    errs, typed = [], {}
    for k, v in changes.items():
        if k not in SCHEMA:
            errs.append(f'unknown setting {k!r}')
            continue
        if v is None or v == '':
            typed[k] = None
            continue
        try:
            typed[k] = coerce(k, v)
        except ValueError as e:
            errs.append(str(e))
    if errs:
        return False, errs, {}
    current = overrides()
    after = dict(effective or {})
    for k, v in typed.items():
        after[k] = v if v is not None else _DEFAULTS.get(k, after.get(k))
    errs = check_together(after, hv_targets=hv_targets, stations=stations, ceilings=ceilings)
    if errs:
        return False, errs, {}

    data = _read_json(SETTINGS_PATH)
    vals = data.get('values', {})
    applied = {}
    for k, v in typed.items():
        old = current.get(k)
        if old == v:
            continue
        applied[k] = (old, v)
        if 'file' in SCHEMA[k]:
            if v is None:
                try:
                    os.remove(SCHEMA[k]['file'])
                except FileNotFoundError:
                    pass
            else:
                _atomic_write(SCHEMA[k]['file'], f'{v}\n')
        elif v is None:
            vals.pop(k, None)
        else:
            vals[k] = v
    if not applied:
        return True, [], {}
    now = datetime.now().isoformat(timespec='seconds')
    data.update(values=vals, updated=now, updated_by=who)
    _atomic_write(SETTINGS_PATH, json.dumps(data, indent=2) + '\n')
    with open(HISTORY_PATH, 'a') as f:
        for k, (old, new) in applied.items():
            f.write(json.dumps({'time': now, 'who': who, 'key': k,
                                'old': old, 'new': new}) + '\n')
    return True, [], applied


def history(n=30):
    try:
        with open(HISTORY_PATH) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out
