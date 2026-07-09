#!/usr/bin/env python3
"""
DAQ monitor: periodically checks session statuses and sends Telegram alerts.

Rules are defined as methods named rule_<name>(self) -> (bool, str).
  bool : True = currently in alert state
  str  : human-readable description of the current state

To add a rule, add a rule_* method to DaqMonitor.
To disable a rule without deleting it, add  "rule_<name>": false  to the
"rules" dict in monitor_config.json.
"""

import os
import json
import threading
from datetime import datetime

import requests

from daq_status import (get_dream_daq_status, get_hv_control_status,
                        get_daq_control_status, get_processor_watcher_status,
                        get_qa_watcher_status)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(token, chat_id, text):
    """Send a message. Returns (success: bool, error: str|None)."""
    try:
        r = requests.post(
            TELEGRAM_URL.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)


def fetch_chat_id(token):
    """Return the chat_id from the most recent message sent to the bot.

    The user must send any message (e.g. /start) to the bot before this works.
    Returns (chat_id: int|None, error: str|None).
    """
    try:
        r = requests.get(
            TELEGRAM_URL.format(token=token, method="getUpdates"),
            timeout=10,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
        if not updates:
            return None, "No messages received yet — send any message to the bot first."
        chat_id = updates[-1]["message"]["chat"]["id"]
        return chat_id, None
    except Exception as e:
        return None, str(e)


def get_bot_username(token):
    """Return the bot's @username. Returns (username: str|None, error: str|None)."""
    try:
        r = requests.get(
            TELEGRAM_URL.format(token=token, method="getMe"),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["result"]["username"], None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class DaqMonitor:
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load_config()

        self._thread = None
        self._stop_event = threading.Event()

        # Per-rule state
        self._alert_active = {}   # rule_name → bool
        self._alert_sent_at = {}  # rule_name → datetime
        self._pending_since = {}  # rule_name → datetime | None (condition first went True)

        self.last_check_time = None
        self.last_alert_time = None

        # Restore enabled state from config
        self.enabled = self.config.get("enabled", False)
        if self.enabled:
            self.start(save=False)

    # ---------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------

    def _load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path) as f:
                return json.load(f)
        return {}

    def save_config(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        self.config["enabled"] = self.enabled
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)

    @property
    def token(self):
        return self.config.get("telegram_token")

    @property
    def chat_id(self):
        return self.config.get("telegram_chat_id")

    def set_chat_id(self, chat_id):
        self.config["telegram_chat_id"] = chat_id
        self.save_config()

    @property
    def check_interval(self):
        return self.config.get("check_interval_seconds", 60)

    @property
    def resend_interval(self):
        return self.config.get("resend_interval_minutes", 30) * 60

    def rule_enabled(self, name):
        return self.config.get("rules", {}).get(name, True)

    def _rule_min_duration(self, name):
        """Seconds the condition must be True before an alert is sent (default 0)."""
        return self.config.get("rule_options", {}).get(name, {}).get("min_duration_seconds", 0)

    def _rule_resend_interval_secs(self, name):
        """Seconds between repeated alerts for this rule (default: global resend_interval)."""
        minutes = self.config.get("rule_options", {}).get(name, {}).get("resend_minutes", None)
        if minutes is not None:
            return minutes * 60
        return self.resend_interval

    # ---------------------------------------------------------------
    # Thread control
    # ---------------------------------------------------------------

    def start(self, save=True):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.enabled = True
        if save:
            self.save_config()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="daq-monitor")
        self._thread.start()
        print("[monitor] Started")

    def stop(self, save=True):
        self.enabled = False
        if save:
            self.save_config()
        self._stop_event.set()
        print("[monitor] Stopped")

    def toggle(self):
        if self.is_running:
            self.stop()
        else:
            self.start()

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------
    # Monitor loop
    # ---------------------------------------------------------------

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_all_rules()
            except Exception as e:
                print(f"[monitor] Unhandled error in check loop: {e}")
            self._stop_event.wait(self.check_interval)

    def _check_all_rules(self):
        self.last_check_time = datetime.now()

        rules = {
            name: getattr(self, name)
            for name in sorted(dir(self))
            if name.startswith("rule_") and callable(getattr(self, name))
        }

        for name, fn in rules.items():
            if not self.rule_enabled(name):
                continue
            try:
                is_alert, detail = fn()
            except Exception as e:
                print(f"[monitor] Rule {name} raised: {e}")
                continue

            was_alert = self._alert_active.get(name, False)
            last_sent = self._alert_sent_at.get(name)
            now = datetime.now()

            if is_alert:
                # Record when the condition first became True
                if self._pending_since.get(name) is None:
                    self._pending_since[name] = now

                elapsed = (now - self._pending_since[name]).total_seconds()
                min_dur = self._rule_min_duration(name)

                if elapsed >= min_dur:
                    resend_secs = self._rule_resend_interval_secs(name)
                    resend_due = last_sent is None or (now - last_sent).total_seconds() > resend_secs
                    if not was_alert or resend_due:
                        self._send_alert(name, detail)
                    self._alert_active[name] = True
                else:
                    pending_remaining = int(min_dur - elapsed)
                    print(f"[monitor] {name} in alert state — waiting {pending_remaining}s more before alerting.")
            else:
                if was_alert:
                    self._send_recovery(name)
                self._alert_active[name] = False
                self._pending_since[name] = None  # reset pending timer

    # ---------------------------------------------------------------
    # Sending
    # ---------------------------------------------------------------

    def _send_alert(self, rule_name, detail):
        if not self.token or not self.chat_id:
            print(f"[monitor] Alert triggered ({rule_name}) but Telegram not configured.")
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"⚠️ <b>DAQ ALERT</b>\n<code>{rule_name}</code>\n{detail}\n<i>{ts}</i>"
        ok, err = send_telegram(self.token, self.chat_id, msg)
        if ok:
            self._alert_sent_at[rule_name] = datetime.now()
            self.last_alert_time = datetime.now()
            print(f"[monitor] Alert sent: {rule_name} — {detail}")
        else:
            print(f"[monitor] Failed to send alert for {rule_name}: {err}")

    def _send_recovery(self, rule_name):
        if not self.token or not self.chat_id:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"✅ <b>DAQ RECOVERED</b>\n<code>{rule_name}</code>\n<i>{ts}</i>"
        ok, err = send_telegram(self.token, self.chat_id, msg)
        if ok:
            print(f"[monitor] Recovery sent: {rule_name}")
        else:
            print(f"[monitor] Failed to send recovery for {rule_name}: {err}")

    def send_test_alert(self):
        if not self.token or not self.chat_id:
            return False, "Telegram token or chat_id not configured."
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"🔔 <b>DAQ monitor test</b>\nMonitoring is active.\n<i>{ts}</i>"
        ok, err = send_telegram(self.token, self.chat_id, msg)
        return ok, err

    # ---------------------------------------------------------------
    # Status summary (for the UI)
    # ---------------------------------------------------------------

    def status_dict(self):
        active = [name for name, v in self._alert_active.items() if v]
        return {
            "running": self.is_running,
            "enabled": self.enabled,
            "chat_id_set": self.chat_id is not None,
            "chat_id": self.chat_id,
            "check_interval": self.check_interval,
            "last_check": self.last_check_time.strftime("%H:%M:%S") if self.last_check_time else None,
            "last_alert": self.last_alert_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_alert_time else None,
            "active_alerts": active,
        }

    # ---------------------------------------------------------------
    # Rules
    # ---------------------------------------------------------------

    def rule_dream_daq_session_dead(self):
        """Alert if the dream_daq tmux session is not running at all."""
        info = get_dream_daq_status()
        fields_str = str(info.get("fields", ""))
        if info["color"] == "danger" and "tmux not running" in fields_str:
            return True, "dream_daq tmux session is not running."
        return False, f"dream_daq: {info['status']}"

    def rule_daq_control_session_dead(self):
        """Alert if the daq_control tmux session is not running at all."""
        info = get_daq_control_status()
        fields_str = str(info.get("fields", ""))
        if info["color"] == "danger" and "tmux not running" in fields_str:
            return True, "daq_control tmux session is not running."
        return False, f"daq_control: {info['status']}"

    def rule_hv_control_monitoring(self):
        """Alert if hv_control is not actively monitoring HV (dead, off, or unknown)."""
        info = get_hv_control_status()
        ok_statuses = {"Monitoring HV", "HV Ramped", "Ramping HV"}
        if info["status"] not in ok_statuses:
            return True, f"hv_control is not monitoring HV — status: {info['status']}"
        return False, f"hv_control: {info['status']}"

    def rule_dream_daq_unknown_state(self):
        """Alert if dream_daq is running but in an unrecognised state."""
        info = get_dream_daq_status()
        if info["status"] == "UNKNOWN STATE":
            return True, "dream_daq is in UNKNOWN STATE — check the terminal."
        return False, f"dream_daq: {info['status']}"

    def rule_daq_control_unknown_state(self):
        """Alert if daq_control is running but in an unrecognised state."""
        info = get_daq_control_status()
        if info["status"] == "UNKNOWN STATE":
            return True, "daq_control is in UNKNOWN STATE — check the terminal."
        return False, f"daq_control: {info['status']}"
