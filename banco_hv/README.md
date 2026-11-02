# Lab HV monitor on banco (USB supplies, read-only)

Since Nov 2026 the lab CAEN crate is out of service. The P2 chambers are powered by two USB-only modules plugged into banco (`ssh banco_daplxa`, dedippcq196, 132.166.10.196):

| port (by-id) | module | protocol |
|---|---|---|
| `usb-FTDI_USB__-__Serial-if00-port0` (ttyUSB0) | CAEN N1471, SN 308, board 00 | CAEN ASCII, 9600 8N1 XON/XOFF, `$BD:00,CMD:MON,CH:4,PAR:VMON` |
| `usb-FTDI_FT232R_USB_UART_AL04II1G-if00-port0` (ttyUSB1) | iseg NHR 42 60r, SN 8200139, fw 1.57 | iseg SCPI, 9600 8N1, echoes each command line |

Channel map (user, 2026-11-02; N1471 hardware max-V 900/600/950/650 agrees with drift/mesh/drift/mesh):

| channel | electrode |
|---|---|
| N1471 ch0 | P2_IN drift |
| N1471 ch1 | P2_IN mesh |
| N1471 ch2 | P2_OUT drift |
| N1471 ch3 | P2_OUT mesh |
| NHR ch0 | P2_MID mesh |
| NHR ch1 | P2_MID drift |
| NHR ch2, ch3 | not used (monitored, not sent to run logs) |

## What it does

`hv_serial_live.py` polls both modules every 5 s, monitor commands only (non-monitor commands are refused in code), and:

- `GET /hv_live?token=...` serves the same JSON contract as `docs/dream_side/hv_live.py`, keyed by label, so `hv_dream_shim.py` writes every sub-run's `hv_monitor.csv` with no VMM-side code change.
- `GET /hv_all?token=...` serves all 8 channels with raw status words.
- `GET /` is the **HV Control page** (also the VMM GUI's "HV Control" tab, via the flask `/hvctl/` proxy); `GET /table` is the plain read-only table. Requests from banco itself (localhost) need no token.
- `GET /hv_history?minutes=N&token=...` serves the last N minutes (up to 24 h, in memory, seeded from today's CSV at start) as `{time, voltage, current, power}`. The VMM flask proxies it as `/hv_data_live`, and the GUI's HV Monitor panel plots it when "Show" is set to "Live (banco)" (non-beam sites only).
- Writes `~/hv_serial_logs/hv_serial_YYYY-MM-DD.csv` every 10 s, whether or not a run is polling.

Units: V and µA, magnitudes (both modules are negative polarity).

## Control

The page is read-only until unlocked with `control_password` (banco `~/lab_hv_monitor/hv_serial_live.json`; empty = control disabled). Unlocked, each detector channel has ON / OFF / Edit (Vset, current limit, ramp up/down, CAEN trip time / power-down RAMP|KILL / Imon range, iseg trip time [ms] / trip action 0-4) and there is an ALL OFF button. Every change asks for confirmation showing old → new.

Enforced by the service before anything is sent (config `limits`):

| check | limit |
|---|---|
| Vset on Drift electrodes | ≤ 1000 V |
| Vset on Mesh electrodes | ≤ 500 V |
| switching ON | setpoint must already be within that limit |
| current limit | 0 < I ≤ 100 µA |
| ramp up / down | 1-50 V/s |
| unlabelled channels (NHR ch2, ch3) | locked |

Every attempt, accepted or refused (including wrong passwords), goes to `~/hv_serial_logs/hv_actions.jsonl` with time, source (banco page or VMM GUI + client), channel, old and new value; the page lists the latest ones.

**N1471:** set commands need its front-panel switch on **REMOTE**; on LOCAL it answers `LOC:ERR` and the page says so. Monitoring works in either mode. The iseg NHR takes commands at any time.

Command syntax (checked against the `hvps` library, github.com/lobis/hvps): CAEN `$BD:00,CMD:SET,CH:n,PAR:VSET,VAL:700.0` / `PAR:ON` / `PAR:OFF` → `#BD:00,CMD:OK`; iseg `:VOLT 430.000,(@0);*OPC?` / `:VOLT ON,(@0);*OPC?` → echo, then `1`. Voltages are sent as magnitudes; the channel's polarity gives the sign.

## Running it on banco (starts at boot)

Files live in `~/lab_hv_monitor/`: `hv_serial_live.py`, `hv_control_page.html`, `run_hv_service.sh` and `hv_serial_live.json` (token, control password, limits; not in the repository).

banco's crontab starts it at boot and re-checks every 10 minutes (the previous crontab is saved as `~/lab_hv_monitor/crontab.bak-20261102`):

```
@reboot sleep 30; /local/home/banco/lab_hv_monitor/run_hv_service.sh --tmux
*/10 * * * * /local/home/banco/lab_hv_monitor/run_hv_service.sh --tmux
```

`run_hv_service.sh --tmux` starts tmux server `-L lab_hv`, session `lab_hv`, only if it is not already running. Inside it, the script restarts the service 10 s after any exit and holds a lock, so only one copy can ever run. At boot there is no user session, so the memory cap falls back from `systemd-run MemoryMax=300M` to `ulimit -v` (1 GB).

```
tmux -L lab_hv attach -t lab_hv     # look at it (detach: Ctrl-b d)
# new version: copy the files, then Ctrl-C ONCE in that pane -> restarts within 10 s
# stop for good:   touch ~/lab_hv_monitor/DISABLED; tmux -L lab_hv kill-server
# allow again:     rm ~/lab_hv_monitor/DISABLED   (cron restarts it within 10 min)
```

It runs on its own tmux server on purpose. The default tmux server on banco dates from 22 Oct, before `banco` joined `dialout`, so anything started inside it is refused access to the serial ports. The same applies to any login session older than the group change.

## VMM side

In the deployed `config/dream_bridge.json` on the VMM box:

```
"hv_live_url": "http://132.166.10.196:5010/hv_live",
"hv_live_token": "<token from banco ~/lab_hv_monitor/hv_serial_live.json>",
"hv_control": {"cards": {"0": "N1471", "1": "NHR"}, "password": "<control_password>",
               "tolerance_v": 3.0, "settle_s": 30, "timeout_s": 900, "poll_s": 5}
```

The shim re-reads that file at every run start.

- **hv_live_url** gives each sub-run its `hv_monitor.csv`, the live HV plots, and the HV Control tab (flask `/hvctl/`).
- **hv_control** makes runs SET the HV. The lab setup file (`/local/p2/p2-setups/lab_saclay_2026.yaml`, `hv:`) maps each electrode to `[card, channel]`, with card 0 = N1471 and card 1 = NHR. It also holds the ceilings (mesh 500, drift 1000) and the operating point.

## Runs set the HV (Setup tab -> Run)

Lab plans run at the **Run HV** values (`Run HV: <detector> <mesh|drift>`): `lab_source`, `config_scan`, `trigger_test` and `operating_long` run at them, and `hv_scan` steps from them. The defaults are the setup's operating point. **0 V means that channel is OFF.** On 2026-11-02 they were set to 0 because the chambers had no gas.

For every sub-run, `hv_dream_shim.py`:

1. refuses the sub-run, before sending anything, if any target is above banco's limit or on a locked channel;
2. sets each Vset, then ON (or OFF for 0 V), through banco's validated API;
3. waits until every channel reads back ON and within `tolerance_v` of its target, and holds it for `settle_s`;
4. only then answers `HV Set`, and the capture starts.

A refusal, a trip, Stop Run or `timeout_s` skips that sub-run (shim pane: `HV NOT SET — ...`). `hv_monitor.csv` covers the ramp as well. **At the end of a run the HV is left where the last sub-run put it**; set Run HV to 0, or press ALL OFF, to switch it off.
