#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""s10modemd: userspace modem daemon for the Galaxy S10 Shannon 5000 CP.
 - serves the CP's RFS requests (rfsd.py handlers, SIPC ch 0xf5)
 - talks Samsung IPC (ch 0xeb) and keeps modem state from notifications
 - exposes a Hayes AT interface on a pty (/run/s10modem/at) for a bridge to ModemManager
Run as root after boot-link.sh has brought the link up (boot-link.sh does that and stops before this)."""
import os, sys, time, struct, select, tty, errno, traceback, fcntl, subprocess, socket, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rfsd
from s10cp import Link
from ipc import Ipc
from s10sms import SMSBridge
from imslink import ImsLink
from voice import VoiceBridge
from telephony_policy import TelephonyPolicy

RUN = '/run/s10modem'
LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 's10modemd.log'), 'a', buffering=1)


def log(*a):
    LOG.write('%.3f ' % time.time() + ' '.join(str(x) for x in a) + '\n')


class Modem:
    def __init__(self):
        self.link = Link()
        self.ipc = Ipc(self.link)
        self.rfs_acc = b''
        self.resp = {}
        self.pin = None          # raw SEC_PIN_STATUS byte
        self.reg = {2: None, 3: None}
        self.serving = None
        self.asu = 99
        self.imei = None
        self.version = None
        self.last_mb = 0
        self.net = os.open('/dev/s10modem-net0', os.O_RDWR | os.O_NONBLOCK) if os.path.exists('/dev/s10modem-net0') else None
        self.ps_up = self.ps_down = 0
        self.tuns = {}           # pdp channel (0x0a + cid - 1) -> (fd, ifname) for additional PDNs (IMS, ...)
        # CP mailbox interrupts via /dev/s10modem-evt (s10-cp-boot.ko 'irq_on'); falls back to polling if absent
        self.evt = os.open('/dev/s10modem-evt', os.O_RDONLY | os.O_NONBLOCK) if os.path.exists('/dev/s10modem-evt') else None
        # --- PDP / data (UNTESTED: formats derived from vendor libsec-ril, no SIM was available) ---
        self.pdp = {}            # cid -> dict(state=..., ip=..., dns=[...], gw=..., mask=..., mtu=...)
        self.ps_attached = False
        self.trace = []
        self.cfun = 1
        self.attach_sent = False
        self.telephony = TelephonyPolicy(os.path.dirname(os.path.abspath(__file__)), log)
        self.sms = None
        self.ims_link = ImsLink()
        self.voice = VoiceBridge(self.ims_link)
        try:
            self.sms = SMSBridge(self.ipc.tx, request=self.request, ims=self.ims_link)
        except Exception as e:
            log('SMS store unavailable:', type(e).__name__)
        self._load_state()

    # The CP only *notifies* signal strength (the GET is rejected), so keep the last value across daemon restarts.
    STATE = RUN + '/state.json'

    def _load_state(self):
        try:
            import json
            d = json.load(open(self.STATE))
            self.asu = d.get('asu', 99)
        except Exception:
            pass

    def _save_state(self):
        try:
            import json
            os.makedirs(RUN, exist_ok=True)
            json.dump({'asu': self.asu}, open(self.STATE, 'w'))
        except Exception:
            pass

    def wait_evt(self, timeout):
        """Sleep until a CP interrupt arrives (or timeout); plain sleep when no interrupt device exists."""
        if self.evt is None:
            time.sleep(min(timeout, 0.002))
            return
        r, _, _ = select.select([self.evt], [], [], timeout)
        if r:
            try:
                os.read(self.evt, 4)
            except OSError:
                pass

    # ---- link pump ----
    def pump(self):
        for p in self.link.poll_dl(0xf5):
            self.rfs_acc += p
            while len(self.rfs_acc) >= 4:
                total, = struct.unpack_from('<I', self.rfs_acc)
                if total < 6 or total > 0x80000:
                    log('RFS bad total', total); self.rfs_acc = b''; break
                if len(self.rfs_acc) < total:
                    break
                rfsd.handle(self.link, self.rfs_acc[:total]); self.rfs_acc = self.rfs_acc[total:]
        for main, sub, typ, seq, ack, pl in self.ipc.rx():
            self.on_ipc(main, sub, typ, seq, ack, pl)
        self.pump_data()

    PDN_CH0 = 0x0a                 # rmnet0 <-> pdp channel 0x0a; cid N uses 0x0a + N - 1

    def pump_data(self):
        if self.net is None or os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'NO_PS_DATA')):
            return
        # uplink: rmnet0 and the extra PDN interfaces -> multipdp SBD ring
        for ch, fd in [(self.PDN_CH0, self.net)] + [(c, v[0]) for c, v in self.tuns.items()]:
            for _ in range(64):
                try:
                    pkt = os.read(fd, 65536)
                except (BlockingIOError, OSError):
                    break
                if self.link.send_ps(pkt, ch):
                    self.ps_up += 1
        # downlink: pktproc ring -> interface of the packet's pdp channel
        for chan, pkt in self.link.poll_pktproc():
            fd = self.net if chan == self.PDN_CH0 else (self.tuns.get(chan) or (None,))[0]
            if fd is None:
                log('PS drop ch%#x len %d: no interface' % (chan, len(pkt)))
                continue
            try:
                os.write(fd, pkt)
                self.ps_down += 1
            except OSError as e:
                log('PS drop ch%#x len %d: %s' % (chan, len(pkt), e))

    def setup_pdn_netdev(self, cid, d):
        """Raw-IP TUN interface for an additional PDN (cid >= 2), address and source-based routing.
        Interface pdpN (N = cid) is never made the default route; traffic sourced from the PDN address uses table 100+N."""
        ch = self.PDN_CH0 + cid - 1
        name = 'pdp%d' % cid
        try:
            if ch not in self.tuns:
                fd = os.open('/dev/net/tun', os.O_RDWR | os.O_NONBLOCK)
                fcntl.ioctl(fd, 0x400454ca, struct.pack('16sH', name.encode(), 0x0001 | 0x1000))   # TUNSETIFF: IFF_TUN | IFF_NO_PI
                self.tuns[ch] = (fd, name)
            ip6 = d.get('ip6')
            run = lambda *a: subprocess.run(a, capture_output=True, text=True)
            run('ip', 'link', 'set', name, 'mtu', str(d.get('mtu6') or 1280), 'up')
            if ip6:
                run('ip', '-6', 'addr', 'replace', ip6 + '/64', 'dev', name, 'nodad')
                run('ip', '-6', 'route', 'replace', 'default', 'dev', name, 'table', str(100 + cid))
                run('ip', '-6', 'rule', 'del', 'from', ip6 + '/128', 'lookup', str(100 + cid))
                run('ip', '-6', 'rule', 'add', 'from', ip6 + '/128', 'lookup', str(100 + cid), 'priority', '200')
            log('PDN netdev', name, 'pdp channel %#x' % ch, d.get('ip6'), 'table', 100 + cid)
        except Exception as e:
            log('PDN netdev error cid %d: %r' % (cid, e))

    def on_ipc(self, main, sub, typ, seq, ack, pl):
        if (main, sub, typ) == (1, 7, 3) and getattr(self, 'telephony', None) is not None:
            self.telephony.radio_changed()
        if (main, sub) == (4, 1) and typ in (2, 3) and len(pl) >= 3 and pl[0] == 2:
            # Diagnostic enum only, never message reference, PDU or destination.
            log('SMS final type=%d result=%d' % (typ, int.from_bytes(pl[1:3], 'little')))
        self.trace.append((main, sub, typ, ack, b'' if main in (2, 4, 5, 10) else bytes(pl[:24])))
        del self.trace[:-200]
        if getattr(self, 'sms', None) is not None:
            try:
                self.sms.on_ipc(main, sub, typ, pl, seq=seq, ack=ack)
            except Exception as e:
                # Never log PDU, sender or message content; no ACK on failure.
                log('SMS receive not acknowledged:', type(e).__name__)
        if typ == 2:
            self.resp[ack] = (main, sub, pl)
        key = (main, sub)
        if key == (0x05, 0x01) and pl:
            self.pin = pl[0]
        elif key == (0x08, 0x05) and len(pl) >= 3:
            self.reg[pl[1]] = pl
        elif key == (0x08, 0x03) and pl:
            self.serving = pl
        elif key == (0x07, 0x06) and len(pl) >= 2:
            old, self.asu = self.asu, (pl[1] if pl[1] <= 31 else 99)
            if old != self.asu:
                self._save_state()
        elif key == (0x0d, 0x10) and len(pl) >= 3:      # GPRS call status noti: cid, status (1=connected), cause
            cid = pl[0] % 100 or pl[0]
            self.pdp.setdefault(cid, {})['status'] = pl[1]
            self.pdp[cid]['cause'] = pl[2]
            log('PDP status cid %d: status %#x cause %#x' % (cid, pl[1], pl[2]))
        elif key == (0x0d, 0x09) and len(pl) >= 24:
            # IP configuration noti, layout from IpcProtocol41Data::IpcRxIpConfiguration (offsets = frame - 7):
            # [0] cid, [1..2] flags (bit0 IPv4 addr, bit1/2 IPv4 DNS1/2, bit3 gw, bit4 mask, bit6 IPv6, bit13 MTU, bit14 PCO),
            # [3..6] IPv4, [7..14] IPv4 DNS, [0x23..0x32] IPv6, [0x63..0x82] IPv6 DNS1/2, [0x98..0x9b] MTU (v4 low, v6 high 16 bit)
            log('PDP ipcfg raw', pl.hex(' '))
            import ipaddress
            cid = pl[0] % 100 or pl[0]
            flags = struct.unpack_from('<H', pl, 1)[0]
            d = self.pdp.setdefault(cid, {})
            d['flags'] = flags
            dns = [pl[7:11], pl[11:15]]
            d['dns'] = ['.'.join(str(b) for b in x) for i, x in enumerate(dns) if flags & (2 << i) and any(x)]
            if flags & 1:
                ip = pl[3:7]
                gw = pl[15:19] if flags & 0x08 else ip
                mask = bin(struct.unpack('>I', pl[19:23])[0]).count('1') if flags & 0x10 else 24
                d.update(ip='.'.join(str(b) for b in ip), gw='.'.join(str(b) for b in gw), prefix=mask)
            if flags & 0x40 and len(pl) >= 0x83:
                d['ip6'] = str(ipaddress.IPv6Address(bytes(pl[0x23:0x33])))
                d['dns6'] = [str(ipaddress.IPv6Address(bytes(pl[o:o + 16]))) for o in (0x63, 0x73) if any(pl[o:o + 16])]
                if not d.get('ip'):
                    d['ip'] = d['ip6']          # wait_pdp needs an address; IPv6-only bearer
            if flags & 0x2000 and len(pl) >= 0x9c:
                mtu = struct.unpack_from('<I', pl, 0x98)[0]
                d['mtu4'], d['mtu6'] = mtu & 0xffff, (mtu >> 16) or (mtu & 0xffff)
            log('PDP ipcfg cid %d: %s' % (cid, d))
            if cid >= 2:
                self.setup_pdn_netdev(cid, d)
        elif key == (0x0d, 0x03):
            self.ps_attached = bool(pl and pl[0])
            log('PS state noti', pl.hex(' '))
        elif main in (0x02, 0x04, 0x05, 0x0a) and typ != 2:
            # SIM/security and miscellaneous frames may contain equipment or
            # subscriber identifiers. Keep only the message tag in logs.
            log('IPC noti %02x/%02x t%d <identity/security payload omitted>' % (main, sub, typ))
        elif key != (0x0b, 0x0d) and typ != 2:
            log('IPC noti %02x/%02x t%d %s' % (main, sub, typ, pl[:24].hex(' ')))

    def request(self, main, sub, typ=2, payload=b'', timeout=2.0):
        seq = self.ipc.tx(main, sub, typ, payload)
        t = time.time()
        while time.time() - t < timeout:
            self.pump()
            if seq in self.resp:
                m, s, pl = self.resp.pop(seq)
                return None if m == 0x80 else pl
            self.wait_evt(0.02)
        return None

    # ---- radio power (IPC PWR 01.07.01, EXEC; payload format from IpcTxRadioPower, meaning verified on the device) ----
    def set_radio(self, cfun):
        # Verified 2026-10-25: 02 02 = radio on (CP camps after ~50 s), 01 00 = radio off (no cell search).
        # The CP boots with the radio on. 02 04 is unverified and not used.
        pay = {1: b'\x02\x02', 4: b'\x01\x00', 0: b'\x01\x00'}.get(cfun)
        if pay is None:
            return False
        if getattr(self, 'telephony', None) is not None:
            self.telephony.radio_changed()
        self.ipc.tx(0x01, 0x07, 1, pay)         # answered by a PWR noti, not a type-2 response
        t = time.time()
        while time.time() - t < 1.0:
            self.pump(); self.wait_evt(0.05)
        self.cfun = cfun
        return True

    # ---- PDP context (UNTESTED) ----
    def define_pdp(self, cid, apn, proto=2):
        """GPRS 0d.01.03 'define PDP context' - 149 byte message, layout from IpcProtocol41Data::IpcTxDefinePdpContext."""
        body = bytearray(149 - 7)
        body[0] = 1                     # msg[7]
        body[1] = cid                   # msg[8]
        body[2] = proto                 # msg[9]  (2 = IPv4 in the vendor mapping, other codes guessed)
        a = apn.encode()[:100]
        body[3:3 + len(a)] = a          # msg[10..110] APN, NUL padded
        self.ipc.tx(0x0d, 0x01, 3, bytes(body))  # result arrives by notification

    def activate_pdp(self, cid, user=b'', pw=b'', auth=0):
        """GPRS 0d.04.03 'set PDP context' (activate) - 248 byte message, layout from IpcTxSetPdpContext."""
        body = bytearray(248 - 7)
        body[0] = 1                     # msg[7] = PdpActDeactState activate (with credentials)
        body[1] = cid                   # msg[8]
        flags = 0
        if user:
            flags |= 1; body[6:6 + len(user[:100])] = user[:100]            # msg[13..113]
        if pw:
            flags |= 2; body[107:107 + len(pw[:100])] = pw[:100]            # msg[114..214]
        if auth in (1, 2, 3):
            flags |= 0x10; body[0xf5 - 7] = {1: 1, 2: 2, 3: 1}[auth]
        struct.pack_into('<I', body, 2, flags)                              # msg[9..12]
        self.ipc.tx(0x0d, 0x04, 3, bytes(body))  # no synchronous response

    def deactivate_pdp(self, cid):
        body = bytearray(248 - 7)
        body[0] = 0
        body[1] = cid
        self.ipc.tx(0x0d, 0x04, 3, bytes(body))

    def ps_attach(self, attach=True):
        """GPRS 0d.03.03 PS attach/detach (payload: attach, 1, reason)."""
        return self.request(0x0d, 0x03, 3, bytes([1 if attach else 0, 1, 0]))

    def wait_pdp(self, cid, timeout=40.0):
        t = time.time()
        while time.time() - t < timeout:
            self.pump()
            d = self.pdp.get(cid, {})
            if d.get('status') == 1 and d.get('ip'):
                return True
            if d.get('status') not in (None, 1, 0xa, 0xb) and 'ip' not in d:
                return False
            self.wait_evt(0.05)
        return False

    def wait_pdp_down(self, cid, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            if self.pdp.get(cid, {}).get('status') not in (1, 0xa, 0xb):
                return True
            self.wait_evt(0.05)
        return False

    # ---- state refresh ----
    def scan_networks(self, timeout=170.0):
        """Manual network search (IPC NET 08.04.02). Takes up to minutes; the reply is a type-2 response."""
        seq = self.ipc.tx(0x08, 0x04, 2)
        t = time.time()
        while time.time() - t < timeout:
            self.pump()
            if seq in self.resp:
                m_, s_, pl = self.resp.pop(seq)
                log('PLMN list raw', pl.hex(' '))
                return None if m_ == 0x80 else parse_plmn_list(pl)
            self.wait_evt(0.05)
        return None

    def refresh_static(self):
        pl = self.request(0x0a, 0x03, 2, b'\x01')
        if pl and len(pl) > 2:
            self.imei = pl[2:17].decode('latin1')
        pl = self.request(0x0a, 0x01, 2, b'\xff')
        if pl:
            self.version = pl[1:].split(b'\0')[0].decode('latin1')
        self.ipc.tx(0x07, 0x06, 2, b'\x01')      # ask for a fresh RSSI notification
        t = time.time()
        while time.time() - t < 2.0 and self.asu == 99:
            self.pump(); self.wait_evt(0.05)
        self.refresh_dynamic()

    # ---- LTE attach profile (IPC 0d.14.03; layout from IpcProtocol41Data::IpcTxSetLteAttachProfile) ----
    # The Android RIL sends this after radio power-up (DataCallManager::OnRadioPowerUp -> SetAttachApn).
    # Without it the CP stays in PS state 07 on LTE; with IPv4v6 + the operator APN the combined attach
    # succeeds (CS 02 on LTE, verified 2026-10-25 with Telekom). The CP sends no RESP for this SET.
    PDP_TYPE = {'IP': 2, 'IPV4': 2, 'IPV6': 5, 'IPV4V6': 6}

    def attach_apn(self):
        """(apn, pdp_type) from ./attach.apn ('apn,IPV4V6'), or None."""
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'attach.apn')
        try:
            apn, proto = (open(f).read().strip().split(',') + ['IPV4V6'])[:2]
        except OSError:
            return None
        return apn.strip(), self.PDP_TYPE.get(proto.strip().upper(), 6)

    def send_attach_profile(self):
        cfg = self.attach_apn()
        if not cfg or len(cfg[0]) > 100:
            return False
        apn, ptype = cfg
        f = bytearray(0x151)                  # whole frame; the 7-byte header is added by ipc.tx
        f[7], f[8], f[9] = 1, 1, ptype        # type, profile id, PDP type
        f[0x0a:0x0a + len(apn)] = apn.encode()
        f[0x14e] = 0                          # auth: none
        f[0x14f] = ptype                      # roaming PDP type
        self.ipc.tx(0x0d, 0x14, 3, bytes(f[7:]))
        log('LTE attach profile sent: apn %s type %d' % (apn, ptype))
        return True

    def refresh_dynamic(self):
        pl = self.request(0x05, 0x01)
        if pl: self.pin = pl[0]
        for dom in (2, 3):
            pl = self.request(0x08, 0x05, 2, bytes([0xff, dom]))
            if pl: self.reg[dom] = pl
        pl = self.request(0x08, 0x03)
        if pl: self.serving = pl
        if getattr(self, 'telephony', None) is not None:
            self.telephony.consider(self)
        if not self.attach_sent and self.sim_present and self.imsi_ready():
            self.attach_sent = self.send_attach_profile()

    # ---- SIM PIN (IPC 05.01.03; format from IpcTxSetPinStatus, UNTESTED against a real PIN) ----
    def pin_attempts(self):
        """(remaining, max) for PIN1 from IPC 05.08.02, or None."""
        pl = self.request(0x05, 0x08, 2, bytes([1, 3]) + bytes(7))
        if pl and len(pl) >= 4 and pl[1] == 3:
            return pl[2], pl[3]
        return None

    def imsi_ready(self):
        pl = self.request(0x0a, 0x02, 2)
        return bool(pl and any(pl))

    def get_imsi(self):
        # Never log the value. Accept only an unambiguous ASCII response;
        # unknown encodings remain unsupported instead of inventing an ID.
        pl = self.request(0x0a, 0x02, 2)
        if not pl:
            return None
        candidates = [pl.rstrip(b'\0')]
        if pl[0] <= 15 and pl[0] == len(pl[1:].rstrip(b'\0')):
            candidates.append(pl[1:].rstrip(b'\0'))
        for value in candidates:
            if 5 <= len(value) <= 15 and all(48 <= b <= 57 for b in value) and any(b != 48 for b in value):
                return value.decode('ascii')
        return None

    def enter_pin(self, pin):
        """Returns 0 on success, otherwise a +CME ERROR code."""
        if not (4 <= len(pin) <= 8 and pin.isdigit()):
            return 16
        att = self.pin_attempts()
        log('PIN entry requested, remaining attempts', att)
        # 05.08.02 does not report remaining attempts as assumed (field stayed 1 after the counter was reset); log only.
        pay = bytes([0x03, len(pin), 0]) + pin.encode().ljust(8, b'\0') + bytes(8)
        seq = self.ipc.tx(0x05, 0x01, 3, pay)
        t = time.time()
        res = None
        while time.time() - t < 15:
            self.pump()
            if seq in self.resp:
                res = self.resp.pop(seq)
                break
            self.wait_evt(0.05)
        log('PIN response', None if res is None else (hex(res[0]), hex(res[1]), res[2].hex(' ')))
        # 0x80 frame = error reply; its code byte is 0 when the PIN was accepted (seen 2026-10-25), 1 for real errors
        if res is None or (res[0] == 0x80 and res[2][3:4] != b'\x00'):
            return 16
        t = time.time()
        while time.time() - t < 10 and not self.imsi_ready():
            self.wait_evt(0.5)
        self.refresh_dynamic()
        return 0

    @property
    def sim_present(self):
        return self.pin is not None and self.pin != 0x80

    def plmn(self):
        if not self.serving:
            return None
        d = b''
        for c in self.serving[3:9]:
            if 0x30 <= c <= 0x39: d += bytes([c])
            else: break
        return d.decode() if len(d) >= 5 else None

    # NET_REGIST reply payload: [?, domain, state, ...]; state codes as in Replicant/libsamsung-ipc
    # (1 none, 2 home, 3 searching, 4 emergency only, 5 unknown, 6 roaming) -> 3GPP +CREG stat.
    # Consistent with the live data without SIM: voice=4 (emergency camp), data=1 (none).
    # Mapping verified only for these two values; the rest (home/roaming/searching) is UNTESTED.
    REG_MAP = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5,
               7: 4, 8: 0, 9: 3, 10: 4, 0x20: 1, 0x22: 1}

    def reg_stat(self, dom):
        pl = self.reg.get(dom)
        if not pl or len(pl) < 3:
            return 0
        return self.REG_MAP.get(pl[2], 0)

    def emergency_only(self):
        pl = self.reg.get(2)
        return bool(pl and len(pl) > 2 and pl[2] in (7, 10))


# modem RAT code (libsec-ril ConvertIpcToRadioTech @0x33d6c8) -> 3GPP AcT of +COPS (0 GSM, 2 UTRAN, 3 EDGE, 4 HSDPA, 7 E-UTRAN, 12 NR)
RAT_ACT = {1: 0, 2: 0, 3: 3, 4: 2, 5: 2, 0x16: 4, 0x17: 4, 0x18: 4, 0x21: 7, 0x22: 2, 0x24: 12}
PLMN_STAT = {2: 1, 3: 2, 4: 3}      # NET_PLMN list status -> +COPS stat (1 available, 2 current, 3 forbidden)


def parse_plmn_list(pl):
    """NET_PLMN_LIST reply payload: [count], 10-byte entries {status, plmn[6] ('#' padded), rat, 2 more}."""
    out = []
    n = pl[0] if pl else 0
    for i in range(n):
        e = pl[1 + 10 * i: 11 + 10 * i]
        if len(e) < 8:
            break
        plmn = e[1:7].split(b'#')[0].decode('latin1')
        out.append((PLMN_STAT.get(e[0], 0), plmn, RAT_ACT.get(e[7], 2)))
    return out


class AT:
    def __init__(self, m):
        self.m = m
        self.echo = True
        self.cmee = 0
        self.creg = {'CREG': 0, 'CGREG': 0, 'CEREG': 0}
        self.cops_fmt = 2
        self.cfun = 1
        self.pdp = {}
        self.buf = b''
        # prefer the kernel tty bridge (/dev/ttyCP0, for ModemManager); fall back to a pty
        self.misc = os.path.exists('/dev/s10modem-at0')
        os.makedirs(RUN, exist_ok=True)
        link = RUN + '/at'
        try: os.unlink(link)
        except OSError: pass
        if self.misc:
            self.master = os.open('/dev/s10modem-at0', os.O_RDWR | os.O_NONBLOCK)
            self.slave_name = '/dev/ttyCP0'
        else:
            self.master, slave = os.openpty()
            tty.setraw(self.master); tty.setraw(slave)
            self.slave_name = os.ttyname(slave)
            self.slave = slave   # keep open so the pty never hangs up
            os.chmod(self.slave_name, 0o666)
        os.symlink(self.slave_name, link)
        log('AT on', self.slave_name, '->', link)

    def out(self, s):
        os.write(self.master, s.encode('latin1'))

    def ok(self): self.out('\r\nOK\r\n')

    def err(self, cme=None):
        if cme is not None and self.cmee == 1:
            self.out('\r\n+CME ERROR: %d\r\n' % cme)
        elif cme is not None and self.cmee == 2:
            self.out('\r\n+CME ERROR: %s\r\n' % {10: 'SIM not inserted', 16: 'incorrect password', 30: 'no network service', 3: 'operation not allowed', 4: 'operation not supported'}.get(cme, 'unknown'))
        else:
            self.out('\r\nERROR\r\n')

    def lines(self, ls):
        self.out(''.join('\r\n' + l for l in ls) + '\r\n\r\nOK\r\n')

    def feed(self, data):
        for ch in data:
            sms = getattr(self.m, 'sms', None)
            if sms is not None and sms.input_byte(ch, self):
                continue
            if ch in (13, 10):
                if self.echo and ch == 13: os.write(self.master, b'\r')
                line, self.buf = self.buf.decode('latin1').strip(), b''
                if line:
                    self.handle(line)
            else:
                self.buf += bytes([ch])
                if self.echo: os.write(self.master, bytes([ch]))

    def handle(self, line):
        private = line.upper().startswith(('AT+CPIN=', 'ATD', 'AT+CMGS', 'AT+CMGW'))
        log('AT<', '<private command omitted>' if private else line)
        m = self.m
        u = line.upper()
        if not u.startswith('AT'):
            return self.err()
        c = u[2:]
        try:
            sms = getattr(m, 'sms', None)
            if sms is not None and sms.handle(c, self):
                return
            if m.voice.handle(c, self):
                return
            if c in ('', 'Z', '&F', 'E1', 'V1', 'Q0', 'X4', '&C1', '&D2', 'S0=0'):
                if c == 'E1': self.echo = True
                return self.ok()
            if c == 'E0': self.echo = False; return self.ok()
            if c in ('I', 'I0', 'I1'):
                return self.lines(['Manufacturer: Samsung', 'Model: SM-G973F', 'Revision: %s' % (m.version or '?'), 'IMEI: %s' % (m.imei or '')])
            if c in ('+CGMI', '+GMI'): return self.lines(['Samsung'])
            if c in ('+CGMM', '+GMM'): return self.lines(['SM-G973F'])
            if c in ('+CGMR', '+GMR'): return self.lines([m.version or 'unknown'])
            if c in ('+CGSN', '+GSN'): return self.lines([m.imei or ''])
            if c == '+GCAP': return self.lines(['+GCAP: +CGSM,+DS'])
            if c.endswith('=?') and c != '+COPS=?':
                tests = {'+CREG': '+CREG: (0-2)', '+CGREG': '+CGREG: (0-2)', '+CEREG': '+CEREG: (0-2)',
                         '+CMEE': '+CMEE: (0-2)', '+CFUN': '+CFUN: (0,1,4)', '+CSCS': '+CSCS: ("IRA","GSM","UCS2")',
                         '+CGDCONT': '+CGDCONT: (1-1),"IP",,,(0-2),(0-4)\r\n+CGDCONT: (1-1),"IPV6",,,(0-2),(0-4)\r\n+CGDCONT: (1-1),"IPV4V6",,,(0-2),(0-4)',
                         '+WS46': '+WS46: (12,22,25,28,29,30,31)', '+CLCK': '+CLCK: ("SC","FD","PN","PU","PF","PC","AO","OI","OX","AI","IR","AB","AG","AC")',
                         '+CGATT': '+CGATT: (0,1)'}
                if c[:-2] in tests: return self.lines([tests[c[:-2]]])
                return self.err()
            if c.startswith('+S10IPC='):   # debug: AT+S10IPC=main,sub,type[,hexpayload] -> raw IPC response
                a = c[8:].split(',')
                n0 = len(m.trace)
                pl = m.request(int(a[0], 16), int(a[1], 16), int(a[2], 16), bytes.fromhex(a[3]) if len(a) > 3 else b'')
                extra = ['  frame %02x/%02x t%d ack%d %s' % (f[0], f[1], f[2], f[3], f[4].hex(' ')) for f in m.trace[n0:]]
                return self.lines(['+S10IPC: ' + (pl.hex(' ') if pl is not None else 'error')] + extra)
            if c in ('H', 'H0'): return self.ok()
            if c == '+WS46?': return self.lines(['+WS46: 25'])
            if c.startswith('+CMEE='):
                self.cmee = int(c[6:]); return self.ok()
            if c == '+CMEE?': return self.lines(['+CMEE: %d' % self.cmee])
            if c == '+CPIN?':
                m.refresh_dynamic()
                if not m.sim_present: return self.err(10)
                return self.lines(['+CPIN: READY' if m.imsi_ready() else '+CPIN: SIM PIN'])
            if c.startswith('+CPIN=') and c != '+CPIN=?':
                a = line[line.index('=') + 1:].split(',')
                if len(a) != 1: return self.err(3)      # PUK entry is not implemented on purpose
                rc = m.enter_pin(a[0].strip('"'))
                return self.ok() if rc == 0 else self.err(rc)
            if c == '+CIMI':
                value = m.get_imsi()
                return self.lines([value]) if value else self.err(4)
            if c in ('+CCID', '+ICCID', '+CRSM'):
                return self.err(4)  # unsupported, not proof of an absent SIM
            if c == '+CFUN?': return self.lines(['+CFUN: %d' % m.cfun])
            if c.startswith('+CFUN='):
                v = int(c[6:].split(',')[0])
                if v == 1 or v in (0, 4):
                    m.set_radio(v)
                    return self.ok()
                return self.err(4)
            for nm in ('CREG', 'CGREG', 'CEREG'):
                if c == '+%s?' % nm:
                    m.refresh_dynamic()
                    stat = m.reg_stat(2 if nm == 'CREG' else 3)
                    return self.lines(['+%s: %d,%d' % (nm, self.creg[nm], stat)])
                if c.startswith('+%s=' % nm):
                    self.creg[nm] = int(c.split('=')[1].split(',')[0]); return self.ok()
            if c == '+CSQ':
                return self.lines(['+CSQ: %d,99' % m.asu])
            if c == '+COPS?':
                m.refresh_dynamic()
                p = m.plmn()
                if p and m.sim_present: return self.lines(['+COPS: 0,%d,"%s",7' % (self.cops_fmt, p)])
                return self.lines(['+COPS: 0'])
            if c.startswith('+COPS=') and c != '+COPS=?':
                a = c[6:].split(',')
                if a[0] == '3' and len(a) > 1: self.cops_fmt = int(a[1])
                return self.ok()
            if c == '+COPS=?':
                if not m.sim_present and False:
                    return self.err(10)
                lst = m.scan_networks()
                if lst is None: return self.err(30)
                return self.lines(['+COPS: ' + ','.join('(%d,"%s","%s","%s",%d)' % (st, p, p, p, act) for st, p, act in lst) + ',,(0-4),(0-2)'])
            if c.startswith('+CGDCONT='):
                a = line[line.index('=') + 1:].split(',')
                if int(a[0]) != 1: return self.err(4)  # one rmnet data channel
                self.pdp[int(a[0])] = a[1:]; return self.ok()
            if c == '+CGDCONT?':
                return self.lines(['+CGDCONT: %d,%s' % (k, ','.join(v)) for k, v in sorted(self.pdp.items())] or [])
            if c == '+CGATT?': return self.lines(['+CGATT: %d' % (1 if m.ps_attached else 0)])
            if c.startswith('+CGATT='):
                if not m.sim_present: return self.err(10)
                m.ps_attach(c[7:8] == '1'); return self.ok()
            if c == '+CGACT?':
                return self.lines(['+CGACT: %d,%d' % (cid, 1 if m.pdp.get(cid, {}).get('status') == 1 else 0) for cid in sorted(self.pdp)])
            if c.startswith('+CGACT='):          # UNTESTED
                a = c[7:].split(',')
                if not m.sim_present: return self.err(10)
                cids = [int(x) for x in a[1:]] or sorted(self.pdp)
                if any(cid != 1 for cid in cids): return self.err(4)
                for cid in cids:
                    if a[0] == '1':
                        ctx = self.pdp.get(cid) or ['', '']
                        ptype = m.PDP_TYPE.get(ctx[0].strip('"').upper(), 2)
                        apn = ctx[1].strip('"') if len(ctx) > 1 else ''
                        m.pdp.pop(cid, None)
                        # The CP sends no RESP for these SETs (verified 2026-10-25); the result arrives as
                        # GPRS call status (0d/10) and IP configuration (0d/09) notifications.
                        m.define_pdp(cid, apn, ptype)
                        m.activate_pdp(cid)
                        if not m.wait_pdp(cid): return self.err(30)
                    else:
                        m.deactivate_pdp(cid)
                        if not m.wait_pdp_down(cid): return self.err(30)
                        m.pdp.pop(cid, None)
                return self.ok()
            if c.startswith('+CGPADDR'):         # UNTESTED
                cid = int(c.split('=')[1]) if '=' in c else 1
                ip = m.pdp.get(cid, {}).get('ip')
                return self.lines(['+CGPADDR: %d,"%s"' % (cid, ip)] if ip else [])
            if c.startswith('+S10IPCFG='):
                cid = int(c.split('=', 1)[1])
                d = m.pdp.get(cid, {})
                if d.get('status') != 1: return self.err(30)
                result = []
                if d.get('flags', 0) & 1 and d.get('ip'):
                    dns = d.get('dns', []) + ['', '']
                    result.append('+S10IPCFG: 4,%s,%d,%s,%s,%s,%d' %
                                  (d['ip'], d.get('prefix', 32), d.get('gw', ''),
                                   dns[0], dns[1], d.get('mtu4') or 1500))
                if d.get('ip6'):
                    dns = d.get('dns6', []) + ['', '']
                    result.append('+S10IPCFG: 6,%s,64,,%s,%s,%d' %
                                  (d['ip6'], dns[0], dns[1], d.get('mtu6') or 1500))
                return self.lines(result) if result else self.err(30)
            if c.startswith('+CGCONTRDP'):       # UNTESTED
                cid = int(c.split('=')[1]) if '=' in c else 1
                d = m.pdp.get(cid, {})
                if not d.get('ip'): return self.err(30)
                apn = self.pdp.get(cid, ['', ''])[1].strip('"') if cid in self.pdp else ''
                is_v6 = bool(d.get('ip6')) and not d.get('flags', 0) & 1
                dns = d.get('dns6' if is_v6 else 'dns', []) + ['', '']
                return self.lines(['+CGCONTRDP: %d,0,"%s","%s/%d","%s","%s","%s"' %
                                   (cid, apn, d['ip'], 64 if is_v6 else d.get('prefix', 32),
                                    '' if is_v6 else d.get('gw', ''), dns[0], dns[1])])
            if c.startswith(('+CSCS=', '+CLIP=', '+CRC=', '+CR=', '+CGATT=', '+CLIR=', '+CMER=')):
                return self.ok()
            if c == '+CGSMS?': return self.lines(['+CGSMS: 3'])
            return self.err()
        except Exception:
            log('AT error', traceback.format_exc())
            return self.err()


CTL_ALLOWED = {(0x0a, 0x02, 2): 2.0, (0x05, 0x09, 2): 8.0}     # IMSI read, USIM AKA (VoWiFi/IMS clients)


def ctl_open():
    path = RUN + '/ctl.sock'
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(4)
    srv.setblocking(False)
    return srv


def ctl_serve(m, srv):
    """One JSON request per connection: {"op":"ipc","main":..,"sub":..,"typ":..,"payload":hex} or {"op":"imei"}."""
    try:
        c, _ = srv.accept()
    except BlockingIOError:
        return
    try:
        c.settimeout(2)
        req = json.loads(c.recv(4096))
        if req.get('op') == 'imei':
            reply = {'ok': bool(m.imei), 'imei': m.imei}
        elif req.get('op') == 'ipc' and (req['main'], req['sub'], req['typ']) in CTL_ALLOWED:
            pl = m.request(req['main'], req['sub'], req['typ'], bytes.fromhex(req.get('payload', '')),
                           timeout=CTL_ALLOWED[(req['main'], req['sub'], req['typ'])])
            reply = {'ok': pl is not None, 'payload': pl.hex() if pl is not None else ''}
        else:
            reply = {'ok': False}
        c.sendall(json.dumps(reply).encode())
    except Exception as e:
        log('ctl error', type(e).__name__)
    finally:
        c.close()


def main():
    m = Modem()
    log('s10modemd start')
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'NO_RADIO_ON')):
        log('NO_RADIO_ON present: not sending radio power on')   # test: leave the CP radio state alone
    else:
        m.set_radio(1)              # the CP boots with the radio off; the Android RIL turns it on as well
    m.refresh_static()
    log('imei ok' if m.imei else 'imei missing', 'ver', m.version, 'pin', m.pin, 'plmn', m.plmn())
    at = AT(m)
    ctl = ctl_open()
    last = 0
    while True:
        m.pump()
        m.telephony.consider(m)
        m.voice.flush(at)
        if m.sms is not None:
            try:
                m.sms.poll_ims()
                m.sms.flush(at)
            except OSError:
                pass  # retain CMTI for next loop if no AT reader yet
        fds = [at.master, ctl] + ([m.net] if m.net is not None else []) + ([m.evt] if m.evt is not None else [])
        # with interrupts we can sleep long; without, poll every 5 ms like before
        r, _, _ = select.select(fds, [], [], 0.5 if m.evt is not None else 0.005)
        if m.evt is not None and m.evt in r:
            try:
                os.read(m.evt, 4)
            except OSError:
                pass
        if ctl in r:
            ctl_serve(m, ctl)
        if time.time() - last > 30:
            last = time.time()
            m.refresh_dynamic()
        if at.master not in r:
            continue
        try:
            data = os.read(at.master, 4096)
        except OSError as e:
            if e.errno == errno.EIO:
                time.sleep(0.1); continue
            raise
        at.feed(data)


if __name__ == '__main__':
    main()
