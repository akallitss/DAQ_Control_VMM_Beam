#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on April 29 8:41 PM 2024
Created in PyCharm
Created as Cosmic_Bench_DAQ_Control/Server.py

@author: Dylan Neff, Dylan
"""

import socket
import time
import json
import struct


class Server:
    def __init__(self, port=1100):
        self.port = port
        self.server_host = '0.0.0.0'
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_socket = None
        self.client_address = None
        self.max_recv = 1024 * 1000  # Max bytes to receive

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client_socket is not None:
            self.client_socket.close()
        self.server.close()
        print('Server closed')

    def start(self):
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow immediate reuse of address
        self.server.bind((self.server_host, self.port))
        self.server.listen(1)
        print(f"Listening on {self.server_host}:{self.port}")
        self.accept_connection()

    def accept_connection(self):
        while True:
            try:
                self.client_socket, self.client_address = self.server.accept()
                print(f"{self.client_address[0]}:{self.client_address[1]} Connected")
                break
            except socket.error:
                print("Connection error. Retrying...")
                time.sleep(1)

    def _recv_exactly(self, n):
        data = b''
        while len(data) < n:
            chunk = self.client_socket.recv(n - len(data))
            if not chunk:
                return b''
            data += chunk
        return data

    def receive(self):
        length_header = self._recv_exactly(4)
        if not length_header:
            return ''
        length = struct.unpack('!I', length_header)[0]
        text = self._recv_exactly(length).decode()
        print(f"Received: {text}")
        return text

    def receive_json(self):
        length_header = self._recv_exactly(4)
        if not length_header:
            return None
        length = struct.unpack('!I', length_header)[0]
        data = json.loads(self._recv_exactly(length).decode())
        print(f"Received: {data}")
        return data

    def send(self, data, silent=False):
        encoded = data.encode()
        self.client_socket.sendall(struct.pack('!I', len(encoded)) + encoded)
        if not silent:
            print(f"Sent: {data}")

    def send_json(self, data):
        json_data = json.dumps(data).encode()
        length = struct.pack('!I', len(json_data))  # Pack length as a 4-byte unsigned integer
        self.client_socket.sendall(length + json_data)
        print(f"Sent: {data}")

    def set_blocking(self, blocking=True):
        self.client_socket.setblocking(blocking)

    def set_timeout(self, timeout):
        self.client_socket.settimeout(timeout)

    def get_timeout(self):
        return self.client_socket.gettimeout()
