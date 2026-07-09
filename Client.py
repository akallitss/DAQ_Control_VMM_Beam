#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on April 29 8:48 PM 2024
Created in PyCharm
Created as Cosmic_Bench_DAQ_Control/Client.py

@author: Dylan Neff, Dylan
"""

import socket
import time
import json
import struct


class Client:
    def __init__(self, host, port=1100):
        self.host = host
        self.port = port
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.max_recv = 1024 * 1000  # Max bytes to receive
        self.silent = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()
        if not self.silent:
            print('Client closed')

    def start(self):
        # while True:
        try:
            self.client.connect((self.host, self.port))
            if not self.silent:
                print(f"Connected to {self.host}:{self.port}")
            # break
        except (ConnectionRefusedError, OSError) as e:
            # print(f"Failed to connect to {self.host}:{self.port}. Retrying...")
            # time.sleep(1)
            if not self.silent:
                print(f"Failed to connect to {self.host}:{self.port}. {e}")

    def _recv_exactly(self, n):
        data = b''
        while len(data) < n:
            chunk = self.client.recv(n - len(data))
            if not chunk:
                return b''
            data += chunk
        return data

    def receive(self, silent=False):
        length_header = self._recv_exactly(4)
        if not length_header:
            return ''
        length = struct.unpack('!I', length_header)[0]
        text = self._recv_exactly(length).decode()
        if not (self.silent or silent):
            print(f"Received: {text}")
        return text

    def receive_json(self):
        length_header = self._recv_exactly(4)
        if not length_header:
            return None
        length = struct.unpack('!I', length_header)[0]
        data = json.loads(self._recv_exactly(length).decode())
        if not self.silent:
            print(f"Received: {data}")
        return data

    def send(self, data, silent=False):
        encoded = data.encode()
        self.client.sendall(struct.pack('!I', len(encoded)) + encoded)
        if not (self.silent or silent):
            print(f"Sent: {data}")

    def send_json(self, data):
        json_data = json.dumps(data).encode()
        length = struct.pack('!I', len(json_data))  # Pack length as a 4-byte unsigned integer
        self.client.sendall(length + json_data)
        if not self.silent:
            print(f"Sent: {data}")

    def set_blocking(self, blocking=True):
        self.client.setblocking(blocking)

    def set_timeout(self, timeout):
        self.client.settimeout(timeout)
