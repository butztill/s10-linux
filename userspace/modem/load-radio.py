#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Usage (root): load-radio.py [/dev/disk/by-partlabel/radio]
# Copies BOOT/MAIN/VSS from the Samsung radio TOC image into the modem_if shm via debugfs.
import struct, sys
DBG = '/sys/kernel/debug/s10cp/'
BASE = 0x40000000
src = open(sys.argv[1] if len(sys.argv) > 1 else '/dev/disk/by-partlabel/radio', 'rb')
hdr = src.read(0x20 + 8 * 32)
assert hdr[:3] == b'TOC', 'no TOC'
n = struct.unpack_from('<I', hdr, 0x1c)[0]
shm = open(DBG + 'shm', 'r+b', buffering=0)
sizes = {}
for i in range(n):
    name, off, addr, size, crc, id_ = struct.unpack_from('<12sIIIII', hdr, 0x20 + 32 * i)
    name = name.rstrip(b'\0').decode()
    print(f'{name:8} file+{off:#x} addr={addr:#x} size={size:#x} id={id_}')
    if id_ not in (1, 2, 3) or not size:
        continue
    src.seek(off); shm.seek(addr - BASE)
    left = size
    while left:
        b = src.read(min(left, 1 << 20)); assert b
        shm.write(b); left -= len(b)
    sizes[id_] = size
    print('  -> shm+%#x done' % (addr - BASE))
# NV data (IMEI, calibration): the stock cbd copies efs/nv_data.bin to the TOC 'NV' region (CP 0x45600000, 1 MiB)
import os
nv = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rfs-root', 'nv_data.bin')
if os.path.exists(nv):
    data = open(nv, 'rb').read()[:0x100000]
    shm.seek(0x45600000 - BASE); shm.write(data)
    print('NV region loaded from', nv, len(data))
open(DBG + 'ctl', 'w').write('sizes %#x %#x\n' % (sizes[1], sizes[2]))
