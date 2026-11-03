# SPDX-License-Identifier: GPL-2.0-only
"""Client side of the imsd unix socket (userspace/vowifi/imsd.py): SMS over IMS. Non-blocking, reconnects by itself.
Never logs message contents."""
import json
import os
import socket
import time

SOCK = '/run/s10modem/ims.sock'


class ImsLink:
    def __init__(self, path=SOCK):
        self.path = path
        self.sock = None
        self.buf = b''
        self.registered = False
        self.next_id = 1
        self.last_try = 0
        self.call_events = []

    def _connect(self):
        if self.sock is not None or time.monotonic() - self.last_try < 5 or not os.path.exists(self.path):
            return
        self.last_try = time.monotonic()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
        except OSError:
            s.close()
            return
        s.setblocking(False)
        self.sock, self.buf = s, b''

    def _drop(self):
        if self.sock is not None:
            self.sock.close()
        self.sock, self.registered = None, False

    def _write(self, obj):
        if self.sock is None:
            raise OSError('IMS not connected')
        try:
            self.sock.sendall((json.dumps(obj) + '\n').encode())
        except OSError:
            self._drop()
            raise

    def poll(self):
        """Return a list of event dicts (status events are consumed here)."""
        self._connect()
        out = []
        if self.sock is None:
            return out
        try:
            data = self.sock.recv(65536)
            if not data:
                self._drop()
                return out
            self.buf += data
        except BlockingIOError:
            pass
        except OSError:
            self._drop()
            return out
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            ev = json.loads(line)
            if ev.get('op') == 'status':
                self.registered = bool(ev['registered'])
            elif ev.get('op') == 'call':
                self.call_events.append(ev)
            else:
                out.append(ev)
        return out

    def send(self, smsc, tpdu):
        rid = self.next_id
        self.next_id += 1
        self._write({'op': 'send', 'id': rid, 'smsc': bytes(smsc).hex(), 'tpdu': bytes(tpdu).hex()})
        return rid

    def ack(self, delivery_id, ok):
        self._write({'op': 'ack', 'id': delivery_id, 'ok': bool(ok)})

    def command(self, op, **kw):
        self._write(dict(kw, op=op))
