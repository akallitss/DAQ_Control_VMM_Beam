#!/usr/bin/env python3
"""
Hybrid/ALINX LV power control: run the site's power scripts (e.g. the
TestBenchCERN switchOn_tdk.sh / switchOff_tdk.sh that drive the TDK-Lambda
supplies) from the GUI.

Machine-specific — configured in config/power_config.json (gitignored):

{
  "cwd": "/local/p2/p2testbench/TestBenchCERN",
  "path_prepend": ["/local/p2/p2equipment/LVPS/tdk_lambda",
                   "/local/p2/p2equipment/arduino"],
  "actions": {
    "measure": "pilot_tdkl.py -c config/tdkl_Alinx.json --action measure && pilot_tdkl.py -c config/tdkl_Hybrids.json --action measure",
    "on":  "./switchOn_tdk.sh",
    "off": "./switchOff_tdk.sh"
  },
  "names": {"192.168.0.249": "ALINX 12V",
            "192.168.0.248": "Hybrids 3.3V",
            "192.168.0.247": "Hybrids 2.2V"},
  "auto_measure_s": 60
}

Commands run with cwd set (the scripts use relative config/ paths) and
path_prepend added to PATH (the pilot tools are not on regular users'
PATH). One action at a time; output is captured for the GUI. No config
file -> the GUI hides the panel.

Measure output lines ("... TDK: <ip> <v> V ; <i> A") are parsed into
structured readings (the GUI shows values, not terminal output) and appended
to logs/tdk_lv_history.csv — the LV Monitor plots draw these curves. With
auto_measure_s > 0 a background thread measures on that period whenever no
manual action is in flight, so the curves stay alive without button presses.
"""

import csv
import json
import os
import re
import subprocess
import threading
from datetime import datetime

ACTION_TIMEOUT_S = 120

# "2026-07-28 18:53:29.7   TDK: 192.168.0.249 11.9975 V ; 01.0998 A"
TDK_LINE_RE = re.compile(r"TDK:\s+(\S+)\s+([\d.]+)\s*V\s*;\s*([\d.]+)\s*A")

HISTORY_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "tdk_lv_history.csv")


class PowerControl:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load()
        self._lock = threading.Lock()
        self.running = None   # action name while one is in flight
        self.last = None      # {"action", "rc", "output", "ts"}
        self.readings = None  # {"ts", "rows": [{"ip","name","v","i"}]} — last measure
        auto_s = (self.config or {}).get("auto_measure_s", 0)
        if self.configured and auto_s and self.config["actions"].get("measure"):
            threading.Thread(target=self._auto_measure_loop, args=(auto_s,),
                             daemon=True, name="power-auto-measure").start()

    def _load(self):
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            if not isinstance(cfg.get("actions"), dict) or not cfg["actions"]:
                return None
            return cfg
        except Exception:
            return None

    @property
    def configured(self):
        return self.config is not None

    def actions(self):
        return list(self.config["actions"].keys()) if self.configured else []

    def _name(self, ip):
        return (self.config.get("names") or {}).get(ip, ip)

    def start(self, action):
        """Launch an action in a worker thread. Returns (ok, message)."""
        if not self.configured:
            return False, "Power control not configured (config/power_config.json)."
        cmd = self.config["actions"].get(action)
        if cmd is None:
            return False, f"Unknown power action: {action}"
        with self._lock:
            if self.running:
                return False, f"Power action '{self.running}' still running."
            self.running = action
        threading.Thread(target=self._run, args=(action, cmd), daemon=True,
                         name=f"power-{action}").start()
        return True, f"Power '{action}' started."

    def _exec(self, cmd):
        env = os.environ.copy()
        prepend = self.config.get("path_prepend") or []
        if prepend:
            env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
        try:
            r = subprocess.run(cmd, shell=True, cwd=self.config.get("cwd"),
                               env=env, capture_output=True, text=True,
                               timeout=ACTION_TIMEOUT_S)
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return -1, f"Timed out after {ACTION_TIMEOUT_S}s."
        except Exception as e:
            return -1, str(e)

    def _run(self, action, cmd):
        rc, output = self._exec(cmd)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.last = {"action": action, "rc": rc,
                         "output": output[-2000:],  # shown by the GUI only on failure
                         "ts": ts}
            self.running = None
        if action == "measure" and rc == 0:
            self._store_readings(output, ts)

    def _parse_readings(self, output):
        return [{"ip": ip, "name": self._name(ip), "v": float(v), "i": float(i)}
                for ip, v, i in TDK_LINE_RE.findall(output)]

    def _store_readings(self, output, ts):
        rows = self._parse_readings(output)
        if not rows:
            return
        with self._lock:
            self.readings = {"ts": ts, "rows": rows}
        try:
            os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
            new_file = not os.path.exists(HISTORY_CSV)
            with open(HISTORY_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["timestamp", "ip", "name", "volt_v", "curr_a"])
                for r in rows:
                    w.writerow([ts, r["ip"], r["name"], r["v"], r["i"]])
        except Exception:
            pass  # history is best-effort; live readings still shown

    def _auto_measure_loop(self, period_s):
        """Background measure every period_s while no manual action runs."""
        import time
        while True:
            time.sleep(period_s)
            with self._lock:
                if self.running:
                    continue
            cmd = self.config["actions"].get("measure")
            rc, output = self._exec(cmd)
            if rc == 0:
                self._store_readings(output,
                                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def status(self):
        with self._lock:
            return {"configured": self.configured,
                    "actions": self.actions(),
                    "running": self.running,
                    "last": self.last,
                    "readings": self.readings}
