#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""imsd: IMS registration (IMS-AKA, 3GPP TS 33.203 IPsec inside the ePDG tunnel) and SMS over IP (TS 24.341/24.011).

Runs inside the network namespace of the SWu tunnel:  ip netns exec ims python3 imsd.py LOGFILE
  LOGFILE   swu_emulator output (read for the P-CSCF list); our address is taken from tun1.
Local interface: unix stream socket /run/s10modem/ims.sock, one JSON object per line.
  client -> imsd  {"op":"send","id":N,"smsc":"<hex SMSC address field incl. length byte>","tpdu":"<hex SMS-SUBMIT>"}
  imsd -> client  {"op":"sent","id":N,"ok":true|false,"detail":"..."}      final RP result for a send
  imsd -> client  {"op":"deliver","id":M,"smsc":"<hex>","tpdu":"<hex SMS-DELIVER>"}   incoming SMS
  client -> imsd  {"op":"ack","id":M,"ok":true|false}                       RP-ACK / RP-ERROR is sent after this
  imsd -> client  {"op":"status","registered":bool}                         on connect and on change
SMS contents are never logged."""
import base64, collections, hashlib, ipaddress, json, os, random, re, select, socket, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usim_cp
from ims_call import Calls
from sip_tcp import SipConnection, TcpPeer

UC, US = 5062, 5063
SOCK = '/run/s10modem/ims.sock'
LOG = '/run/s10modem/imsd.log'
ALLOW = 'INVITE, ACK, CANCEL, BYE, UPDATE, PRACK, MESSAGE, OPTIONS, NOTIFY'
ACCEPT = 'application/sdp, application/vnd.3gpp.sms, application/reginfo+xml'
METHODS = set(ALLOW.split(', ')) | {'REGISTER', 'SUBSCRIBE', 'INFO'}


def log(*a):
    line = time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True)


def md5h(b):
    return hashlib.md5(b if isinstance(b, bytes) else b.encode()).hexdigest()


def parse(data):
    head, _, body = data.partition('\r\n\r\n')
    lines = head.split('\r\n')
    hdr = {}
    unfolded = []
    for ln in lines[1:]:
        if ln.startswith((' ', '\t')) and unfolded:
            unfolded[-1] += ' ' + ln.strip()
        else:
            unfolded.append(ln)
    compact = {'v': 'via', 'f': 'from', 't': 'to', 'i': 'call-id', 'm': 'contact',
               'l': 'content-length', 'c': 'content-type', 'k': 'supported'}
    for ln in unfolded:
        if ':' in ln:
            k, v = ln.split(':', 1)
            k = k.strip().lower()
            hdr.setdefault(compact.get(k, k), []).append(v.strip())
    return lines[0], hdr, body


def header_values(values):
    """Split SIP comma lists without splitting quoted feature tags/URIs."""
    for value in values:
        start, angle, quoted, escaped = 0, 0, False, False
        for n, char in enumerate(value):
            if escaped:
                escaped = False
            elif quoted and char == '\\':
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted:
                if char == '<':
                    angle += 1
                elif char == '>':
                    angle = max(0, angle - 1)
                elif char == ',' and angle == 0:
                    yield value[start:n].strip()
                    start = n + 1
        yield value[start:].strip()


def binding_summary(hdr, src, port):
    """No URI/identity output; a 200 alone is not an active Contact binding."""
    result = {'contacts': 0, 'own_active': False, 'own_expires': 0,
              'own_mmtel': False, 'own_audio': False, 'own_smsip': False}
    endpoint = re.compile(r'\[' + re.escape(src) + r'\]:' + str(port) + r'(?:[;>]|$)', re.I)
    for contact in header_values(hdr.get('contact', [])):
        result['contacts'] += 1
        uri = re.search(r'<([^>]+)>', contact)
        if not uri or not endpoint.search(uri[1]):
            continue
        expiry = re.search(r'(?:^|;)\s*expires\s*=\s*"?(\d+)', contact[uri.end():], re.I)
        if not expiry or int(expiry[1]) <= 0:
            continue
        result.update(own_active=True, own_expires=int(expiry[1]),
                      own_mmtel='3gpp-service.ims.icsi.mmtel' in contact,
                      own_audio=bool(re.search(r';\s*audio(?:[;=\s]|$)', contact)),
                      own_smsip='+g.3gpp.smsip' in contact)
    return result


def transaction_key(method, hdr):
    return hashlib.sha256(repr((method, hdr.get('via'), hdr.get('call-id'), hdr.get('cseq'))).encode()).digest()


def sh(*a):
    r = subprocess.run(a, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError('%s: %s' % (' '.join(a), r.stderr.strip()))


def tag():
    return '%08x' % random.getrandbits(32)


def bcd_encode(digits):
    digits = digits + ('F' if len(digits) % 2 else '')
    return bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, len(digits), 2))


def bcd_decode(b):
    out = ''
    for x in b:
        out += '%X%X' % (x & 15, x >> 4)
    return out.rstrip('F')


class Ims:
    def __init__(self, src, pcscfs):
        self.src, self.pcscfs = src, pcscfs
        self.imsi = usim_cp.imsi()
        self.imei = usim_cp.imei()
        self.pani = self.access_info()
        self.domain = 'ims.mnc%03d.mcc%s.3gppnetwork.org' % (int(self.imsi[3:5]), self.imsi[:3])
        self.impi = '%s@%s' % (self.imsi, self.domain)
        self.impu = 'sip:' + self.impi
        self.call_id = '%016x' % random.getrandbits(64)
        self.tag = tag()
        self.cseq = 0
        self.uc, self.us = 5062, 5063
        self.registered = False
        self.expires_at = 0
        self.sc = self.ss = None
        self.ts = None
        self.tcp_connections = {}
        self.pcscf = None
        self.service_route = None
        self.pref_id = None
        self.rp_mr = random.randrange(256)
        self.pending = {}     # branch -> (kind, payload)
        self.rp_wait = {}     # rp_mr -> client request id
        self.deliveries = {}  # id -> (peer uri, rp mr)
        self.next_delivery = 1
        self.response_cache = collections.OrderedDict()

    @staticmethod
    def access_info():
        bssid = ''
        try:
            out = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,BSSID', 'dev', 'wifi'], capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if ln.startswith('yes:'):
                    bssid = ln[4:].replace('\\:', '').replace(':', '').lower()
        except Exception:
            pass
        return 'IEEE-802.11; country=DE; i-wlan-node-id=%s' % (bssid if len(bssid) == 12 else '021a2b3c4d5e')

    # ------------------------------------------------------------ registration
    def contact(self, port=None):
        port = port or self.us
        return ('<sip:%s@[%s]:%d>;+sip.instance="<urn:gsma:imei:%s-%s-%s>";+g.3gpp.icsi-ref="urn%%3Aurn-7%%3A3gpp-service.ims.icsi.mmtel";+g.3gpp.mmtel;+g.3gpp.smsip;+g.3gpp.accesstype="wlan";audio'
                % (self.imsi, self.src, port, self.imei[:8], self.imei[8:14], self.imei[14:] or '0'))

    def register_msg(self, via_port, auth, sec_client, sec_verify=None, expires=600000):
        self.cseq += 1
        lines = ['REGISTER sip:%s SIP/2.0' % self.domain,
                 'Via: SIP/2.0/UDP [%s]:%d;branch=z9hG4bK%08x;rport' % (self.src, via_port, random.getrandbits(32)),
                 'Max-Forwards: 70', 'From: <%s>;tag=%s' % (self.impu, self.tag), 'To: <%s>' % self.impu,
                 'Call-ID: ' + self.call_id, 'CSeq: %d REGISTER' % self.cseq, 'Contact: ' + self.contact(via_port),
                 'Expires: %d' % expires, 'Supported: path, sec-agree', 'Require: sec-agree', 'Proxy-Require: sec-agree',
                 'Allow: ' + ALLOW,
                 'Security-Client: ' + sec_client,
                 'P-Access-Network-Info: ' + self.pani,
                 'Authorization: ' + auth, 'User-Agent: SM-G973F-G973FXXSGHWC2 Samsung IMS 6.0 DBT']
        if sec_verify:
            lines.append('Security-Verify: ' + sec_verify)
        return '\r\n'.join(lines) + '\r\nContent-Length: 0\r\n\r\n'

    def cleanup_sas(self):
        for c in (('ip', 'xfrm', 'state', 'flush'), ('ip', 'xfrm', 'policy', 'flush')):
            subprocess.run(c, capture_output=True)
        for s in (self.sc, self.ss):
            if s:
                s.close()
        self.sc = self.ss = None
        if self.ts:
            self.ts.close()
        self.ts = None
        for connection in self.tcp_connections.values():
            connection.close()
        self.tcp_connections.clear()

    def install_sas(self, sa, key):
        for n, (a, b, sp, dp, spi, direction) in enumerate(sa):
            sh('ip', 'xfrm', 'state', 'add', 'src', a, 'dst', b, 'proto', 'esp', 'spi', '0x%x' % spi, 'reqid', str(10 + n),
               'mode', 'transport', 'auth-trunc', 'hmac(sha1)', '0x' + key, '96', 'enc', 'ecb(cipher_null)', '',
               'sel', 'src', a, 'dst', b, 'proto', 'any', 'sport', str(sp), 'dport', str(dp))
            for protocol in ('udp', 'tcp'):
                sh('ip', 'xfrm', 'policy', 'add', 'src', a, 'dst', b, 'proto', protocol, 'sport', str(sp), 'dport', str(dp),
                   'dir', direction, 'tmpl', 'src', a, 'dst', b, 'proto', 'esp', 'reqid', str(10 + n), 'mode', 'transport')

    def login(self, pcscf):
        """Full REGISTER cycle. Returns True on 200 OK; leaves SAs and sockets installed."""
        self.cleanup_sas()
        self.pcscf = pcscf
        self.uc = random.randrange(20000, 60000) & ~1
        self.us = self.uc + 1
        spi_c, spi_s = 0x10000 + random.getrandbits(16), 0x20000 + random.getrandbits(16)
        sec_client = ('ipsec-3gpp; alg=hmac-sha-1-96; ealg=null; spi-c=%d; spi-s=%d; port-c=%d; port-s=%d'
                      % (spi_c, spi_s, self.uc, self.us))
        s0 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s0.bind((self.src, 5060))
        s0.settimeout(8)
        try:
            auth = 'Digest username="%s", realm="ims.telekom.de", nonce="", uri="sip:%s", response="", algorithm=AKAv1-MD5' % (self.impi, self.domain)
            for attempt in range(3):
                s0.sendto(self.register_msg(5060, auth, sec_client).encode(), (pcscf, 5060))
                status, hdr, _ = parse(s0.recvfrom(65535)[0].decode('latin1'))
                if '401' not in status:
                    log('REGISTER ->', status)
                    return False
                chal = hdr['www-authenticate'][0]
                nonce_b64 = re.search(r'nonce="([^"]+)"', chal).group(1)
                realm = re.search(r'realm="([^"]+)"', chal).group(1)
                nonce = base64.b64decode(nonce_b64)
                res, ck, ik = usim_cp.res_ck_ik(nonce[:16].hex(), nonce[16:32].hex())
                uri = 'sip:' + self.domain
                if ck is None:
                    log('SQN resync')
                    auth = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="", algorithm=AKAv1-MD5, auts="%s"'
                            % (self.impi, realm, nonce_b64, uri, base64.b64encode(bytes.fromhex(res)).decode()))
                    continue
                break
            else:
                return False
        finally:
            s0.close()
        ss_hdr = hdr['security-server'][0]
        f = dict(re.findall(r'(\w[\w-]*)=([^;,\s]+)', ss_hdr))
        p_spi_c, p_spi_s, p_port_c, p_port_s = int(f['spi-c']), int(f['spi-s']), int(f['port-c']), int(f['port-s'])
        if f.get('ealg', 'null') != 'null' or f.get('alg') != 'hmac-sha-1-96':
            log('unsupported Security-Server'); return False
        key = (bytes.fromhex(ik) + bytes(4)).hex()
        sa = [(self.src, pcscf, self.uc, p_port_s, p_spi_s, 'out'), (pcscf, self.src, p_port_s, self.uc, spi_c, 'in'),
              (pcscf, self.src, p_port_c, self.us, spi_s, 'in'), (self.src, pcscf, self.us, p_port_c, p_spi_c, 'out')]
        self.install_sas(sa, key)
        self.sc = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.sc.bind((self.src, self.uc))
        self.ss = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.ss.bind((self.src, self.us))
        self.ts = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.ts.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ts.bind((self.src, self.us))
        self.ts.listen(4)
        self.ts.setblocking(False)
        self.p_port_s, self.p_port_c = p_port_s, p_port_c
        self.sec_verify = ss_hdr
        cn, nc = '%08x' % random.getrandbits(32), '00000001'
        ha1 = md5h(self.impi.encode() + b':' + realm.encode() + b':' + bytes.fromhex(res))
        resp = md5h('%s:%s:%s:%s:auth:%s' % (ha1, nonce_b64, nc, cn, md5h('REGISTER:' + uri)))
        auth = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", algorithm=AKAv1-MD5, '
                'integrity-protected=yes, qop=auth, nc=%s, cnonce="%s"' % (self.impi, realm, nonce_b64, uri, resp, nc, cn))
        msg = self.register_msg(self.us, auth, sec_client, ss_hdr).encode()
        if os.environ.get('IMSD_DEBUG'):
            log('protected REGISTER prepared; credentials/identities omitted')
        status = None
        delay = 0.5
        for _ in range(7):                       # UDP retransmission, RFC 3261 timer A
            self.sc.sendto(msg, (pcscf, p_port_s))
            ready = select.select([self.sc, self.ss], [], [], delay)[0]
            if ready:
                status, hdr, _ = parse(ready[0].recvfrom(65535)[0].decode('latin1'))
                break
            delay = min(delay * 2, 8)
        if status is None:
            log('protected REGISTER: no answer'); return False
        log('protected REGISTER ->', status)
        if ' 200 ' not in status:
            return False
        self.binding = binding_summary(hdr, self.src, self.us)
        log('registration binding', json.dumps(self.binding, sort_keys=True))
        if not self.binding['own_active']:
            log('REGISTER 200 without our active binding; not registered')
            return False
        self.service_route = (hdr.get('service-route') or [None])[0]
        for v in header_values(hdr.get('p-associated-uri', [])):
            if v.startswith('<sip:'):
                self.pref_id = v[1:].split('>', 1)[0]
                break
        grant = self.binding['own_expires']
        self.expires_at = time.time() + grant * 0.5
        self.registered = True
        log('registered, refresh in %d s' % (grant * 0.5))
        try:
            self.sc.sendto(self.request_msg('SUBSCRIBE', self.pref_id or self.impu, b'', [
                'Event: reg', 'Expires: 600000', 'Accept: application/reginfo+xml', 'Allow-Events: reg, dialog, conference']), (pcscf, p_port_s))
            log('SUBSCRIBE reg sent')
        except Exception as e:
            log('SUBSCRIBE failed', e)
        return True

    # ------------------------------------------------------------ SIP messaging
    def request_msg(self, method, ruri, body=b'', extra=(), to=None, ctype=None):
        self.cseq += 1
        lines = ['%s %s SIP/2.0' % (method, ruri),
                 'Via: SIP/2.0/UDP [%s]:%d;branch=z9hG4bK%08x;rport' % (self.src, self.us, random.getrandbits(32)),
                 'Max-Forwards: 70',
                 'From: <%s>;tag=%s' % (self.pref_id or self.impu, tag()), 'To: <%s>' % (to or ruri),
                 'Call-ID: %016x@%s' % (random.getrandbits(64), self.src), 'CSeq: %d %s' % (self.cseq, method),
                 'Contact: ' + self.contact(), 'Route: ' + self.service_route if self.service_route else 'X-NoRoute: 1',
                 'P-Preferred-Identity: <%s>' % (self.pref_id or self.impu),
                 'P-Access-Network-Info: ' + self.pani,
                 'Allow: ' + ALLOW,
                 'Supported: sec-agree', 'Require: sec-agree', 'Proxy-Require: sec-agree',
                 'Security-Verify: ' + self.sec_verify,
                 'User-Agent: SM-G973F-G973FXXSGHWC2 Samsung IMS 6.0 DBT'] + list(extra)
        if ctype:
            lines.append('Content-Type: ' + ctype)
        lines.append('Content-Length: %d' % len(body))
        return ('\r\n'.join(l for l in lines if l != 'X-NoRoute: 1') + '\r\n\r\n').encode() + body

    def send_rp(self, ruri, rp, more=()):
        extra = list(more) + ['Accept-Contact: *;+g.3gpp.smsip', 'Request-Disposition: no-fork',
                 'Content-Transfer-Encoding: binary']
        msg = self.request_msg('MESSAGE', ruri, rp, extra, ctype='application/vnd.3gpp.sms')
        self.sc.sendto(msg, (self.pcscf, self.p_port_s))

    def send_sms(self, smsc, tpdu):
        """smsc: SMSC address field (len, type, BCD); returns RP message reference."""
        self.rp_mr = (self.rp_mr + 1) & 255
        digits = bcd_decode(smsc[2:1 + smsc[0]])
        number = ('+' if smsc[1] & 0x70 == 0x10 else '') + digits
        rp = bytes([0x00, self.rp_mr, 0x00]) + bytes(smsc) + bytes([len(tpdu)]) + bytes(tpdu)
        ruri = 'sip:%s@%s;user=phone' % (number, self.domain)
        log('SMS submission prepared; addressing/content omitted')
        self.send_rp(ruri, rp)
        return self.rp_mr

    def reply(self, req_hdr, status, peer, extra=(), body=b'', to_tag=None):
        lines = ['SIP/2.0 ' + status]
        vias = list(header_values(req_hdr.get('via', [])))
        if vias:
            # RFC3581: empty rport is filled by the UAS, along with received.
            top = vias[0]
            if re.search(r';\s*rport(?:\s*=\s*\d+)?(?=;|\s|$)', top, re.I):
                top = re.sub(r';\s*rport(?:\s*=\s*\d+)?(?=;|\s|$)', ';rport=%d' % peer[1], top, count=1, flags=re.I)
                top = re.sub(r';\s*received=[^;\s]+', '', top, count=1, flags=re.I)
                top += ';received=' + peer[0]
            vias[0] = top
        lines += ['Via: ' + via for via in vias]
        for k, name in (('from', 'From'), ('call-id', 'Call-ID'), ('cseq', 'CSeq')):
            for v in req_hdr.get(k, []):
                lines.append('%s: %s' % (name, v))
        to = req_hdr['to'][0]
        lines.append('To: ' + (to if 'tag=' in to else to + ';tag=' + (to_tag or tag())))
        lines += ['Record-Route: ' + r for r in req_hdr.get('record-route', []) if not any(e.startswith('Record-Route') for e in extra)]
        lines += list(extra)
        lines.append('Content-Length: %d' % len(body))
        out = ('\r\n'.join(lines) + '\r\n\r\n').encode() + body
        self.send_response(out, peer)
        n = len(out)
        cseq_parts = (req_hdr.get('cseq') or [''])[0].split()
        method = cseq_parts[-1] if cseq_parts else ''
        if method in ('MESSAGE', 'NOTIFY', 'OPTIONS') and status[:1] in '23456':
            if not hasattr(self, 'response_cache'):
                self.response_cache = collections.OrderedDict()
            key = transaction_key(method, req_hdr)
            self.response_cache[key] = (time.monotonic() + 64, out, peer[:2])
            self.response_cache.move_to_end(key)
            while len(self.response_cache) > 256:
                self.response_cache.popitem(last=False)
        log('reply', status, 'sent', n, 'B; headers/content omitted')

    def replay_reply(self, method, hdr, peer):
        """Repeat the SAME final response, including To-tag, without delivery."""
        cache = getattr(self, 'response_cache', {})
        key = transaction_key(method, hdr)
        item = cache.get(key)
        if not item:
            return False
        expires, response, original_peer = item
        if expires < time.monotonic():
            del cache[key]
            return False
        if original_peer != peer[:2]:
            return False
        self.send_response(response, peer)
        return True

    def send_response(self, response, peer):
        if isinstance(peer, TcpPeer):
            peer.connection.send(response)
        else:
            self.ss.sendto(response, peer)


class Daemon:
    def __init__(self, src, pcscfs):
        self.ims = Ims(src, pcscfs)
        self.pcscfs = pcscfs
        self.clients = {}
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(SOCK)
        os.chmod(SOCK, 0o600)
        self.srv.listen(4)
        self.sends = {}    # rp mr -> (client, id, deadline)
        self.calls = Calls(self.ims, self.emit, log, self.start_media, self.stop_media)
        self.last_status = None
        self.sip_counts = collections.Counter()
        self.sip_seen = collections.OrderedDict()

    def emit(self, obj, only=None):
        data = (json.dumps(obj) + '\n').encode()
        for c in ([only] if only else list(self.clients)):
            try:
                c.sendall(data)
            except OSError:
                self.drop(c)

    def drop(self, c):
        self.clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass

    def register(self):
        for p in self.pcscfs:
            try:
                if self.ims.login(p):
                    self.emit({'op': 'status', 'registered': True})
                    return True
            except Exception as e:
                log('login error', type(e).__name__, e)
        self.ims.registered = False
        self.emit({'op': 'status', 'registered': False})
        return False

    def start_media(self, call):
        r = call['remote']
        self.stop_media()
        log('media start', r['codec'][0], 'pt', r['codec'][1], 'remote port', r['port'], 'local port', call['rtp_port'])
        self.media = subprocess.Popen([sys.executable, '-u', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'callmedia.py'),
                                       self.ims.src, str(call['rtp_port']), r['addr'], str(r['port']), r['codec'][0], str(r['codec'][1])])

    def stop_media(self):
        m = getattr(self, 'media', None)
        if m and m.poll() is None:
            m.terminate()
            try:
                m.wait(3)
            except subprocess.TimeoutExpired:
                m.kill()
        self.media = None

    def on_sip(self, data, peer, via_server):
        status, hdr, body = parse(data.decode('latin1'))
        if status.startswith('SIP/2.0'):
            code = status.split()[1]
            cseq = (hdr.get('cseq') or [''])[0]
            method = cseq.split()[-1] if cseq else ''
            method = method if method in METHODS else 'OTHER'
            self.sip_counts['response_%s_%s' % (method, code if code.isdigit() else 'invalid')] += 1
            log('response', code if code.isdigit() else 'invalid', method)
            if any(m in cseq for m in ('INVITE', 'BYE', 'CANCEL', 'PRACK')):
                self.calls.on_response(status, hdr, body.encode('latin1'))
                return
            if 'MESSAGE' in cseq and int(code) >= 300:
                self.fail_sends('SIP ' + code)
            return
        method = status.split()[0]
        safe_method = method if method in METHODS else 'OTHER'
        self.sip_counts['request_' + safe_method] += 1
        key = transaction_key(method, hdr)
        repeated = key in self.sip_seen
        self.sip_seen[key] = True
        self.sip_seen.move_to_end(key)
        if len(self.sip_seen) > 128:
            self.sip_seen.popitem(last=False)
        if repeated:
            self.sip_counts['repeated_' + safe_method] += 1
        transport = 'tcp' if isinstance(peer, TcpPeer) else 'udp'
        self.sip_counts['request_transport_' + transport] += 1
        log('request', safe_method, 'transport', transport, 'socket', 'server' if via_server else 'client', 'repeat', repeated,
            'requires_precondition', 'precondition' in ' '.join(hdr.get('require', [])).lower())
        if self.ims.replay_reply(method, hdr, peer):
            self.sip_counts['replayed_' + safe_method] += 1
            return
        if method == 'MESSAGE' and 'application/vnd.3gpp.sms' in ' '.join(hdr.get('content-type', [])):
            self.ims.reply(hdr, '200 OK', peer)
            self.on_rp(hdr, data.partition(b'\r\n\r\n')[2])
        elif method == 'NOTIFY':
            self.sip_counts['notify_reg'] += int(any(v.split(';')[0].strip().lower() == 'reg' for v in hdr.get('event', [])))
            self.ims.reply(hdr, '200 OK', peer)
        elif method in ('INVITE', 'ACK', 'BYE', 'CANCEL', 'PRACK', 'UPDATE', 'INFO'):
            self.calls.on_request(method, hdr, body.encode('latin1'), peer, data)
        elif method == 'OPTIONS':
            active = self.calls.call
            same_dialog = active and active.get('call_id') == (hdr.get('call-id') or [None])[0]
            status = '486 Busy Here' if active and not same_dialog else '200 OK'
            extra = ['Allow: ' + ALLOW, 'Accept: ' + ACCEPT, 'Supported: sec-agree',
                     'Contact: ' + self.ims.contact()]
            accept = ','.join(hdr.get('accept', ['application/sdp'])).lower()
            wants_sdp = any(v.split(';')[0].strip() in ('application/sdp', 'application/*', '*/*')
                            and not re.search(r';\s*q\s*=\s*0(?:\.0*)?(?:\s*;|\s*$)', v)
                            for v in accept.split(','))
            capabilities = b''
            if status == '200 OK' and wants_sdp:
                # RFC3264 section9: OPTIONS capabilities use port0, no RTP session.
                capabilities = re.sub(rb'o=- \d+ \d+ ', ('o=- %d 0 ' % time.time_ns()).encode(), self.calls.sdp(0), count=1)
                extra.append('Content-Type: application/sdp')
            self.ims.reply(hdr, status, peer, extra, body=capabilities)
        else:
            self.ims.reply(hdr, '405 Method Not Allowed', peer, ['Allow: ' + ALLOW])

    def fail_sends(self, detail):
        for mr, (c, rid, dl) in list(self.sends.items()):
            self.emit({'op': 'sent', 'id': rid, 'ok': False, 'detail': detail}, c)
            del self.sends[mr]

    def on_rp(self, hdr, rp):
        if len(rp) < 2:
            return
        mti, mr = rp[0] & 7, rp[1]
        if mti == 1:   # RP-DATA network -> UE
            n = rp[2]
            oa = bytes(rp[2:3 + n])
            pos = 3 + n
            pos += 1 + rp[pos]            # RP-DA (empty)
            ln = rp[pos]
            tpdu = bytes(rp[pos + 1:pos + 1 + ln])
            frm = re.search(r'<?(sip:[^>;]+|tel:[^>;]+)', hdr.get('p-asserted-identity', hdr.get('from', ['']))[0])
            did = self.ims.next_delivery
            self.ims.next_delivery += 1
            self.ims.deliveries[did] = (frm.group(1) if frm else self.ims.impu, mr, (hdr.get('call-id') or [''])[0])
            log('incoming SMS (RP-DATA), delivery', did)
            self.emit({'op': 'deliver', 'id': did, 'smsc': oa.hex(), 'tpdu': tpdu.hex()})
        elif mti in (3, 5):   # RP-ACK / RP-ERROR for our submit
            ent = self.sends.pop(mr, None)
            log('RP', 'ACK' if mti == 3 else 'ERROR', 'for mr', mr, 'known' if ent else 'unknown',
                ('cause ' + rp[3:3 + (rp[2] if len(rp) > 2 else 0)].hex()) if mti == 5 else '')
            if ent:
                cause = rp[3] if mti == 5 and len(rp) > 3 else None
                self.emit({'op': 'sent', 'id': ent[1], 'ok': mti == 3, 'detail': 'RP-ERROR cause %s' % cause if cause is not None else 'RP-ACK'}, ent[0])

    def on_client(self, c, line):
        req = json.loads(line)
        if req['op'] == 'diagnostics':
            return self.emit({'op': 'diagnostics', 'registered': self.ims.registered,
                              'call_active': self.calls.call is not None,
                              'binding': getattr(self.ims, 'binding', {}),
                              'sip_counts': dict(self.sip_counts)}, c)
        if req['op'] == 'send':
            if not self.ims.registered:
                return self.emit({'op': 'sent', 'id': req['id'], 'ok': False, 'detail': 'not registered'}, c)
            mr = self.ims.send_sms(bytes.fromhex(req['smsc']), bytes.fromhex(req['tpdu']))
            self.sends[mr] = (c, req['id'], time.time() + 60)
            log('SMS submitted, rp mr', mr)
        elif req['op'] == 'dial':
            if not self.ims.registered or not self.calls.dial(req['number']):
                self.emit({'op': 'call', 'state': 'ended', 'dir': 'out', 'number': req['number'], 'why': 'not possible'}, c)
        elif req['op'] == 'answer':
            self.calls.answer()
        elif req['op'] == 'hangup':
            self.calls.hangup()
        elif req['op'] == 'ack':
            uri, mr, cid = self.ims.deliveries.pop(req['id'], (None, None, ''))
            if uri:
                rp = bytes([0x02, mr]) if req['ok'] else bytes([0x04, mr, 1, 0x16])
                self.ims.send_rp(uri, rp, ['In-Reply-To: ' + cid] if cid else [])
                log('RP-%s sent for delivery' % ('ACK' if req['ok'] else 'ERROR'), req['id'])

    def run(self):
        self.register()
        bufs = {}
        while True:
            rl = [self.srv] + list(self.clients)
            if self.ims.sc:
                rl += [self.ims.sc, self.ims.ss]
            if self.ims.ts:
                rl.append(self.ims.ts)
            rl += list(self.ims.tcp_connections)
            writable = [s for s, c in self.ims.tcp_connections.items() if c.outgoing]
            r, w, _ = select.select(rl, writable, [], 5)
            for s in w:
                connection = self.ims.tcp_connections.get(s)
                if connection:
                    try:
                        connection.flush()
                    except OSError:
                        connection.close()
                        del self.ims.tcp_connections[s]
            for s in r:
                if s.fileno() < 0:
                    continue
                if s is self.srv:
                    c, _ = self.srv.accept()
                    self.clients[c] = True
                    bufs[c] = b''
                    self.emit({'op': 'status', 'registered': self.ims.registered}, c)
                elif s is self.ims.ts:
                    try:
                        stream, address = s.accept()
                        if ipaddress.ip_address(address[0]) != ipaddress.ip_address(self.ims.pcscf) or address[1] != self.ims.p_port_c or len(self.ims.tcp_connections) >= 8:
                            stream.close()
                            self.sip_counts['tcp_connection_rejected'] += 1
                        else:
                            self.ims.tcp_connections[stream] = SipConnection(stream, address)
                            self.sip_counts['tcp_connection_accepted'] += 1
                    except BlockingIOError:
                        pass
                elif s in self.ims.tcp_connections:
                    connection = self.ims.tcp_connections[s]
                    try:
                        chunk = s.recv(65536)
                        if not chunk:
                            connection.close()
                            del self.ims.tcp_connections[s]
                            continue
                        for frame in connection.feed(chunk):
                            self.on_sip(frame, connection.peer, True)
                    except BlockingIOError:
                        pass
                    except (OSError, ValueError, KeyError, IndexError):
                        connection.close()
                        del self.ims.tcp_connections[s]
                        self.sip_counts['tcp_stream_error'] += 1
                elif s in (self.ims.sc, self.ims.ss):
                    try:
                        data, peer = s.recvfrom(65535)
                        self.on_sip(data, peer, s is self.ims.ss)
                    except Exception as e:
                        log('sip error', type(e).__name__, e)
                else:
                    d = s.recv(65536)
                    if not d:
                        self.drop(s)
                        continue
                    bufs[s] += d
                    while b'\n' in bufs[s]:
                        line, bufs[s] = bufs[s].split(b'\n', 1)
                        try:
                            self.on_client(s, line)
                        except Exception as e:
                            log('client error', type(e).__name__, e)
            now = time.time()
            self.calls.tick()
            for mr, (c, rid, dl) in list(self.sends.items()):
                if now > dl:
                    del self.sends[mr]
                    self.emit({'op': 'sent', 'id': rid, 'ok': False, 'detail': 'timeout'}, c)
            if self.ims.registered and now > self.ims.expires_at:
                log('re-registering')
                self.register()
            elif not self.ims.registered and now - getattr(self, 'last_try', 0) > 60:
                self.last_try = now
                self.register()


def discover(logfile):
    txt = open(logfile).read()
    pcscfs = re.findall(r"P-CSCF IPV6 ADDRESS \[(.*?)\]", txt)[-1]
    pcscfs = re.findall(r"'([0-9a-f:]+)'", pcscfs)
    addr = re.findall(r"IPV6 ADDRESS \['([0-9a-f:]+)'\]", txt)
    out = subprocess.run(['ip', '-6', '-br', 'addr', 'show', 'dev', 'tun1'], capture_output=True, text=True).stdout
    src = next(a for a in re.findall(r'([0-9a-f:]+)/64', out) if a.endswith('::2'))
    return src, pcscfs


if __name__ == '__main__':
    src, pcscfs = discover(sys.argv[1])
    log('imsd start, %d P-CSCF' % len(pcscfs))
    Daemon(src, pcscfs).run()
