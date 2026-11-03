#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""gnss_probe.py [seconds] [hexdata...]: boot the BCM4773 hub (sensor patch), start the GNSS "Navigation" channel and log what it sends.
Stock lhd RPC ids (from samsung-gpsd, lhe_imp.cpp): host->hub 0x1c Navigation Start(u8 on/off), 0x1e Navigation Data(u16 len + bytes);
hub->host 0x1f Navigation output(u16 len + bytes), 0x38 GNSS request(u16 len + bytes).
Run as root with s10-sensord stopped:  systemctl stop s10-sensord; python3 gnss_probe.py 40; systemctl start s10-sensord
Only protocol bytes are printed (no user data is involved)."""
import struct, sys, time
sys.path.insert(0, '/usr/local/lib/s10-sensors')
sys.path.insert(0, '.')
import s10shub

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30
hub = s10shub.Hub(log=lambda *a: print('hub:', *a, flush=True))
hub.boot()
raw = bytearray()
seen = {}


def drain(t=0.05):
    hub.pump(t)
    for p in hub.pk:
        pl = p['payload']
        while len(pl) >= 2:
            rid = pl[0]; l = pl[1]; o = 2
            if rid & 0x80:
                rid = ((rid & 0x7f) << 8) | pl[1]; l = pl[2]; o = 3
                if l & 0x80:
                    l = ((l & 0x7f) << 8) | pl[3]; o = 4
            elif l & 0x80:
                l = ((l & 0x7f) << 8) | pl[2]; o = 3
            body = pl[o:o + l]; pl = pl[o + l:]
            seen[rid] = seen.get(rid, 0) + 1
            if rid in (0x1f, 0x38):
                n = struct.unpack_from('<H', body)[0] if len(body) >= 2 else 0
                data = body[2:2 + n]
                raw.extend(data)
                print('RPC %#x len %d: %s | %r' % (rid, n, data[:48].hex(' '), bytes(data[:48])), flush=True)
            elif rid not in (0x21, 0x03):
                print('RPC %#x len %d: %s' % (rid, len(body), body[:32].hex(' ')), flush=True)
            else:
                pass
    hub.pk = []


try:
    drain(0.2)
    print('send Navigation Start(1)', flush=True)
    hub.reliable(hub.rpc(0x1c, b'\x01'))
    for h in sys.argv[2:]:
        d = bytes.fromhex(h)
        print('send Navigation Data', d.hex(' '), flush=True)
        hub.reliable(hub.rpc(0x1e, struct.pack('<H', len(d)) + d))
    t0 = time.time()
    while time.time() - t0 < secs:
        drain(0.1)
finally:
    print('rpc id counts:', {hex(k): v for k, v in seen.items()}, 'gnss bytes', len(raw), flush=True)
    open('/tmp/gnss-raw.bin', 'wb').write(raw)
    hub.power_off()
