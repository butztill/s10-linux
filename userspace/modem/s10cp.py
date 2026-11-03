# SPDX-License-Identifier: GPL-2.0-only
"""Userspace side of the S10 Shannon SBD link (layout identical to boot/s10-cp-boot.c initsbd)."""
import os, struct, time

D = '/sys/kernel/debug/s10cp/'
IPC = 0xb200000
DESC_OFF = 2048
BUFF_OFF = 65536
MAX_CH = 32
# (sipc channel, ul_len, ul_bsz, dl_len, dl_bsz) in link-id order
CHANS = [
    (0xf4, 0x10, 0x800, 0x20, 0x800), (0x1c, 0x10, 0x800, 0x80, 0x800),
    (0x19, 0x10, 0x800, 0x10, 0x800), (0x01, 0x20, 0x800, 0x40, 0x800),
    (0xf5, 0x10, 0x800, 0x200, 0x800), (0xec, 0x10, 0x1000, 0x20, 0x1000),
    (0xeb, 0x10, 0x1000, 0x20, 0x1000), (0x23, 0x10, 0x800, 0x10, 0x800),
    (0x21, 0x10, 0x800, 0x80, 0x800), (0x1a, 0x10, 0x800, 0x80, 0x800),
    (0x0a, 0x200, 0x800, 0x400, 0x08), (0x0b, 0x100, 0x800, 0x100, 0x800),   # PS data rings (QoS "10" / "11")
]
NCH = len(CHANS)


class Link:
    def __init__(self):
        self.shm = os.open(D + 'shm', os.O_RDWR)
        self.ctl = os.open(D + 'ctl', os.O_WRONLY)
        self.mb = D + 'mbox'
        gsz = 12 + MAX_CH * 16 + MAX_CH * 2 * 20
        desc = DESC_OFF + gsz
        self.rbps = desc
        desc += 2 * 2 * 2 * NCH
        buff = BUFF_OFF
        self.ring = {}
        for i, (ch, ul, ulb, dl, dlb) in enumerate(CHANS):
            r = {'id': i}
            for d, (ln, bs) in enumerate(((ul, ulb), (dl, dlb))):
                r[d] = dict(len=ln, bsz=bs, size_v=desc + ln * 4, buf=buff)
                desc += ln * 8
                buff += ln * bs
            self.ring[ch] = r

    def rd(self, off, n):
        return os.pread(self.shm, n, IPC + off)

    def wr(self, off, data):
        os.pwrite(self.shm, data, IPC + off)

    def rbp(self, d, wr, i):
        return struct.unpack('<H', self.rd(self.rbps + 2 * (NCH * (d * 2 + wr) + i), 2))[0]

    def set_rbp(self, d, wr, i, v):
        self.wr(self.rbps + 2 * (NCH * (d * 2 + wr) + i), struct.pack('<H', v))

    def cmd(self, s):
        try:
            os.write(self.ctl, s.encode())
        except OSError:
            pass

    def irq(self, v):
        self.cmd('irq %#x' % v)

    def mbox(self):
        a, b, c = open(self.mb).read().split()
        return int(a, 16), int(b, 16), int(c, 16)

    def poll_dl(self, ch):
        """Return list of SIPC5 payloads received on channel ch."""
        r = self.ring[ch]
        i, dl = r['id'], r[1]
        rp, wp = self.rbp(1, 0, i), self.rbp(1, 1, i)
        out = []
        while rp != wp:
            n = struct.unpack('<I', self.rd(dl['size_v'] + 4 * rp, 4))[0] & 0xffff
            fr = self.rd(dl['buf'] + rp * dl['bsz'], n)
            if len(fr) >= 4 and fr[0] & 0xf8 == 0xf8:
                flen = struct.unpack_from('<H', fr, 2)[0]
                out.append(fr[4:flen])
            rp = (rp + 1) % dl['len']
            self.set_rbp(1, 0, i, rp)
        return out

    def send(self, ch, payload):
        r = self.ring[ch]
        i, ul = r['id'], r[0]
        maxp = ul['bsz'] - 4
        for o in range(0, max(len(payload), 1), maxp):
            chunk = payload[o:o + maxp]
            while True:
                wp, rp = self.rbp(0, 1, i), self.rbp(0, 0, i)
                if (wp + 1) % ul['len'] != rp:
                    break
                time.sleep(0.001)
            fr = bytes([0xf8, ch]) + struct.pack('<H', len(chunk) + 4) + chunk
            self.wr(ul['buf'] + wp * ul['bsz'], fr)
            self.wr(ul['size_v'] + 4 * wp, struct.pack('<I', len(fr)))
            self.set_rbp(0, 1, i, (wp + 1) % ul['len'])
            self.irq(0x81)


    # ---- PS data ----
    def send_ps(self, packet, pdp_ch=0x0a, ring_ch=0x0b):
        """Uplink raw IP packet on the multipdp SBD ring; size_v = len | pdp channel << 16 (no SIPC5 header)."""
        r = self.ring[ring_ch]
        i, ul = r['id'], r[0]
        wp, rp = self.rbp(0, 1, i), self.rbp(0, 0, i)
        if (wp + 1) % ul['len'] == rp or len(packet) > ul['bsz']:
            return False
        self.wr(ul['buf'] + wp * ul['bsz'], packet)
        self.wr(ul['size_v'] + 4 * wp, struct.pack('<I', len(packet) | (pdp_ch << 16)))
        self.set_rbp(0, 1, i, (wp + 1) % ul['len'])
        self.irq(0x81)
        return True

    # pktproc downlink: global desc in the 'spare' window (CP 0x50000000 == spare+0)
    SPARE = D + 'spare'

    def _spare(self):
        if not hasattr(self, '_sp'):
            self._sp = os.open(self.SPARE, os.O_RDWR)
        return self._sp

    def poll_pktproc(self):
        """Return list of (pdp_channel, ip_packet) from the CP pktproc ring and advance rear_ptr."""
        fd = self._spare()
        fore, rear = struct.unpack('<II', os.pread(fd, 8, 12))
        nd = struct.unpack('<I', os.pread(fd, 4, 4))[0] or 8192
        out = []
        while rear != fore:
            d = os.pread(fd, 24, 0x100 + rear * 24)
            addr, info, _, _, filt, length, chan = struct.unpack_from('<IIIHHHB', d)
            if length <= 2048 and chan != 0xff:
                pkt = os.pread(fd, length, addr & ~0x50000000)
                out.append((chan, pkt))
            rear = (rear + 1) % nd
            os.pwrite(fd, struct.pack('<I', rear), 16)
        return out
