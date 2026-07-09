#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on September 29 9:36 PM 2025
Created in PyCharm
Created as Cosmic_Bench_DAQ_Control/daq_status.py

@author: Dylan Neff, Dylan
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

def get_dream_daq_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-500", "-t", "dream_daq:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "dream_daq tmux not running"}]
        }

    fields = []
    if "_TakePedThr" in output:
        status = "Taking Pedestals"
        color = "warning"
    elif "Scan trigger thresholds in process" in output:
        status = "Scanning Trigger Thresholds"
        color = "warning"
    elif "_TakeData:" in output:
        status = "RUNNING"
        color = "success"
        m_rt = re.search(r"RunTime\s+(\d+h\s+\d+m\s+\d+s)", output)
        if m_rt: fields.append({"label": "Run Time", "value": m_rt.group(1)})

        m_ir = re.search(r"IntRate=\s*([\d.]+\s*[A-Za-z]+)", output)
        if m_ir: fields.append({"label": "Int Rate", "value": m_ir.group(1)})

        # Live per-subrun event count (nb_of_events). Labeled "Subrun Events" to
        # distinguish it from the cumulative "Events this run" total shown up top.
        m_ev = re.search(r"nb_of_events=(\d+)", output)
        if m_ev: fields.append({"label": "Subrun Events", "value": m_ev.group(1)})

        # m_wait = re.search(
        #     r"wait for\s*((?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?)", output
        # )
        # if m_wait:
        #     h, m, s = m_wait.groups()
        #     # Fill in missing values as 0
        #     h = h or "0"
        #     m = m or "0"
        #     s = s or "0"
        #     fields.append({"label": "Wait For", "value": f"{h}h {m}m {s}s"})
    elif "Listening on " in output:
        status = "WAITING"
        color = "secondary"
    elif "Moving data files." in output or "Waiting for on-the-fly copy thread to finish" in output:
        status = "Copying fdfs"
        color = "info"
    elif "Sent: Dream DAQ stopped" in output:
        status = "DAQ Stopped"
        color = "info"
    else:
        status = "UNKNOWN STATE"
        color = "danger"

    return {"status": status, "color": color, "fields": fields}


def get_hv_control_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "hv_control:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "hv_control tmux not running"}]
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

    # Parse individual channel lines in the last block
    fields = []

    return {"status": status, "color": color, "fields": fields}



def get_daq_control_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "daq_control:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "daq_control tmux not running"}]
        }

    rules = [
        ("Daq control session started", "WAITING", "secondary"),
        ("Run complete", "Run Complete", "info"),
        ("donzo", "Run Complete", "info"),
        ("[pause] Paused after sub-run", "Paused", "info"),
        ("[pause] Post-sub-run pause: waiting", "Paused", "info"),
        ("Finished with sub run ", "Finished Sub Run", "warning"),
        ("Dream DAQ taking pedestals", "Prepping DAQs", "warning"),
        ("Prepping DAQs for ", "Prepping DAQs", "warning"),
        ("Ramping HVs for ", "Ramping HV", "warning"),
        ("Starting DAQ Control", "STARTING", "warning"),
        ("Dream DAQ starting", "RUNNING", "success"),
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


def get_processor_watcher_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "processor_watcher:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {"status": "STOPPED", "color": "secondary", "fields": []}

    lines = [l for l in output.splitlines() if l.strip()]

    # Find the most recent run/subrun/file_num context line
    # Format: [watcher] run_name/subrun_name  file_num=000  (N FEU(s))
    fields = []
    for line in reversed(lines):
        m = re.search(r'\[watcher\] (\S+)/(\S+)\s+file_num=(\d+)', line)
        if m:
            fields = [
                {"label": "Run",    "value": m.group(1)},
                {"label": "Subrun", "value": m.group(2)},
                {"label": "File #", "value": str(int(m.group(3)))},
            ]
            break

    # Lines that carry no status signal on their own — skip when determining state
    _noise = ("[watcher] Marked stale", "[analyze] Using pedestal",
              "[analyze] No pedestal",  "[analyze] Multiple pedestals",
              "[analyze] Cannot extract")

    for line in reversed(lines):
        if any(n in line for n in _noise):
            continue
        if "[watcher] Decoding pedestal:" in line:
            return {"status": "Ped. Decoding", "color": "success",  "fields": fields}
        if "[decode]" in line:
            return {"status": "Decoding",      "color": "success",  "fields": fields}
        if "[analyze]" in line:
            return {"status": "Analyzing",     "color": "success",  "fields": fields}
        if "[combine]" in line:
            return {"status": "Combining",     "color": "success",  "fields": fields}
        if "[cleanup]" in line:
            return {"status": "Cleaning Up",   "color": "success",  "fields": fields}
        if "[watcher]" in line and " idle " in line:
            return {"status": "IDLE",          "color": "info",     "fields": fields}
        if "[watcher]" in line and "waiting for runs_dir" in line:
            return {"status": "Waiting for Dir", "color": "warning", "fields": []}
        if "[watcher]" in line:
            return {"status": "RUNNING",       "color": "info",     "fields": fields}

    return {"status": "UNKNOWN", "color": "danger", "fields": []}


def get_qa_watcher_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "qa_watcher:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {"status": "STOPPED", "color": "secondary", "fields": []}

    lines = [l for l in output.splitlines() if l.strip()]

    # Extract most recent run/subrun context from a [qa_watcher] run/subrun line
    fields = []
    for line in reversed(lines):
        m = re.search(r'\[qa_watcher\] (\S+)/(\S+)', line)
        if m and 'idle' not in line and 'Marked stale' not in line \
                and 'waiting' not in line and 'runs_dir' not in line:
            fields = [
                {"label": "Run",    "value": m.group(1)},
                {"label": "Subrun", "value": m.group(2)},
            ]
            break

    _noise = ("[qa_watcher] Marked stale",)
    for line in reversed(lines):
        if any(n in line for n in _noise):
            continue
        m = re.search(r'\[qa\] (\S+) —', line)
        if m:
            return {"status": "Running QA",  "color": "success", "fields": fields + [{"label": "Detector", "value": m.group(1)}]}
        if "[qa_watcher]" in line and " idle " in line:
            return {"status": "IDLE",        "color": "info",    "fields": fields}
        if "[qa_watcher]" in line and "waiting for runs_dir" in line:
            return {"status": "Waiting for Dir", "color": "warning", "fields": []}
        if "[qa_watcher]" in line:
            return {"status": "RUNNING",     "color": "info",    "fields": fields}

    return {"status": "UNKNOWN", "color": "danger", "fields": []}


def get_backup_watcher_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "backup_watcher:0.0"],
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


def get_pedestal_watcher_status():
    """
    Minimal status for the pedestal QA watcher — rendered as a compact card
    ("small": True), so only running/analyzing/stopped, no detail fields.
    """
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-50", "-t", "pedestal_watcher:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {"status": "STOPPED", "color": "secondary", "small": True, "fields": []}

    def _st(status, color):
        return {"status": status, "color": color, "small": True, "fields": []}

    for line in reversed([l for l in output.splitlines() if l.strip()]):
        if "[ped_watcher]" in line and (" done" in line or " idle " in line):
            return _st("IDLE", "info")
        if "[ped_watcher]" in line and "waiting for pedestals_dir" in line:
            return _st("No Dir", "warning")
        if "[ped_watcher]" in line and " failed " in line:
            return _st("Failed", "warning")
        if "[ped_watcher]" in line and "n_pedthr=" in line:
            return _st("Analyzing", "success")
        if "[ped_watcher]" in line:
            return _st("RUNNING", "info")
        if "[decode]" in line or "[analyze]" in line or "[info]" in line or "[done]" in line:
            return _st("Analyzing", "success")

    # Session alive but no recognized line in the capture window (e.g. a long
    # unprefixed analysis printout scrolled everything out) — not an error.
    return _st("RUNNING", "info")


def get_decoder_status():
    try:
        output = subprocess.check_output(
            ["tmux", "capture-pane", "-pS", "-10", "-t", "decoder:0.0"],
            text=True
        )
    except subprocess.CalledProcessError:
        return {
            "status": "ERROR",
            "color": "danger",
            "fields": [{"label": "Details", "value": "decoder tmux not running"}]
        }

    rules = [
        ("Decoder started", "RUNNING", "success"),
        ("Starting Decoder", "STARTING", "warning"),
        ("Decoder stopped", "STOPPED", "warning"),
        ("Listening on ", "WAITING", "secondary"),
    ]

    for line in reversed(output.splitlines()):
        for flag, status, color in rules:
            if flag in line:
                return {"status": status, "color": color, "fields": []}

    return {"status": "UNKNOWN STATE", "color": "danger", "fields": []}
