# DAQ_Control_VMM_Beam

Control, monitoring and online QA for the P2 detector **VMM readout** at the SPS
beam test. Runs alongside `DAQ_Control_Dream_Beam` (same architecture, disjoint
ports and tmux session names) so both DAQ stacks can co-host on one machine.

Clone of Dylan Neff's nTof/Dream DAQ control architecture, adapted for VMM:
raw UDP from the front-ends is captured into rotating `.pcapng` files per
network interface (dumpcap); there is **no ROOT conversion and no
processor/pedestal chain** — online QA runs directly on each finalized pcapng.
Adds **LV monitoring** of the Aim-TTi bench PSUs next to the CAEN HV monitoring.

## Architecture

tmux services (all prefixed `vmm_` — start with `./start_servers.sh`):

| session           | program                | port | role |
|-------------------|------------------------|------|------|
| `vmm_hv_control`  | `hv_control.py`        | 2100 | CAEN HV set/ramp/monitor → `hv_monitor.csv` per subrun |
| `vmm_lv_control`  | `lv_control.py`        | 2102 | Aim-TTi PSUs (SCPI TCP 9221) → `lv_monitor.csv` per subrun |
| `vmm_daq`         | `vmm_daq_control.py`   | 2101 | dumpcap/tcpdump capture per interface, ALINX slow control, status lines |
| `vmm_daq_control` | interactive shell      |  —   | `daq_control.py <run_config.json>` orchestrates sub-runs |
| `vmm_flask`       | `flask_app/`           | 5002 | GUI: run control, status cards, SPS beam monitor (CERN Vistar), HV/LV plots, online QA gallery |
| `vmm_qa_watcher`  | `qa_watcher.py`        |  —   | runs `vmm_qa/vmm_pcapng_qa.py` on each finalized pcapng |
| `vmm_backup_watcher` | `backup_watcher.py` |  —   | rsync to EOS (configure `backup_config.py` first) |

Data tree (`base_data_dir` from `run_config_beam.py`):

```
runs/<run>/<subrun>/raw_daq_data/<iface>_<seq>_<ts>.pcapng   capture files
runs/<run>/<subrun>/{hv,lv}_monitor.csv                      slow-control logs
analysis/<run>/<subrun>/<pcap_base>/*.png + events.json      online QA output
```

A pcapng is analyzed once it is *finalized*: a higher-sequence file for the
same interface exists, the sub-run ended (`.capture_done` marker), or its mtime
is older than 2× the rotation interval. `events.json` per file carries
`n_hits`/`hits_per_vmm`; `get_run_events.py` sums them for the GUI counter.

The GUI's **Beam Monitor** strip shows the SPS beam state (below). The live
Vistar page images themselves (SPS Page 1 / Fast BCT / BSRT / BT — whitelist
`BEAM_VISTARS` in `flask_app/beam_state.py`) are hidden behind the strip's
*Show page* toggle and only polled while visible. Flask proxies the public
`vistar-capture.s3.cern.ch/<page>.png` with a 5 s cache so any number of open
GUIs cause one upstream fetch; only the DAQ machine needs outbound HTTPS.
If CERN is unreachable the last good frame is kept and marked *stale*.

**Beam ON/OFF tracking** (`flask_app/beam_state.py`): Vistar publishes SPS
Page 1 only as an image, so the target intensity table (T2/T4/T6/T10, I/E11)
is read out of the PNG by exact bitmap-glyph matching (`beam_glyphs.json`;
the Vistar font is un-antialiased, so a glyph either matches exactly or the
parse fails loudly — no misreads). Beam is ON when the tracked target's
intensity ≥ threshold (default: T2, 1.0 E11 — panel header / `POST
/beam_state/set_target`, `set_threshold`; persisted in
`config/beam_config.json`), debounced over 2 samples at the CERN-requested
7 s poll. The panel chip shows ON / OFF + how long it's been off / UNKNOWN
(page unreachable or unparsable > 90 s). Every transition is appended to
`logs/beam_history.csv` (timestamp, event, target, intensity, off-duration);
`GET /beam_history` serves it. State persists across restarts
(`config/beam_state.json`), so an off-period spanning a Flask restart keeps
its start time. Monitor rules `rule_beam_off` / `rule_beam_state_unknown`
alert when the beam is down (use `rule_options.rule_beam_off.
min_duration_seconds` in `monitor_config.json` to ignore short gaps).
Alerts go to **Telegram** (bot token + chat ID) and/or **WhatsApp** (free
CallMeBot gateway — a third-party relay, fine for status pings) — both are
configured from the GUI's Monitoring → Setup panel.

## Remote access and operations

The DAQ computer sits in the beam area — all interaction is remote. Three
rules cover it: reach the GUI through an SSH tunnel, edit code anywhere
*except* the DAQ machine, and let one account own the running stack.

### GUI from another machine (SSH tunnel)

The GUI has **no authentication** — anyone who reaches port 5002 controls
the DAQ. Never ask for the port to be opened; the SSH tunnel *is* the
access control. From any machine whose SSH config can reach the DAQ host
(directly or via a jump host, e.g. the `vmm_daplxa` alias at Saclay):

```bash
ssh -f -N -L 15002:localhost:5002 <daq-host-alias>
# then browse http://localhost:15002
```

If the page stops loading (laptop suspend, network change, DAQ reboot),
the tunnel is dead — kill any leftover and start a new one:

```bash
pkill -f 'ssh.*-L 15002' ; ssh -f -N -L 15002:localhost:5002 <daq-host-alias>
```

For a tunnel that survives drops on its own, use autossh
(`autossh -M 0 -f -N -L 15002:localhost:5002 <daq-host-alias>`) or add
`ServerAliveInterval 30` / `ExitOnForwardFailure yes` to the host's SSH
config block. Several people can hold their own tunnels to the same GUI
at once — the port number on the laptop side is free choice.

### Code changes: edit locally, push, pull on the DAQ machine

Treat the DAQ checkout as a **deploy target, never a workspace**:

- develop and commit on your own machine → `git push`;
- deploy on the DAQ machine with the GUI's **Git Reset** button (it runs
  `git reset --hard origin && git pull`) or `ssh <daq> 'cd DAQ_Control_VMM_Beam && git pull'`;
- then **Restart All** in the GUI so the running services pick up the code.

Because Git Reset is a hard reset, any uncommitted edit made directly on
the DAQ machine is silently wiped at the next deploy — that is by design
(the button must always produce a known state during a shift). If a
mid-shift hotfix on the machine is unavoidable, commit and push it from
there immediately afterwards. Note DAQ networks often block outbound
HTTPS, so the git remote must use SSH (`git@github.com:...`); pushing
from the DAQ machine requires a key — either your forwarded agent
(`ForwardAgent yes`, as at Saclay) or a per-machine deploy key with write
access. Runtime state (`config/*_state.json`, run configs, credentials,
`logs/`) is gitignored, so deploys never touch it.

### Several operators, one DAQ

Shifters do not need shell accounts to *operate* the DAQ — the GUI is the
control surface, and a browser plus tunnel is enough. The clean model:

- **One account owns the stack** (ideally a service account, e.g. `p2daq`,
  created by the machine admin; otherwise the account that cloned the
  repo). It owns the checkout, the venv, the data directory and all
  `vmm_*` tmux sessions, and is the only one that can `tmux attach` to
  them — tmux sockets are per-user, so a stack started by one user is
  invisible to others by design. Don't run pieces as different users.
- **Shifters** use their personal SSH accounts on the machine *only for
  the tunnel* (`ssh -N -L ...` needs nothing but a login), then drive
  everything from the browser. The event log records the client IP of
  every Start/Stop, so actions stay attributable even with a shared
  service account.
- **Experts** who need the terminals get access to the service account
  itself: the admin adds their public keys to the service account's
  `~/.ssh/authorized_keys` (the usual test-beam pattern), or grants
  `sudo -u p2daq -i`. Then `tmux attach -t vmm_daq` etc. work.
- Keep `hv_creds.txt` and `config/monitor_config.json` readable by the
  service account only (`chmod 600`) — they hold the CAEN password and
  the Telegram/WhatsApp keys. If personal accounts must also read the
  data tree, put a group on the data directory
  (`chgrp -R <group> <base_data_dir> && chmod -R g+rX ...`), not on the
  credentials.

## Site switch

Everything machine-specific lives in the `SITES` dict at the top of
`run_config_beam.py` (**keep the filename** — several modules import it):

- `SITE = 'local'` — full simulation: fake CAEN HV, fake TTi LV and a fake VMM
  DAQ that replays a sample pcapng from `sim_pcapng/` into the run directory.
  Test the whole chain with no hardware. (Verified end-to-end 2026-07-09.)
- `SITE = 'sps'` — real hardware. Deployment checklist below.

## Deploying on the DAQ computer (SPS)

> **`TODO_SPS.txt`** in the repo root is the working checklist for this
> section: one entry per `TODO-SPS` field with concrete commands to *find*
> each value (scan for the CAEN/TTi IPs, identify the capture NIC, etc.)
> and a final verification list. Start there when setting up the machine.

### 1. Install

```bash
git clone <this repo> && cd DAQ_Control_VMM_Beam
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Machine prerequisites:

- **`dumpcap`** (wireshark-common) with capture rights for the DAQ user —
  either add the user to the `wireshark` group or
  `sudo setcap cap_net_raw,cap_net_admin+eip $(which dumpcap)`.
  Fallback: set `capture_tool: 'tcpdump'` in `vmm_daq_info` (needs the same
  capability treatment).
- **`alinx-sc`** on `PATH` (the shell that runs `start_servers.sh`) — it is
  called with `--read-link-status` / `--acq-on` / `--acq-off` around each
  sub-run for every interface with `slow_control: 'alinx'`.
- **tmux**, and enough disk on the data partition — raw pcapng is the full
  UDP payload, so size it for the expected hit rate × run plan (the GUI's
  System Resources panel tracks the data disk).

### 2. Connect the IPs — `SITES['sps']` in `run_config_beam.py`

Every field to touch is marked `TODO-SPS`; `grep -rn TODO-SPS` must come back
empty (except comments) before the first real run.

| field | what to put there |
|-------|-------------------|
| `base_data_dir` | data disk on the DAQ machine, e.g. `/local/p2/p2data/sps_vmm_2026/` (trailing `/`). The `runs/ analysis/ config/detectors/` tree is created under it. |
| `daq_host` | IP the hv/lv/daq servers are reached on **by daq_control and flask**. Everything runs on the same machine → keep `127.0.0.1`. Only set the LAN IP if you ever split services across machines. |
| `hv_ip` | CAEN mainframe IP on the crate network. |
| `hv_n_cards` | number of HV cards in the SPS crate. |
| `lv_units` | `{'tti1': <ip>, 'tti2': <ip>}` — Aim-TTi PSU LAN IPs. They speak SCPI on raw TCP **9221** (fixed on the units; per-unit channels configurable in `lv_info['units']`). |
| `interfaces` | one entry per NIC that receives VMM UDP. `iface` = the Linux interface name (check `ip a`, e.g. `enp4s0f1` for the ALINX link). `alinx_config` = absolute path to `config_alinx_noThresholds.json` on this machine. To also capture the SRS chain, uncomment the `srs` entry and set its `iface`; `slow_control: 'none'` skips alinx-sc for it. |

Also in `run_config_beam.py`:

- `P2_HV` — (card, channel) for mesh and drift **once the SPS crate is cabled**;
  the detector block's `hv_channels` and the GUI HV labels follow from it.
- `CAPTURE_DURATION_S` — pcapng rotation interval (44 s default). QA latency
  and the finalize timeout (2×) both scale with it.
- `detectors[0]['det_center_coords']` / `vmm_map` — survey coordinates and
  iface→VMM cabling map (informational, labels the QA).

Then switch `SITE = 'sps'` and sanity-check the printed summary:

```bash
.venv/bin/python run_config_beam.py   # prints site, dirs, HV points, capture, LV units, schedule
```

### 3. Credentials & backup

- `hv_creds.txt` in the repo root: CAEN username on line 1, password on
  line 2 (gitignored). Without it the code falls back to admin/admin and
  warns.
- Backup to EOS (optional, "Start Backup" button): fill the `TODO-SPS`
  constants in `backup_config.py` (`EOS_DIR`, `CERN_PRINCIPAL`,
  `GPG_PASS_FILE` — create with `gpg --encrypt`), with EOS FUSE-mounted and
  kerberos available.

### 4. First-connection tests (before the first run)

```bash
ping <hv_ip>; ping <tti1>; ping <tti2>
nc -zv <tti1> 9221                       # TTi SCPI port reachable
alinx-sc --config-file <cfg> --read-link-status
./start_servers.sh                       # then check each pane:
tmux attach -t vmm_hv_control            #   "Listening on 0.0.0.0:2100", no CAEN errors
tmux attach -t vmm_lv_control            #   "[lv] tti1 connected: <IDN>" for each unit
tmux attach -t vmm_daq                   #   "Listening on 0.0.0.0:2101"
```

A short dry run without beam (1 sub-run, 1–2 min) confirms HV ramp, LV CSV,
capture rotation and the QA gallery end to end.

## Running

```bash
./start_servers.sh          # starts vmm_hv_control / vmm_lv_control / vmm_daq /
                            # vmm_daq_control / vmm_flask tmux sessions
```

Open **http://\<daq-machine-ip\>:5002** (port 5002 must be reachable from the
control-room machine; the 2100–2102 service ports stay on localhost).

1. Set the run plan in `run_config_beam.py`: `N_SUBRUNS`, `SUBRUN_MIN`,
   `MESH_V`, `DRIFT_V`, `POST_SUBRUN_PAUSE_MIN` — or comment in the HV-scan
   template to build the sub-run list from a voltage loop.
2. **Start QA** (and **Start Backup** if configured) from the Watchers row.
3. **Start Run** — iterates the run number, regenerates the JSON config and
   launches `daq_control.py`. Per sub-run it ramps HV, starts LV monitoring,
   arms ALINX (`acq-on`), captures for `run_time` minutes, then `acq-off`.
4. During the run: status cards refresh at 1 Hz (capture files/MB, HV/LV
   state, QA progress), HV + LV plots at 5 s, the Online QA tab fills per
   capture file, "Hits this run" sums the analyzed files.
5. **Stop Sub-Run** ends only the current sub-run; **Stop Run** ends the whole
   run through the normal shutdown (HV is powered off only if
   `power_off_hv_at_end = True` in the config); **Pause After Subrun** holds
   at the next boundary until Resume. A stopped sub-run is left unmarked, so
   restarting with `resume = True` re-runs it.

Manual equivalents: `bash_scripts/start_run.sh <config.json>`,
`stop_run.sh`, `stop_sub_run.sh`, `restart_daq_tmux_processes.sh`.

QA on a single file by hand:

```bash
.venv/bin/python vmm_qa/vmm_pcapng_qa.py <file.pcapng> --out-dir out/ --events-json
# --format TRG for external-trigger data, --calibration <vmm-sdat json>, --live to follow a growing file
```
