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
  }
}

Commands run with cwd set (the scripts use relative config/ paths) and
path_prepend added to PATH (the pilot tools are not on regular users'
PATH). One action at a time; output is captured for the GUI. No config
file -> the GUI hides the panel.
"""

import json
import os
import subprocess
import threading
from datetime import datetime

ACTION_TIMEOUT_S = 120


class PowerControl:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load()
        self._lock = threading.Lock()
        self.running = None   # action name while one is in flight
        self.last = None      # {"action", "rc", "output", "ts"}

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

    def _run(self, action, cmd):
        env = os.environ.copy()
        prepend = self.config.get("path_prepend") or []
        if prepend:
            env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
        try:
            r = subprocess.run(cmd, shell=True, cwd=self.config.get("cwd"),
                               env=env, capture_output=True, text=True,
                               timeout=ACTION_TIMEOUT_S)
            rc, output = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, output = -1, f"Timed out after {ACTION_TIMEOUT_S}s."
        except Exception as e:
            rc, output = -1, str(e)
        with self._lock:
            self.last = {"action": action, "rc": rc,
                         "output": output[-2000:],  # keep the tail, it has the measures
                         "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self.running = None

    def status(self):
        with self._lock:
            return {"configured": self.configured,
                    "actions": self.actions(),
                    "running": self.running,
                    "last": self.last}
