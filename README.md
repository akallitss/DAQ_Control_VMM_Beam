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
  Test the whole chain with no hardware.
- `SITE = 'sps'` — real hardware. Fill every field marked `TODO-SPS`
  (`grep -rn TODO-SPS` lists them: HV crate IP/cards, data disk, TTi IPs,
  ALINX config path, EOS backup destination).

## Running

```bash
./start_servers.sh                      # start all tmux services
# open http://<daq-host>:5002  →  Start Run  (iterates run number, starts daq_control)
# GUI buttons: Stop Sub-Run / Stop Run / Pause After Subrun / Start QA / Start Backup
```

Sub-run schedule (HV setpoints, durations, pauses) is generated in
`run_config_beam.py` from the constants at the top (`N_SUBRUNS`, `SUBRUN_MIN`,
`MESH_V`, `DRIFT_V`); an HV-scan template is included, commented out.

Manual equivalents: `bash_scripts/start_run.sh <config.json>`,
`stop_run.sh`, `stop_sub_run.sh`, `restart_daq_tmux_processes.sh`.

QA on a single file by hand:

```bash
.venv/bin/python vmm_qa/vmm_pcapng_qa.py <file.pcapng> --out-dir out/ --events-json
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python run_config_beam.py     # writes config/json_run_configs/run_config_beam.json
```

At SPS additionally: `hv_creds.txt` (CAEN user/pass, two lines, repo root),
dumpcap capture privileges for the DAQ user, and the `TODO-SPS` fields above.
