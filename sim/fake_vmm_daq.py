#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fake VMM DAQ for local testing (SITE='local').

Mimics one dumpcap ring-buffer capture: writes dumpcap-named pcapng files
(<iface>_<seq:05d>_<YYYYMMDDHHMMSS>.pcapng) into the subrun raw dir, growing
each file in place chunk-by-chunk (like dumpcap does) and only starting file
seq+1 after seq is flushed and closed — so the QA watcher's "finalized when a
successor exists" gate is exercised exactly as in a real run.

Every finished file is a fully valid pcapng: the replayed bytes come from a
real sample capture (sim_source_pcap_dir) cut at a pcapng block boundary.

@author: Alexandra Kallitsopoulou
"""

import os
import struct
import time
from datetime import datetime


def _pcapng_block_boundaries(path, max_bytes):
    """Byte offsets of pcapng block boundaries in path, up to ~max_bytes.

    pcapng is a sequence of blocks, each: type (4B) + total length (4B) +
    body + total length (4B), lengths 4-byte aligned. Endianness comes from
    the Section Header Block's byte-order magic (offset 8).
    Returns a list of offsets where a file cut yields a valid pcapng; always
    includes at least the first two blocks (SHB + interface description).
    """
    file_size = os.path.getsize(path)
    boundaries = []
    with open(path, 'rb') as f:
        header = f.read(12)
        if len(header) < 12 or header[:4] != b'\x0a\x0d\x0d\x0a':
            raise ValueError(f'{path} is not a pcapng file')
        endian = '<' if header[8:12] == b'\x4d\x3c\x2b\x1a' else '>'
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            _btype, blen = struct.unpack(endian + 'II', hdr)
            if blen < 12 or blen % 4 != 0 or pos + blen > file_size:
                break  # corrupt/truncated tail — stop at last good boundary
            pos += blen
            boundaries.append(pos)
            if pos >= max_bytes and len(boundaries) >= 2:
                break
    if not boundaries:
        raise ValueError(f'No valid pcapng blocks found in {path}')
    return boundaries


def _find_source_pcap(sim_source_pcap_dir):
    """Largest .pcapng in the sim source dir."""
    candidates = [os.path.join(sim_source_pcap_dir, f)
                  for f in os.listdir(sim_source_pcap_dir)
                  if f.endswith(('.pcapng', '.pcap'))]
    if not candidates:
        raise FileNotFoundError(f'No sample pcapng in {sim_source_pcap_dir}')
    return max(candidates, key=os.path.getsize)


def run_simulated_capture(raw_dir, iface, info, stop_check):
    """Replay the sample pcapng into raw_dir as a sequence of dumpcap-named files.

    Args:
        raw_dir: subrun raw_daq_data directory to write into.
        iface: interface name used in the file names.
        info: effective vmm_daq_info dict (sim_* keys).
        stop_check: callable returning True when the capture should stop.
    """
    source = _find_source_pcap(info['sim_source_pcap_dir'])
    target_bytes = int(info.get('sim_bytes_per_file_mb', 20)) * 1024 * 1024
    chunk_bytes = int(info.get('sim_chunk_mb', 4)) * 1024 * 1024
    chunk_interval = float(info.get('sim_chunk_interval', 2))
    file_duration = float(info.get('sim_file_duration_s', 30))

    boundaries = _pcapng_block_boundaries(source, target_bytes)
    cut_len = boundaries[-1]  # largest block-aligned length <= target
    print(f'[sim vmm] Replaying {os.path.basename(source)} on {iface}: '
          f'{cut_len / 1024 / 1024:.1f} MB per file')

    seq = 1
    with open(source, 'rb') as src:
        while not stop_check():
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            out_name = f'{iface}_{seq:05d}_{ts}.pcapng'
            out_path = os.path.join(raw_dir, out_name)
            file_start = time.time()
            src.seek(0)
            written = 0
            with open(out_path, 'wb') as out:
                while written < cut_len:
                    chunk = src.read(min(chunk_bytes, cut_len - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    out.flush()
                    written += len(chunk)
                    if stop_check():
                        break
                    if written < cut_len:
                        time.sleep(chunk_interval)
                if written < cut_len:
                    # Stopped mid-file: truncate to the last block boundary so
                    # the finalized file is still a valid pcapng (like dumpcap
                    # finalizing on SIGINT).
                    good = max((b for b in boundaries if b <= written), default=0)
                    out.truncate(good)
                out.flush()
                os.fsync(out.fileno())
            print(f'[sim vmm] file {out_name} complete '
                  f'({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)')
            if stop_check():
                break
            # Pad to the nominal per-file duration so rotation pacing is realistic.
            while time.time() - file_start < file_duration and not stop_check():
                time.sleep(0.5)
            seq += 1
    print(f'[sim vmm] capture stopped after {seq} file(s) on {iface}')


if __name__ == '__main__':
    # Self-test: write 3 small files into a scratch dir and verify each parses.
    import sys
    import tempfile

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from run_config_beam import Config

    config = Config()
    info = dict(config.vmm_daq_info)
    info.update({'sim_bytes_per_file_mb': 2, 'sim_chunk_mb': 1,
                 'sim_chunk_interval': 0.1, 'sim_file_duration_s': 0.5})

    files_written = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        state = {'n': 0}

        def stop_after_three():
            state['n'] = len([f for f in os.listdir(tmp_dir) if f.endswith('.pcapng')])
            return state['n'] >= 3

        run_simulated_capture(tmp_dir, 'testiface', info, stop_after_three)

        from scapy.all import PcapReader  # noqa: E402  (venv dependency)
        for name in sorted(os.listdir(tmp_dir)):
            path = os.path.join(tmp_dir, name)
            n_pkts = sum(1 for _ in PcapReader(path))
            print(f'self-test: {name}  {os.path.getsize(path)} bytes  {n_pkts} packets')
            assert n_pkts > 0, f'{name} unreadable or empty'
            files_written.append(name)
    assert len(files_written) >= 3
    print('fake_vmm_daq self-test OK')
