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
| `vmm_flask`       | `flask_app/`           | 5002 | GUI: run control, status cards, HV/LV plots, online QA gallery |
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

## Site switch

Everything machine-specific lives in the `SITES` dict at the top of
`run_config_beam.py` (**keep the filename** — several modules import it):

- `SITE = 'local'` — full simulation: fake CAEN HV, fake TTi LV and a fake VMM
  DAQ that replays a sample pcapng from `sim_pcapng/` into the run directory.
  Test the whole chain with no hardware. (Verified end-to-end 2026-07-09.)
- `SITE = 'sps'` — real hardware. Deployment checklist below.

## Deploying on the DAQ computer (SPS)

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
