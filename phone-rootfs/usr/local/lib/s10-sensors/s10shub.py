# SPDX-License-Identifier: GPL-2.0-only
"""Userspace driver for the Galaxy S10 (SM-G973F) Broadcom BCM4773 sensor hub.

Everything here was reverse engineered from the stock kernel (bbd/bcm_spi/ssp) and the
vendor `lhd` daemon:
  * power:    GPS_PWR_EN (gpg0-2) high boots the ROM; MCU_REQ (gpg4-1) high opens the SSI link
  * link:     SSI over SPI mode 3 (spidev1.0), 16 bit length, flow control byte pair
  * transport: HDLC-ish frames  b0 00 | seq | len | flags | fields | payload | crc | b0 01
  * RPC:      id,len,body inside transport payloads (5=memory write, 0x37=service start,
              0x20/0x21 = sensor channel to/from the hub)
  * SSP:      Samsung sensor-hub message layer on RPC 0x20/0x21
"""
import os, fcntl, struct, time, re, array, select

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.environ.get('SHUB_PATCH', os.path.join(HERE, 'shub_bbd_patch.bin'))
def _crc8_table():
    """Generate the transport CRC-8 table (polynomial 0x4d, MSB first)."""
    result = bytearray()
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = ((crc << 1) ^ (0x4d if crc & 0x80 else 0)) & 0xff
        result.append(crc)
    return bytes(result)


CRCTAB = _crc8_table()

# ---------------------------------------------------------------- GPIO (sysfs)
def _find_gpio(label, off):
    for c in os.listdir('/sys/class/gpio'):
        if c.startswith('gpiochip'):
            d = '/sys/class/gpio/' + c
            if open(d + '/label').read().strip() == label:
                return int(open(d + '/base').read()) + off
    raise RuntimeError('gpiochip %s not found' % label)

class Pin:
    def __init__(self, label, off, out=None, edge=None):
        self.n = _find_gpio(label, off)
        d = '/sys/class/gpio/gpio%d' % self.n
        if not os.path.isdir(d):
            open('/sys/class/gpio/export', 'w').write(str(self.n))
        self.d = d
        if out is not None:
            open(d + '/direction', 'w').write('out')
            open(d + '/value', 'w').write(str(int(out)))
        else:
            open(d + '/direction', 'w').write('in')
        if edge:
            try: open(d + '/edge', 'w').write(edge)
            except OSError: pass
        self.f = open(d + '/value', 'r+')
    def get(self):
        self.f.seek(0); return int(self.f.read().strip() or 0)
    def set(self, v):
        self.f.seek(0); self.f.write(str(int(v))); self.f.flush()
    def fileno(self): return self.f.fileno()

# ---------------------------------------------------------------- SPI
def _ioc(d, t, nr, sz): return (d << 30) | (sz << 16) | (ord(t) << 8) | nr

class Spi:
    def __init__(self, dev='/dev/spidev1.0', speed=12000000):
        self.fd = os.open(dev, os.O_RDWR)
        fcntl.ioctl(self.fd, _ioc(1, 'k', 1, 1), struct.pack('B', 3))
        fcntl.ioctl(self.fd, _ioc(1, 'k', 4, 4), struct.pack('I', speed))
    def xfer(self, tx):
        n = len(tx); txb = array.array('B', tx); rxb = array.array('B', bytes(n))
        t = struct.pack('QQIIHBBBBI', txb.buffer_info()[0], rxb.buffer_info()[0], n, 0, 0, 0, 0, 0, 0, 0)
        fcntl.ioctl(self.fd, _ioc(1, 'k', 0, 32), t)
        return bytes(rxb)

# ---------------------------------------------------------------- transport framing
def _stuff(b):
    return {0xb0: b'\xb0\x03', 0x11: b'\xb0\x04', 0x13: b'\xb0\x05'}.get(b, bytes([b]))

def _unstuff(b):
    o = bytearray(); i = 0
    while i < len(b):
        if b[i] == 0xb0 and i + 1 < len(b):
            o.append({3: 0xb0, 4: 0x11, 5: 0x13}.get(b[i + 1], b[i + 1])); i += 2
        else:
            o.append(b[i]); i += 1
    return bytes(o)

def _crc(data):
    c = 0
    for b in data: c = CRCTAB[c ^ b]
    return c

def build(seq, ptype, payload, p4=0):
    fl = ptype
    if len(payload) >> 8: fl |= 0x40
    if fl >= 0x100: fl |= 0x80
    hdr = bytes([len(payload) & 255, fl & 255])
    fields = b''; f = fl
    for k in range(16):
        bit = 1 << k
        if f & bit:
            if bit == 0x80: fields += bytes([f >> 8])
            elif bit in (0x100, 0x200): fields += b'\0'
            elif bit == 0x40: fields += bytes([len(payload) >> 8])
            elif bit == 2: fields += bytes([p4])
            else: raise ValueError('flag %x' % bit)
            f ^= bit
        if not f: break
    cs = _crc(hdr + fields + payload); cs = ((cs >> 4) & 0xf) | ((cs << 4) & 0xff)
    return (b'\xb0\x00' + _stuff(seq) + _stuff(hdr[0]) + _stuff(hdr[1]) +
            b''.join(_stuff(x) for x in fields) + b''.join(_stuff(x) for x in payload) +
            _stuff(cs) + b'\xb0\x01')

def parse(stream):
    out = []
    for m in stream.split(b'\xb0\x01'):
        if not m.startswith(b'\xb0\x00'): continue
        d = _unstuff(m[2:])
        try:
            seq, ln, fl = d[0], d[1], d[2]; p = 3; f = fl; fields = {}
            for k in range(16):
                bit = 1 << k
                if f & bit:
                    if bit == 0x40: fields['hi'] = d[p]; ln |= d[p] << 8; p += 1
                    elif bit == 0x80: fields['fh'] = d[p]; fl |= d[p] << 8; f |= d[p] << 8; p += 1
                    else: fields[hex(bit)] = d[p]; p += 1
                    f ^= bit
                if not f: break
            pl = d[p:p + ln]
            if len(pl) != ln: continue
            if _crc(d[1:p + ln]) != (((d[p + ln] >> 4) | (d[p + ln] << 4)) & 0xff if len(d) > p + ln else -1):
                continue
            out.append(dict(seq=seq, flags=fl, fields=fields, payload=pl))
        except IndexError:
            pass
    return out

# ---------------------------------------------------------------- the hub
SENSOR_SIZE = {0: 6, 1: 12, 2: 13, 3: 6, 4: 8, 5: 6, 7: 7, 8: 5, 9: 21, 10: 2}
ACCEL, GYRO, MAG_UNCAL, MAG_RAW, MAG, PRESSURE, GESTURE, PROX, TEMPHUM, LIGHT = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9

class HubError(Exception): pass

class Hub:
    def __init__(self, log=None, spi_speed=12000000):
        self.pwr = Pin('gpg0', 2, out=0)
        self.req = Pin('gpg4', 1, out=0)
        self.resp = Pin('gpg4', 2)
        self.host = Pin('gpa1', 7, edge='rising')
        self.spi = Spi(speed=spi_speed)
        self.log = log or (lambda *a: None)
        self.seq = 1; self.rid = 0
        self.rxbuf = b''; self.pk = []
        self.stream = b''; self.frames = []
        self.last_rx = time.time()

    # --- SSI (SPI) layer
    def ssi_tx(self, data):
        fr = bytes([0x58, len(data) & 255, len(data) >> 8]) + data + b'\0\0'
        rx = self.spi.xfer(fr)
        return rx[-2] | (rx[-1] << 8)

    def ssi_rx(self):
        h = self.spi.xfer(b'\x70\0\0'); n = h[1] | (h[2] << 8)
        if n == 0 or n > 0x2000: return b''
        r = self.spi.xfer(b'\x70\0\0' + bytes(n))
        return r[3:3 + n]

    def tx_all(self, data):
        off = 0; tries = 0
        while off < len(data):
            acc = self.ssi_tx(data[off:])
            if acc == 0:
                tries += 1
                if tries > 400: raise HubError('SSI tx stalled')
                time.sleep(0.003); continue
            off += acc

    # --- power
    def power_cycle(self):
        self.req.set(0); self.pwr.set(0); time.sleep(1.0)
        self.pwr.set(1); time.sleep(0.3)
        self.req.set(1); time.sleep(0.01)
        self.spi.xfer(bytes(8))
        self.seq = 1; self.rid = 0; self.rxbuf = b''; self.pk = []; self.stream = b''; self.frames = []

    def power_off(self):
        self.req.set(0); self.pwr.set(0)

    # --- transport layer
    def pump(self, t=0.05):
        t0 = time.time()
        while True:
            d = self.ssi_rx()
            if d:
                self.rxbuf += d; self.last_rx = time.time(); t0 = time.time()
            elif time.time() - t0 >= t: break
        while True:
            i = self.rxbuf.find(b'\xb0\x01')
            if i < 0: break
            frm = self.rxbuf[:i + 2]; self.rxbuf = self.rxbuf[i + 2:]
            self.pk += parse(frm)
        if len(self.rxbuf) > 65536: self.rxbuf = b''

    def send(self, ptype, payload, p4=0):
        self.tx_all(build(self.seq, ptype, payload, p4)); self.seq = (self.seq + 1) & 255

    @staticmethod
    def rpc(rid, body):
        l = len(body)
        h = bytes([rid]) if rid < 0x80 else bytes([(rid >> 8) | 0x80, rid & 255])
        h += bytes([l]) if l < 0x80 else bytes([(l >> 8) | 0x80, l & 255])
        return h + body

    def reliable(self, payload, timeout=1.5):
        rid = self.rid; self.rid = (self.rid + 1) & 255
        for _ in range(5):
            self.send(2, payload, p4=rid)
            t0 = time.time()
            while time.time() - t0 < timeout:
                self.pump(0.01)
                for p in self.pk:
                    if p['fields'].get('0x4') == rid:
                        self.pk.remove(p); return p
        raise HubError('no ack for reliable packet %d' % rid)

    def sync(self, n=1, tries=6):
        self.pk = []
        for _ in range(tries):
            self.tx_all(build(self.seq, 0x300, bytes([0, n]))); self.seq = (self.seq + 1) & 255
            self.pump(0.3)
            for p in self.pk:
                pl = p['payload']
                if len(pl) >= 7 and pl[0] == 1 and pl[1] == n:
                    self.seq = (pl[2] + 1) & 255; self.rid = (pl[4] + 1) & 255
                    self.pk = []; return pl
            n += 1
        raise HubError('transport sync failed')

    # --- boot / firmware download
    @staticmethod
    def blob():
        t = open(PATCH, 'rb').read().decode('latin1')
        m = re.search(r'<Blob=([0-9A-Fa-f\s]+)', t)
        return bytes.fromhex(re.sub(r'\s', '', m.group(1)))

    def boot(self):
        self.log('power cycling hub')
        self.power_cycle()
        self.tx_all(bytes([0x80]) * 0x24); time.sleep(0.1)
        self.sync(1)
        r = self.reliable(self.rpc(0, b''))      # GetVersion
        self.pump(0.3)
        ver = None
        for p in self.pk:
            pl = p['payload']
            if pl[:1] == b'\x03' and len(pl) >= 14: ver = struct.unpack_from('<I', pl, 2)[0]
        self.pk = []
        self.log('ROM asic version %s' % (hex(ver) if ver else '?'))
        b = self.blob(); i = 0
        while i < len(b):
            ty = b[i]; ln = struct.unpack_from('<H', b, i + 1)[0]; pl = b[i + 3:i + 3 + ln]; i += 3 + ln
            if ty != 0xf1: raise HubError('unsupported patch record %x' % ty)
            addr = struct.unpack_from('<I', pl)[0]; data = pl[4:]
            self.reliable(self.rpc(5, struct.pack('<IH', addr, len(data)) + data))
        self.log('patch downloaded')
        self.send(0, self.rpc(0x37, struct.pack('<IIIII', 0x800161, 0, 0, 0, 0)))
        time.sleep(1.5); self.pump(0.5)
        self.pk = []
        self.sync(2)
        self.stream = b''; self.frames = []
        # initialize_mcu equivalent
        cid = self.ssp_read(0x0f, 1)
        if cid != b'U': raise HubError('bad chip id %r' % cid)
        self.ssp_write(0xb6, bytes([1, 0]))        # AP info
        self.ssp_write(0xf1, bytes([7, 7, 7]))     # sensor positions
        self.ssp_write(0xec, bytes([0]))           # glass type
        self.scan = int.from_bytes(self.ssp_read(0xf4, 8) or b'', 'little')
        self.fwrev = int.from_bytes(self.ssp_read(0xf0, 4) or b'', 'little')
        self.log('hub ready, scan mask %#x fw %d' % (self.scan, self.fwrev))

    # --- SSP layer
    def feed(self, t=0.0):
        self.pump(t)
        for p in self.pk:
            pl = p['payload']
            while len(pl) >= 3:
                rid = pl[0]; l = pl[1]; o = 2
                if l & 0x80: l = ((l & 0x7f) << 8) | pl[2]; o = 3
                body = pl[o:o + l]; pl = pl[o + l:]
                if rid == 0x21 and len(body) >= 2:
                    n = struct.unpack_from('<H', body)[0]; self.stream += body[2:2 + n]
        self.pk = []
        while len(self.stream) >= 4:
            hdr, ln = struct.unpack_from('<HH', self.stream); ty = hdr & 3
            if ty in (0, 2):
                if len(self.stream) < 4 + ln: break
                self.frames.append(('data' if ty == 2 else 'resp', hdr, self.stream[4:4 + ln]))
                self.stream = self.stream[4 + ln:]
            else:
                self.frames.append(('req', hdr, ln)); self.stream = self.stream[4:]

    def ssp_tx(self, data):
        self.reliable(self.rpc(0x20, struct.pack('<H', len(data)) + data))

    def ssp_read(self, cmd, length, timeout=1.0):
        self.frames = [f for f in self.frames if f[0] == 'data']
        self.ssp_tx(struct.pack('<BHHI', cmd, length, 0, 0))
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.feed(0.03)
            for f in self.frames:
                if f[0] == 'resp': self.frames.remove(f); return f[2]
        return None

    def ssp_write(self, cmd, payload, timeout=1.0):
        self.ssp_tx(struct.pack('<BHHI', cmd, len(payload), 1, 0))
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.feed(0.03)
            for f in self.frames:
                if f[0] == 'req': self.frames.remove(f); self.ssp_tx(payload); return True
        return False

    def sensor_on(self, sid, interval_ms, latency_ms=0):
        return self.ssp_write(0xa1, bytes([sid]) + struct.pack('<II', interval_ms, latency_ms))

    def sensor_rate(self, sid, interval_ms, latency_ms=0):
        return self.ssp_write(0xa4, bytes([sid]) + struct.pack('<II', interval_ms, latency_ms))

    def sensor_off(self, sid):
        return self.ssp_write(0xa2, bytes([sid]))

    def events(self):
        """Yield decoded events from queued data frames: ('sens', sid, payload) / ('log', text)."""
        out = []
        frames, self.frames = self.frames, [f for f in self.frames if f[0] != 'data']
        for kind, hdr, body in frames:
            if kind != 'data': continue
            i = 0
            while i < len(body):
                ty = body[i]
                if ty == 3 and i + 3 <= len(body):
                    n = struct.unpack_from('<H', body, i + 1)[0]; out.append(('log', body[i + 3:i + 3 + n])); i += 3 + n
                elif ty in (0x37, 0x38) and i + 2 <= len(body):
                    sid = body[i + 1]; n = SENSOR_SIZE.get(sid)
                    if n is None or i + 2 + n + 8 > len(body): break
                    out.append(('sens', sid, body[i + 2:i + 2 + n])); i += 2 + n + 8
                elif ty == 0xa8: i += 9
                else: break
        return out
