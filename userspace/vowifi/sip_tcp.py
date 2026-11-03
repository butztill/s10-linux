# SPDX-License-Identifier: GPL-2.0-only
"""Bounded SIP TCP framing/queues. No SIM, network setup or logging."""
import re

MAX_HEADER = 65536
MAX_FRAME = 1024 * 1024


class TcpPeer(tuple):
    def __new__(cls, address, connection):
        peer = super().__new__(cls, address)
        peer.connection = connection
        return peer


class SipConnection:
    def __init__(self, sock, address):
        self.sock = sock
        self.sock.setblocking(False)
        self.peer = TcpPeer(address, self)
        self.incoming = bytearray()
        self.outgoing = bytearray()

    def feed(self, data):
        self.incoming.extend(data)
        if len(self.incoming) > 2 * MAX_FRAME:
            raise ValueError('SIP TCP input limit')
        frames = []
        while self.incoming:
            if self.incoming.startswith(b'\r\n'):
                del self.incoming[:2]  # ignore keepalive/pong lines between SIP frames
                continue
            end = self.incoming.find(b'\r\n\r\n')
            if end < 0:
                if len(self.incoming) > MAX_HEADER:
                    raise ValueError('SIP TCP header limit')
                break
            if end > MAX_HEADER:
                raise ValueError('SIP TCP header limit')
            head = re.sub(rb'\r\n[ \t]+', b' ', bytes(self.incoming[:end]))
            lengths = []
            for line in head.split(b'\r\n')[1:]:
                key, sep, value = line.partition(b':')
                if sep and key.strip().lower() in (b'content-length', b'l'):
                    value = value.strip()
                    if not re.fullmatch(rb'[0-9]{1,8}', value):
                        raise ValueError('SIP TCP invalid length')
                    lengths.append(int(value))
            if len(lengths) != 1:
                raise ValueError('SIP TCP requires single Content-Length')
            size = end + 4 + lengths[0]
            if size > MAX_FRAME:
                raise ValueError('SIP TCP frame limit')
            if len(self.incoming) < size:
                break
            frames.append(bytes(self.incoming[:size]))
            del self.incoming[:size]
        return frames

    def send(self, data):
        if len(self.outgoing) + len(data) > MAX_FRAME:
            raise ValueError('SIP TCP output limit')
        self.outgoing.extend(data)
        self.flush()

    def flush(self):
        while self.outgoing:
            try:
                count = self.sock.send(self.outgoing)
            except BlockingIOError:
                return
            if count == 0:
                raise OSError('SIP TCP connection closed')
            del self.outgoing[:count]

    def close(self):
        self.sock.close()
        self.incoming.clear()
        self.outgoing.clear()
