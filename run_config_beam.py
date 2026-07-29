#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run configuration for the P2 VMM DAQ at the SPS beam test.

Clone of the Dream beam configuration (DAQ_Control_Dream_Beam) for the VMM
readout: raw UDP from the front-ends is captured into rotating .pcapng files
per network interface (dumpcap), with optional ALINX slow control (alinx-sc)
around each sub-run. No fdf/ROOT reconstruction — online QA runs directly on
the finalized pcapng files (vmm_qa/vmm_pcapng_qa.py).

Site switching: config/site.txt on the machine (fallback: SITE below).
  'local' — full simulation on this machine (fake CAEN HV + fake TTi LV + fake
            VMM DAQ that replays a sample pcapng), for testing the whole chain
            without hardware.
  'bench' — Saclay VMM test bench: real ALINX capture, fake HV/LV.
  'sps'   — real hardware at the SPS beam line. Fields marked TODO-SPS must be
            filled in once the beam-area network / crate details are known.

@author: Alexandra Kallitsopoulou (based on Dylan Neff's nTof config)
"""

import os
import sys

from run_config_base import RunConfigBase

# ---------------------------------------------------------------------------
# Site configuration — the ONE place to switch local test <-> bench <-> SPS
# ---------------------------------------------------------------------------
# Default site; each deployed machine pins its own via the gitignored
# config/site.txt (one word: local | bench | sps) so switching site never
# means editing tracked code on a DAQ machine.
SITE = 'local'
_site_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'site.txt')
if os.path.isfile(_site_file):
    with open(_site_file) as _f:
        SITE = _f.read().strip() or SITE

SITES = {
    'local': {
        # All data under a local test tree (runs/, analysis/, sim_pcapng/, ...)
        'base_data_dir': '/local/home/ak271430/Documents/PostDocSaclay/data/sps_vmm_test/',
        'daq_host': '127.0.0.1',    # hv_control / vmm_daq / lv_control servers
        'hv_ip': 'sim',             # 'sim' -> hv_control uses FakeCAENHVController
        'hv_n_cards': 4,
        'lv_units': {               # name -> ip; 'sim' -> lv_control uses FakeTTiPSU
            'tti1': 'sim',
            'tti2': 'sim',
        },
        'simulate': True,           # fake HV + fake LV + fake VMM DAQ (replay sample pcapng)
        # Interfaces to capture on. In simulation the iface name is only used
        # for file naming; alinx_config=None skips alinx-sc entirely.
        'interfaces': [
            {'name': 'alinx', 'iface': 'enp4s0f1', 'slow_control': 'alinx',
             'alinx_config': None},
        ],
    },
    'bench': {
        # Saclay VMM test bench (dedippce185 / vmm_daplxa): real ALINX capture,
        # fake HV + fake LV ('sim' IPs — bench LV is the TDK supplies, driven
        # from the GUI's Hybrids LV Power panel, not by lv_control).
        'base_data_dir': '/local/p2/p2data/vmm_daq_bench/',
        'daq_host': '127.0.0.1',
        'hv_ip': 'sim',
        'hv_n_cards': 4,
        'lv_units': {
            'tti1': 'sim',
            'tti2': 'sim',
        },
        'simulate': False,   # real dumpcap capture on the ALINX interface
        'interfaces': [
            {'name': 'alinx', 'iface': 'enp4s0f1', 'slow_control': 'alinx',
             'alinx_config': '/local/p2/p2testbench/TestBenchCERN/config/config_alinx_noThresholds.json'},
        ],
    },
    'sps': {
        # July 2026 test beam, SPS H4: shared group data tree on the DAQ machine
        # (basketp2 group-writable, setgid + default ACLs).
        'base_data_dir': '/local/p2/p2data/TB_July26_H4/',
        'daq_host': '127.0.0.1',
        # HV: the CAEN crate hangs off banco — port 2100 here is served by
        # hv_dream_shim.py (start_servers.sh picks it at sps), which gates
        # combined runs on the Dream DAQ's readback and passes instantly for
        # VMM-only runs. hv_ip is unused by the shim; 'sim' documents that no
        # CAEN is driven from this machine.
        'hv_ip': 'sim',
        'hv_n_cards': 4,
        # No Aim-TTi units at H4 — the LV is the TDK-Lambda supplies
        # (.247-.249 on this same subnet), driven by switchOn/Off_tdk.sh and
        # monitored via the GUI power panel + per-run tdk_lv_monitor.csv.
        # The .241/.242 addresses were inherited placeholders; run_2 confirmed
        # nothing answers there. lv_monitoring off = no empty lv_monitor.csv.
        'lv_monitoring': False,
        'lv_units': {
            'tti1': '192.168.0.241',
            'tti2': '192.168.0.242',
        },
        'simulate': False,
        'interfaces': [
            # Data NIC: enp4s0f1, CONFIRMED empirically by run_2 (2026-07-29):
            # capturing on the USB adapter enx00249b8724a0 gave a 280-byte
            # empty pcapng while the pulser was firing — the yaml's
            # 192.168.0.14 destination is slow-control addressing, the VMM
            # data stream rides the 10G NIC.
            {'name': 'alinx', 'iface': 'enp4s0f1', 'slow_control': 'p2basket',
             # CERN deploy, per TestBenchCERN/README: acquisition is armed with
             # bare `p2basket-sc --acq-on/--acq-off` (no config file), run from
             # the TestBenchCERN directory. alinx-sc only exists in the old
             # December install and is NOT on PATH here — do not switch back.
             'p2basket_sc': '/local/p2/deploy/bin/p2basket-sc',
             'sc_cwd': '/local/p2/p2testbench/TestBenchCERN',
             'sc_config': 'config_base/p2b-config-cern-base.yaml',
             'alinx_config': None},
            # SRS chain — uncomment to also capture the SRS/FEC interface:
            # {'name': 'srs', 'iface': 'enx0c37968d3d99', 'slow_control': 'none',
            #  'alinx_config': None},
        ],
    },
}

_SITE_CFG = SITES[SITE]
BASE_DATA_DIR = _SITE_CFG['base_data_dir']
SIMULATE = _SITE_CFG['simulate']

# ---------------------------------------------------------------------------
# Run schedule
#   N_SUBRUNS identical sub-runs of SUBRUN_MIN minutes at the nominal P2
#   operating point. Short values for local simulation; set beam values at SPS.
# ---------------------------------------------------------------------------
# DRY RUN (2026-07-29): a single 10-second sub-run to exercise the whole GUI
# chain (arming, capture, QA, provenance) without beam and without HV — the
# sps HV server is the Dream-gating shim, which passes instantly for VMM-only
# runs, so NO detector voltage is ramped by this run.
# Restore beam values after the dry run (e.g. N_SUBRUNS = 2, SUBRUN_MIN = 2).
N_SUBRUNS = 1       # number of identical sub-runs
SUBRUN_MIN = 10 / 60  # run time per sub-run (minutes) — 10 seconds
POST_SUBRUN_PAUSE_MIN = 0   # optional pause AFTER each sub-run (minutes); 0 = no pause

# P2 stations' HV on the SPS CAEN crate (192.168.10.199, banco's DAQ LAN),
# MIRRORED 2026-07-29 from the Dream DAQ's run_config_beam.py on banco, which
# has been running these detectors so far. Keep in sync with Dream until the
# combined-run trigger makes THIS file the single edit point. NOTE: at sps no
# process on this machine drives the crate — these channels/setpoints are the
# targets carried in each sub-run ('hvs'), which the Dream-readback shim gates
# the capture on and the combined-run trigger sends to Dream to ramp.
# Channels (card, channel) confirmed at the beam 2026-07-22: card 8.
P2_HV = {
    'P2_IN':  {'drift': (8, 0), 'mesh': (8, 1)},
    'P2_MID': {'drift': (8, 2), 'mesh': (8, 3)},
    'P2_OUT': {'drift': (8, 4), 'mesh': (8, 5)},
}
# uRWELL tracking references: read out by the DREAM DAQ (not by VMM), but
# their HV is carried here too — this file is the single HV edit point for
# combined runs, and every sub-run's targets include them. They are the
# telescope reference, so scans NEVER step them (scan templates iterate
# P2_HV only); they sit at the operating point for the whole run.
URWELL_HV = {
    'EIC_uRWELL_front': {'drift': (8, 6), 'resist': (12, 0)},
    'EIC_uRWELL_back':  {'drift': (8, 7), 'resist': (12, 1)},
}
# Operating point of 2026-07-27/28 (mirrored from Dream's OPERATING_HV):
# P2_IN is the new metallic-mesh chamber, measured slightly lower.
OPERATING_HV = {
    'P2_IN':  {'drift': 740, 'mesh': 440},   # gap = 300 V
    'P2_MID': {'drift': 750, 'mesh': 450},   # gap = 300 V
    'P2_OUT': {'drift': 750, 'mesh': 450},   # gap = 300 V
    'EIC_uRWELL_front': {'drift': 620, 'resist': 420},
    'EIC_uRWELL_back':  {'drift': 620, 'resist': 420},
}

# VMM readout cabling, per station: connector -> (hybrid, bottom VMM, top VMM).
# As cabled 2026-07-29 (Alexandra). Hybrid H<n> carries VMMs 2n (bottom) and
# 2n+1 (top); hybrid 0 (VMMs 0/1) is the trigger digitizer, not on a station.
# NOTE: P2_OUT now uses THREE connectors c4-c6 (it was c4-c7 on the Dream
# readout) — same for all three stations.
P2_VMM_CABLING = {
    'P2_IN':  {'c4': (1, 2, 3),   'c5': (2, 4, 5),   'c6': (3, 6, 7)},
    'P2_MID': {'c4': (4, 8, 9),   'c5': (5, 10, 11), 'c6': (6, 12, 13)},
    'P2_OUT': {'c4': (7, 14, 15), 'c5': (8, 16, 17), 'c6': (9, 18, 19)},
}

# Capture: seconds per pcapng file (dumpcap ring-buffer rotation interval).
CAPTURE_DURATION_S = 44


class Config(RunConfigBase):
    def __init__(self, config_path=None):
        if not config_path:
            self._set_defaults()

        super().__init__(config_path)

    def _set_defaults(self, config_path=None):
        self.run_name = 'run_3'
        self.base_out_dir = BASE_DATA_DIR
        self.data_out_dir = f'{self.base_out_dir}runs/'
        self.run_out_dir = f'{self.data_out_dir}{self.run_name}/'
        self.raw_daq_inner_dir = 'raw_daq_data'
        self.start_time = None
        self.power_off_hv_at_end = False  # True to power off all CAEN HV at the end of the run.
        self.resume = False  # True to resume an existing run: skip sub-runs already marked .subrun_complete.
        self.write_all_detectors_to_json = True  # Only when making run config json template. Maybe do always?
        self.gas = 'Ar/Iso 95/5'  # Gas type for run
        # self.gas = 'Ar/CO2/Iso 93/5/2'
        self.beam_type = 'sps_beam'
        # self.beam_type = 'cosmics'
        self.target_type = 'none'
        self.trigger = 'self-triggered VMM readout'  # TODO-SPS: describe actual trigger formation

        self.vmm_daq_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2101,
            'interfaces': _SITE_CFG['interfaces'],
            'capture_duration_s': CAPTURE_DURATION_S,  # seconds per pcapng file
            'capture_tool': 'dumpcap',  # 'dumpcap' (ring buffer) or 'tcpdump' (loop, one file per invocation)
            'snaplen': 0,               # 0 = full packets
            'bpf_filter': 'udp',        # capture filter (VMM data is UDP)
            'data_out_dir': f'{self.run_out_dir}',
            'raw_daq_inner_dir': self.raw_daq_inner_dir,
            'status_interval_s': 10,    # seconds between [vmm daq] status lines
            'acq_on_retries': 3,        # alinx-sc --acq-on retries before giving up
            'max_run_time_addition': 60 * 5,  # seconds past requested run time before force stop
            # --- Simulation (SITE='local' only): instead of launching dumpcap,
            # replay a sample pcapng from sim_source_pcap_dir into the run directory.
            'simulate': SIMULATE,
            'sim_source_pcap_dir': f'{self.base_out_dir}sim_pcapng/',
            'sim_chunk_mb': 4,            # MB appended to the growing pcapng per step
            'sim_chunk_interval': 2,      # seconds between append steps
            'sim_bytes_per_file_mb': 20,  # target size of each simulated pcapng file
            'sim_file_duration_s': 30,    # seconds each simulated file takes to 'record'
        }

        self.hv_control_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2100,
        }

        self.hv_info = {
            'ip': _SITE_CFG['hv_ip'],
            'n_cards': _SITE_CFG['hv_n_cards'],
            'n_channels_per_card': 12,
            'run_out_dir': self.run_out_dir,
            'hv_monitoring': True,  # True to monitor HV during run, False to not monitor
            'monitor_interval': 1,  # Seconds between HV monitoring
            'simulate': SIMULATE,   # True -> hv_control uses FakeCAENHVController
        }

        # HV credentials: hv_creds.txt (username on line 1, password on line 2) next
        # to this file. Optional in simulation; required for the real CAEN crate.
        creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hv_creds.txt')
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                lines = f.readlines()
                self.hv_info['username'] = lines[0].strip()
                self.hv_info['password'] = lines[1].strip()
        else:
            self.hv_info['username'] = 'admin'
            self.hv_info['password'] = 'admin'
            if not SIMULATE:
                # stderr, NOT stdout: get_config_py.py's stdout is parsed as
                # JSON by the GUI — a warning line there breaks Start Run.
                print(f'WARNING: {creds_path} not found — using default admin/admin HV credentials.',
                      file=sys.stderr)

        self.lv_control_info = {
            'ip': _SITE_CFG['daq_host'],
            'port': 2102,
        }

        self.lv_info = {
            # Aim-TTi bench PSUs, SCPI over raw TCP (port 9221), up to 3 channels each.
            'units': {
                name: {'ip': ip, 'port': 9221, 'channels': [1, 2, 3]}
                for name, ip in _SITE_CFG['lv_units'].items()
            },
            'run_out_dir': self.run_out_dir,
            # Site-switchable: at sps there are NO Aim-TTi units — the LV is
            # the TDK-Lambda supplies (switchOn/Off_tdk.sh), monitored by the
            # GUI power panel (10 s auto-measure) and recorded per run in
            # tdk_lv_monitor.csv by daq_control. Monitoring 'sim'/absent TTis
            # only produced DISCONNECTED retries + empty lv_monitor.csv.
            'lv_monitoring': _SITE_CFG.get('lv_monitoring', True),
            'monitor_interval': 2,      # seconds between LV monitoring rows
            'also_write_tti_logs': False,  # True: additionally write legacy tti*_mon.log files
            'reconnect_interval': 5,    # seconds between reconnect attempts after a socket error
            'simulate': SIMULATE,       # True -> lv_control uses FakeTTiPSU
            # Optional pre-sub-run LV gate: check voltages against 'expected' below
            # and skip the sub-run if out of tolerance. Off by default.
            'check_before_subrun': False,
            'expected': {
                # 'tti1': {1: {'v': 2.5, 'v_tol': 0.2}},
            },
        }

        # ----- Run schedule (built from module constants above) -----
        # Each sub-run's 'hvs' carries the targets of ALL detectors — the three
        # P2 stations AND the uRWELL references — as {card: {channel: volts}}.
        # Scans modify only the P2 station entries; the references stay put.
        def _operating_hvs():
            hvs = {}
            for group in (P2_HV, URWELL_HV):
                for det, roles in group.items():
                    for role, (card, ch) in roles.items():
                        hvs.setdefault(str(card), {})[str(ch)] = OPERATING_HV[det][role]
            return hvs

        self.sub_runs = []
        for i in range(N_SUBRUNS):
            self.sub_runs.append({
                'sub_run_name': f'nominal_{i:02d}',
                'run_time': SUBRUN_MIN,  # Minutes
                'post_pause_s': int(round(POST_SUBRUN_PAUSE_MIN * 60)),  # pause after this sub-run (seconds)
                'hvs': _operating_hvs(),
            })

        # --- HV scan template (uncomment and adapt at the beam): step every
        # --- station's mesh DOWN from the operating point, drift following so
        # --- each station's own 300 V gap stays constant (Dream convention).
        # for j, dv in enumerate(range(0, 60, 5)):
        #     hvs = {}
        #     for det, roles in P2_HV.items():
        #         for role, (card, ch) in roles.items():
        #             hvs.setdefault(str(card), {})[str(ch)] = OPERATING_HV[det][role] - dv
        #     self.sub_runs.append({
        #         'sub_run_name': f'meshscan_m{dv:02d}V',
        #         'run_time': 20,
        #         'hvs': hvs,
        #     })

        self.included_detectors = ['P2_IN', 'P2_MID', 'P2_OUT']

        # The three P2 telescope stations, mirrored from the Dream config on
        # banco (names, z positions, HV channels) — same detectors, now read
        # out by the VMM chain, with the per-station cabling of 2026-07-29
        # (P2_VMM_CABLING above). The uRWELL references are deliberately NOT
        # detectors here — Dream reads them; only their HV rides along in the
        # sub-run targets (URWELL_HV).
        _P2_Z_MM = {'P2_IN': 320.0, 'P2_MID': 630.0, 'P2_OUT': 940.0}
        self.detectors = [
            {
                'name': det,
                'description': f'P2 telescope {det.split("_")[1]} '
                               f'(z={_P2_Z_MM[det]:.0f} mm), VMM readout. '
                               'HV channels mirrored from the Dream config.',
                'det_type': 'P2',
                'resist_type': 'none',
                'bulked_from': 'Alex+Enzo',
                'det_center_coords': {'x': 0, 'y': 0, 'z': _P2_Z_MM[det]},  # mm
                'det_orientation': {'x': 0, 'y': 0, 'z': 0},  # deg
                'hv_channels': P2_HV[det],
                # VMM readout cabling (informational; labels QA plots).
                'vmm_map': {
                    'enp4s0f1': {
                        'hybrids': [h for h, _, _ in P2_VMM_CABLING[det].values()],
                        'vmms': sorted(v for _, b, t in P2_VMM_CABLING[det].values()
                                       for v in (b, t)),
                        'connectors': {
                            c: {'hybrid': h, 'bot_vmm': b, 'top_vmm': t}
                            for c, (h, b, t) in P2_VMM_CABLING[det].items()},
                    },
                },
            }
            for det in ['P2_IN', 'P2_MID', 'P2_OUT']
        ]

        if not self.write_all_detectors_to_json:
            self.detectors = [det for det in self.detectors if det['name'] in self.included_detectors]


if __name__ == '__main__':
    out_run_dir = 'config/json_run_configs/'
    os.makedirs(out_run_dir, exist_ok=True)

    config_name = 'run_config_beam.json'

    config = Config()

    config.write_to_file(f'{out_run_dir}{config_name}')

    # Schedule summary — sanity-check timing and the HV setpoints.
    run_min = sum(sr['run_time'] for sr in config.sub_runs)
    n_sub = len(config.sub_runs)
    total_h = run_min / 60
    ifaces = [f"{i['name']}({i['iface']})" for i in config.vmm_daq_info['interfaces']]
    print(f'Site: {SITE}  (simulate={SIMULATE})')
    print(f'Base data dir: {BASE_DATA_DIR}')
    print(f'Gas: {config.gas}')
    for _det, _p in OPERATING_HV.items():
        _roles = '   '.join(f'{r} {v} V' for r, v in _p.items())
        _gap = f'   (gap = {_p["drift"] - _p["mesh"]} V)' if 'mesh' in _p else '   (Dream-read reference)'
        print(f'{_det}: {_roles}{_gap}')
    print(f'Capture: {", ".join(ifaces)}  ({config.vmm_daq_info["capture_tool"]}, '
          f'{config.vmm_daq_info["capture_duration_s"]} s/file)')
    print(f'LV units: {", ".join(config.lv_info["units"].keys())}')
    print(f'Sub-runs: {n_sub} x {SUBRUN_MIN} min = {run_min} min (~{total_h:.2f} h + overhead)')

    print('donzo')
