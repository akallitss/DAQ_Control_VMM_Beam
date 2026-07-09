#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Status cards for the VMM SPS DAQ flask GUI: each get_*_status() captures the
tail of one vmm_* tmux pane and reduces it to {status, color, fields}.

Adapted from Dylan Neff's Dream daq_status.py: dream_daq -> vmm_daq (dumpcap
status lines instead of RunCtrl), processor/pedestal watchers dropped, LV
control card added.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's original)
"""

import subprocess
import re


""" Colors:
- danger (red)
- warning (yellow)
- success (green)
- info (blue)
- primary (dark blue)
- secondary (grey)
- light (light grey)
- dark (black)
"""

def get_vmm_daq_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-500", "-t", "vmm_daq:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "vmm_daq tmux not running"}]
        }

    lines = [l for l in output.splitlines() if l.strip()]

    # Most recent capture status line:
    # [vmm daq] status subrun=<name> elapsed=0h 1m 30s files=3 mb=61.2 file=<newest>
    fields = []
    status_seen = False
    for line in reversed(lines):
        m = re.search(r'\[vmm daq\] status subrun=(\S+)\s+elapsed=(\d+h \d+m \d+s)'
                      r'\s+files=(\d+)\s+mb=([\d.]+)', line)
        if m:
            fields = [
                {"label": "Subrun",   "value": m.group(1)},
                {"label": "Run Time", "value": m.group(2)},
                {"label": "Files",    "value": m.group(3)},
                {"label": "Data",     "value": f"{float(m.group(4)):,.0f} MB"},
            ]
            status_seen = True
            break

    for line in reversed(lines):
        if "CAPTURE ERROR" in line:
            m = re.search(r'iface=(\S+)\s+rc=(\S+)', line)
            detail = [{"label": "Capture", "value": f"{m.group(1)} rc={m.group(2)}"}] if m else []
            return {"status": "CAPTURE ERROR", "color": "danger", "fields": fields + detail}
        if "Stop flag found" in line:
            return {"status": "Stopping", "color": "warning", "fields": fields}
        if "Sent: VMM DAQ stopped" in line:
            return {"status": "DAQ Stopped", "color": "info", "fields": []}
        if "[vmm daq] status subrun=" in line or "[vmm daq] Starting capture" in line:
            return {"status": "RUNNING", "color": "success", "fields": fields}
        if "Received: Start" in line:
            return {"status": "STARTING", "color": "warning", "fields": fields}
        if "Listening on " in line:
            return {"status": "WAITING", "color": "secondary", "fields": []}

    if status_seen:
        return {"status": "RUNNING", "color": "success", "fields": fields}
    return {"status": "UNKNOWN STATE", "color": "danger", "fields": []}


def get_hv_control_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "vmm_hv_control:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "vmm_hv_control tmux not running"}]
        }

    # Default status/color rules
    rules = [
        ("Listening on ", "WAITING", "secondary"),
        ("Powering off HV", "HV Off", "secondary"),
        ("HV Powered Off", "HV Off", "secondary"),
        ("Monitoring HV", "Monitoring HV", "success"),
        ("HV Ramped", "HV Ramped", "success"),
        ("Setting HV", "Ramping HV", "warning"),
        ("Checking HV ramp", "Ramping HV", "warning"),
        ("Waiting for HV to ramp", "Ramping HV", "warning"),
    ]

    # Determine overall status/color from most recent matching line
    status, color = "UNKNOWN STATE", "danger"
    for line in reversed(output.splitlines()):
        for flag, s, c in rules:
            if flag in line:
                status, color = s, c
                break
        if status != "UNKNOWN STATE":
            break

    return {"status": status, "color": color, "fields": []}


def get_lv_control_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "vmm_lv_control:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "vmm_lv_control tmux not running"}]
        }

    lines = [l for l in output.splitlines() if l.strip()]

    # A unit currently disconnected trumps everything (reconnects are retried
    # during monitoring, so only flag if no later 'reconnected' line).
    disconnected = set()
    for line in lines:
        m = re.search(r'\[lv\] (\S+) DISCONNECTED', line)
        if m:
            disconnected.add(m.group(1))
        m = re.search(r'\[lv\] (\S+) (?:reconnected|connected)', line)
        if m:
            disconnected.discard(m.group(1))
    if disconnected:
        return {"status": "LV Disconnected", "color": "danger",
                "fields": [{"label": "Units", "value": ", ".join(sorted(disconnected))}]}

    rules = [
        ("Monitoring LV", "Monitoring LV", "success"),
        ("Sent: Stopping LV monitor", "IDLE", "info"),
        ("Sent: Starting LV monitor", "Monitoring LV", "success"),
        ("LV Connected", "LV Connected", "info"),
        ("Listening on ", "WAITING", "secondary"),
    ]
    for line in reversed(lines):
        for flag, status, color in rules:
            if flag in line:
                return {"status": status, "color": color, "fields": []}

    return {"status": "UNKNOWN STATE", "color": "danger", "fields": []}


def get_daq_control_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "vmm_daq_control:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "vmm_daq_control tmux not running"}]
        }

    rules = [
        ("Daq control session started", "WAITING", "secondary"),
        ("Run complete", "Run Complete", "info"),
        ("donzo", "Run Complete", "info"),
        ("[pause] Paused after sub-run", "Paused", "info"),
        ("[pause] Post-sub-run pause: waiting", "Paused", "info"),
        ("Finished with sub run ", "Finished Sub Run", "warning"),
        ("Prepping DAQs for ", "Prepping DAQs", "warning"),
        ("Ramping HVs for ", "Ramping HV", "warning"),
        ("Starting DAQ Control", "STARTING", "warning"),
        ("Received: VMM DAQ starting", "RUNNING", "success"),
        ("Stopping DAQ process", "Stopping DAQ", "warning"),
    ]

    fields = []
    for line in reversed(output.splitlines()):
        m = re.search(r'\[status\] run=(\S+)\s+subrun=(\S+)\s+run_time=(\S+)', line)
        if m:
            fields.append({"label": "Run",     "value": m.group(1)})
            fields.append({"label": "Subrun",  "value": m.group(2)})
            fields.append({"label": "Run Time", "value": m.group(3)})
            break

    for line in reversed(output.splitlines()):
        for flag, status, color in rules:
            if flag in line:
                return {"status": status, "color": color, "fields": fields}

    return {"status": "UNKNOWN STATE", "color": "danger", "fields": fields}


def get_qa_watcher_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "vmm_qa_watcher:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {"status": "STOPPED", "color": "secondary", "fields": []}

    lines = [l for l in output.splitlines() if l.strip()]

    # Most recent QA launch line:
    # [qa_watcher] run_1/sub_a/enp4s0f1_00001_20260709.pcapng  size=650MB  mem=...
    fields = []
    for line in reversed(lines):
        m = re.search(r'\[qa_watcher\] (\S+)/(\S+)/(\S+\.pcapn?g)\s+size=(\S+)', line)
        if m:
            fields = [
                {"label": "Run",    "value": m.group(1)},
                {"label": "Subrun", "value": m.group(2)},
                {"label": "File",   "value": m.group(3)},
            ]
            break

    _noise = ("[qa_watcher] Marked stale",)
    for line in reversed(lines):
        if any(n in line for n in _noise):
            continue
        if re.search(r'\[qa_watcher\] \S+/\S+/\S+\.pcapn?g\s+size=', line):
            return {"status": "Running QA",  "color": "success", "fields": fields}
        if "[qa_watcher]" in line and " idle " in line:
            return {"status": "IDLE",        "color": "info",    "fields": fields}
        if "[qa_watcher]" in line and "waiting for runs_dir" in line:
            return {"status": "Waiting for Dir", "color": "warning", "fields": []}
        if "[qa_watcher]" in line:
            return {"status": "RUNNING",     "color": "info",    "fields": fields}

    # Session alive but only unprefixed QA-script output in the window — the QA
    # subprocess is printing, so a QA is in progress.
    if lines:
        return {"status": "Running QA", "color": "success", "fields": fields}
    return {"status": "UNKNOWN", "color": "danger", "fields": []}


def get_backup_watcher_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "vmm_backup_watcher:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {"status": "STOPPED", "color": "secondary", "fields": []}

    lines = [l for l in output.splitlines() if l.strip()]

    fields = []
    for line in reversed(lines):
        m = re.search(r'\[backup\] (\S+)/(\S+)\s+size=', line)
        if m:
            fields = [
                {"label": "Run",    "value": m.group(1)},
                {"label": "Subrun", "value": m.group(2)},
            ]
            break

    # Pull rsync --info=progress2 line if present: "  1,234,567  45%  12.3MB/s  0:00:42 (xfr#1, to-chk=123/456)"
    progress_fields = []
    for line in lines:
        mp = re.search(r'([\d,]+)\s+(\d+)%\s+([\d.]+\s*\S+/s)\s+([\d:]+)(?:.*?to-chk=(\d+)/(\d+))?', line)
        if mp:
            pct, speed, eta = mp.group(2), mp.group(3).strip(), mp.group(4)
            progress_fields = [{"label": "Progress", "value": f"{pct}%  {speed}  eta {eta}"}]
            if mp.group(5) and mp.group(6):
                remaining, total = int(mp.group(5)), int(mp.group(6))
                progress_fields.append({"label": "Files", "value": f"{total - remaining}/{total}"})

    for line in reversed(lines):
        if "[backup] rsync ->" in line or "[backup] rsync done" in line:
            return {"status": "Syncing",         "color": "success", "fields": fields + progress_fields}
        m = re.search(r'\[backup\] extra sync(?! done| FAILED): (\S+)', line)
        if m:
            folder = m.group(1)
            return {"status": "Syncing",         "color": "success",
                    "fields": [{"label": "Folder", "value": folder}] + progress_fields}
        if "[backup] extra sync done" in line or "[backup] extra sync FAILED" in line:
            pass  # don't use these as the current status — keep scanning
        if "AUTH ERROR" in line or "Kerberos FAILED" in line:
            return {"status": "Auth Error",      "color": "danger",  "fields": fields}
        if "[backup] rsync FAILED" in line:
            return {"status": "rsync Error",     "color": "danger",  "fields": fields}
        if "[backup] Kerberos OK" in line and " idle " not in line:
            return {"status": "IDLE",            "color": "info",    "fields": fields}
        if "[backup]" in line and " idle " in line:
            return {"status": "IDLE",            "color": "info",    "fields": fields}
        if "[backup]" in line and "waiting for source_dir" in line:
            return {"status": "Waiting for Dir", "color": "warning", "fields": []}
        if "[backup]" in line:
            return {"status": "RUNNING",         "color": "info",    "fields": fields}

    return {"status": "UNKNOWN", "color": "danger", "fields": []}
