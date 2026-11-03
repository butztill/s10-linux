#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Samsung IPC fmt (SIPC ch 0xeb) helper. Frame: u16 len, u8 seq, u8 ack, u8 main, u8 sub, u8 type, payload.
Types: tx EXEC=1 GET=2 SET=3 CFRM=4 EVENT=5; rx INDI=1 RESP=2 NOTI=3."""
import os, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s10cp import Link

CH = 0xeb
NAMES = {0x0101: 'PWR_PHONE_PWR_UP', 0x0102: 'PWR_PHONE_STATE', 0x0109: 'PWR_MODEM_STATUS', 0x0701: 'DISP_ICON',
         0x0706: 'DISP_RSSI', 0x0805: 'NET_REGIST', 0x0803: 'NET_SERVING', 0x0501: 'SEC_PIN_STATUS'}


class Ipc:
    def __init__(self, link=None):
        self.l = link or Link()
        self.seq = 0

    def tx(self, main, sub, typ, payload=b''):
        self.seq = (self.seq + 1) & 0xff
        self.l.send(CH, struct.pack('<HBBBBB', 7 + len(payload), self.seq, 0, main, sub, typ) + payload)
        return self.seq

    def rx(self):
        out = []
        for p in self.l.poll_dl(CH):
            if len(p) >= 7:
                ln, seq, ack, main, sub, typ = struct.unpack_from('<HBBBBB', p)
                out.append((main, sub, typ, seq, ack, p[7:ln]))
        return out

    def wait(self, secs=3, show=True, skip=((7, 6),)):
        t = time.time(); got = []
        while time.time() - t < secs:
            for m in self.rx():
                got.append(m)
                if show and (m[0], m[1]) not in skip:
                    print('<< %02x/%02x type=%d seq=%d ack=%d %s %s' % (m[0], m[1], m[2], m[3], m[4], NAMES.get(m[0] << 8 | m[1], ''), m[5].hex(' ')), flush=True)
            time.sleep(0.01)
        return got


if __name__ == '__main__':
    ipc = Ipc()
    for arg in sys.argv[1:]:
        # "main.sub.type[:hexpayload]" e.g. 01.09.02 or 01.09.03:01
        h, _, pl = arg.partition(':')
        m, s, t = (int(x, 16) for x in h.split('.'))
        print('>> %02x/%02x type=%d %s' % (m, s, t, pl), flush=True)
        ipc.tx(m, s, t, bytes.fromhex(pl))
        ipc.wait(float(os.environ.get('WAIT', '3')))
