#!/usr/bin/env python3
"""
VMM chip configuration from the GUI: pick one of the prepared exception .txt
files (config_ext/) and apply it on top of the base YAML (config_base/) by
running the p2basket slow-control utility.

The applier is the TestBenchCosmics p2basket_sc_config.py (p2basket-python
shebang) — called as a subprocess, never imported and never modified here.
Its CLI: -c <conf_dir> -b <base_conf> -e <ext_conf>, names joined to conf_dir.

Machine-specific — configured in config/chip_config.json (gitignored):

{
  "conf_dir": "/local/p2/p2testbench/TestBenchCERN",
  "base_subdir": "config_base",
  "ext_subdir": "config_ext",
  "base_yaml": null,
  "apply_script": "/local/p2/p2testbench/TestBenchCosmics/p2basket_sc_config.py",
  "timeout_s": 300
}

base_yaml null -> auto-detect, requiring exactly one *.yaml in base_subdir (so
a renamed base file follows along without a config edit). The selected ext
file persists in config/chip_config_state.json, where daq_control.py picks it
up to copy into each run directory for provenance. No config file -> the GUI
hides the panel. Apply refuses while a capture (dumpcap/tcpdump) is running —
never reconfigure the chip mid-acquisition.
"""

import glob
import json
import os
import subprocess
import threading
from datetime import datetime

DEFAULT_TIMEOUT_S = 300


class ChipConfig:
    def __init__(self, config_path, state_path):
        self.config_path = config_path
        self.state_path = state_path
        self.config = self._load()
        self._lock = threading.Lock()
        self.running = False
        self.last = None   # {"file", "base_yaml", "rc", "output", "ts"}
        state = self._read_state()
        self.last = state.get("last_applied")

    def _load(self):
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            if not os.path.isdir(cfg.get("conf_dir", "")):
                return None
            return cfg
        except Exception:
            return None

    @property
    def configured(self):
        return self.config is not None

    def _dir(self, key, default):
        return os.path.join(self.config["conf_dir"], self.config.get(key, default))

    def base_yaml(self):
        """Pinned base yaml name, or the single *.yaml in base_subdir."""
        if not self.configured:
            return None
        pinned = self.config.get("base_yaml")
        if pinned:
            return pinned
        yamls = sorted(glob.glob(os.path.join(self._dir("base_subdir", "config_base"), "*.yaml")))
        return os.path.basename(yamls[0]) if len(yamls) == 1 else None

    def ext_files(self):
        if not self.configured:
            return []
        return sorted(os.path.basename(p) for p in
                      glob.glob(os.path.join(self._dir("ext_subdir", "config_ext"), "*.txt")))

    def _read_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_state(self, **updates):
        state = self._read_state()
        state.update(updates)
        with open(self.state_path, "w") as f:
            json.dump(state, f, indent=2)

    def selected(self):
        return self._read_state().get("selected_ext")

    def select(self, fname):
        if not self.configured:
            return False, "Chip config not configured (config/chip_config.json)."
        if fname not in self.ext_files():
            return False, f"Unknown ext config file: {fname}"
        self._write_state(selected_ext=fname)
        return True, f"Selected {fname}"

    @staticmethod
    def capture_active():
        """True while dumpcap/tcpdump captures — never reconfigure mid-run."""
        return subprocess.run(["pgrep", "-f", "dumpcap -i|tcpdump -i"],
                              capture_output=True).returncode == 0

    def apply(self):
        """Run the p2basket config utility with base yaml + selected ext file."""
        if not self.configured:
            return False, "Chip config not configured (config/chip_config.json)."
        base = self.base_yaml()
        if not base:
            return False, "Base yaml not found (need exactly one *.yaml in config_base, or pin base_yaml)."
        sel = self.selected()
        if not sel or sel not in self.ext_files():
            return False, "No valid ext config file selected."
        if self.capture_active():
            return False, "A capture is running — refusing to reconfigure the chip during acquisition."
        with self._lock:
            if self.running:
                return False, "A chip configuration is already in progress."
            self.running = True
        threading.Thread(target=self._run, args=(base, sel), daemon=True,
                         name="chip-config-apply").start()
        return True, f"Configuring chip: {base} + {sel}"

    def _run(self, base, sel):
        cfg = self.config
        cmd = [cfg["apply_script"],
               "-c", cfg["conf_dir"],
               "-b", os.path.join(cfg.get("base_subdir", "config_base"), base),
               "-e", os.path.join(cfg.get("ext_subdir", "config_ext"), sel)]
        timeout = cfg.get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            r = subprocess.run(cmd, cwd=cfg["conf_dir"], capture_output=True,
                               text=True, timeout=timeout)
            rc, output = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, output = -1, f"Timed out after {timeout}s."
        except Exception as e:
            rc, output = -1, str(e)
        with self._lock:
            self.last = {"file": sel, "base_yaml": base, "rc": rc,
                         "output": output[-2000:],
                         "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            self.running = False
        self._write_state(last_applied=self.last)

    def status(self):
        with self._lock:
            return {"configured": self.configured,
                    "files": self.ext_files(),
                    "selected": self.selected(),
                    "base_yaml": self.base_yaml(),
                    "running": self.running,
                    "capture_active": self.capture_active(),
                    "last": self.last}
