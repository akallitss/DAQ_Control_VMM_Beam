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
- `GET /?token=...` serves an auto-refreshing table for a browser.
- Writes `~/hv_serial_logs/hv_serial_YYYY-MM-DD.csv` every 10 s, whether or not a run is polling.

Units: V and µA, magnitudes (both modules are negative polarity).

## Deploy / restart (on banco)

```
# files live in ~/lab_hv_monitor/ (hv_serial_live.py + hv_serial_live.json with the token)
tmux -L lab_hv new-session -d -s lab_hv "cd ~/lab_hv_monitor && systemd-run --user --scope -q -p MemoryMax=300M python3 hv_serial_live.py --config hv_serial_live.json 2>&1 | tee -a ~/lab_hv_monitor/service.log"
tmux -L lab_hv attach -t lab_hv        # look at it
python3 hv_serial_live.py --config hv_serial_live.json --once   # one reading, no server
```

It runs on a **separate tmux server (`-L lab_hv`)** on purpose. The default tmux server on banco dates from 22 Oct, before `banco` joined `dialout`, so anything started inside it is refused access to the serial ports. The same applies to any login session older than the group change.

## VMM side

In the deployed `config/dream_bridge.json` on the VMM box:

```
"hv_live_url": "http://132.166.10.196:5010/hv_live",
"hv_live_token": "<token from banco ~/lab_hv_monitor/hv_serial_live.json>"
```

The shim re-reads that file at every run start, so no restart is needed.
