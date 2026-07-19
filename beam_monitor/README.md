# Beam monitor — live n_TOF beam intensity from NXCALS/Timber

Logs the proton intensity delivered to the n_TOF target and tells the DAQ GUI
whether the beam is on. The data source is **NXCALS** (the database behind
Timber), queried directly from this machine — the same numbers you'd export
from timber.cern.ch by hand.

## What runs where

- `../beam_watcher.py` — standalone process in the `beam_watcher` tmux session
  (GUI button "Start Beam Watcher"). Sole owner of the NXCALS/Spark session.
- `beam_intensity_controller.py` — the actual monitor class + shared paths.
  Import-safe from the Flask venv (pytimber is only imported inside the
  watcher process).
- Per-day CSVs: `beam_monitor/logs/beam_intensity_YYYY-MM-DD.csv`
  (`timestamp, unix_ts, intensity_e10`) — every TOF cycle NXCALS logs,
  zeros included, so this is the same record Timber would give you.
- Published state: `../config/beam_intensity_state.json`, served by `/beam/status`
  and the Shift Overview "n_TOF Beam" card. History: `/beam/history?hours=6`.

## The variable

`FTN.BCT477:AcquisitionLatest:totalIntensity` — the **last** beam-current
transformer in the FTN line before the n_TOF target, i.e. protons actually
on target. Units: **1e10 protons per pulse** (dedicated pulse ≈ 850 = 8.5e12
p, parasitic ≈ 400–700). One point per TOF cycle (~2 s granularity), NXCALS
latency ~0.5–1 min. Points below `PULSE_THRESHOLD_E10` (50) are empty
cycles, not pulses.

**Do NOT use `F16.BCT372.TOF:INTENSITY` (or `CPS.NTOF:INTENSITY`) for beam
on/off** — they sit upstream (TT2 / PS extraction) and count TOF-destination
pulses that can be stopped before the target. Observed 2026-07-10
17:20–18:20: the target received nothing for an hour (BCT477 ≈ 0) while
BCT372.TOF kept logging ~6.9e12 pulses. The watcher used BCT372 until ~18:45
that day; that period's CSV is archived as `logs/bct372_beam_intensity_*.csv`.

Beam ON = a real pulse within `BEAM_OFF_GAP_S` (180 s — must stay above
NXCALS latency + normal supercycle gaps).

## The NXCALS venv (~/venvs/nxcals)

pytimber ≥4 drags in PySpark + a JVM bridge (~1 GB), so it lives in its own
venv, NOT the DAQ venv. Rebuild recipe (system python3.12 has no ensurepip,
hence get-pip):

```bash
python3 -m venv --without-pip ~/venvs/nxcals
curl -sS https://bootstrap.pypa.io/get-pip.py | ~/venvs/nxcals/bin/python
~/venvs/nxcals/bin/pip install setuptools pytimber \
    --index-url https://acc-py-repo.cern.ch/repository/vr-py-releases/simple \
    --extra-index-url https://pypi.org/simple \
    --trusted-host acc-py-repo.cern.ch
```

Gotchas learned the hard way (2026-07-10):

- `--trusted-host acc-py-repo.cern.ch` is required — the repo's CERN CA cert
  is not in this machine's trust store. Without it pip silently falls back to
  PyPI, whose `pytimber` is a stub that refuses to build.
- Do **NOT** install `pyarrow` in this venv. With pyarrow present, PySpark
  uses its Arrow path, which crashes on this box's Java 21
  (`sun.misc.Unsafe ... not available`). Without pyarrow it falls back to
  plain conversion, which is fine for our tiny queries.
- `setuptools` is needed (Python 3.12 removed distutils; PySpark still wants it).
- First query after startup ≈ 30–60 s (local Spark spin-up); after that each
  poll is ~1–2 s.

## Authentication (Kerberos)

Queries authenticate with the user's Kerberos ticket in the default cache —
the same `kinit akallits@CERN.CH` the EOS backup uses. The watcher runs
`kinit -R` periodically, which keeps the ticket alive up to its renewable
life (~5 days); after that a manual `kinit akallits@CERN.CH` reseed is needed.
The state file exposes `krb_valid_until`; the shift card warns when < 12 h
remain, and the Telegram monitor's `rule_beam_watcher_dead` fires once
queries actually start failing.
