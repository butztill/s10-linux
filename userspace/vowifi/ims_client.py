#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Minimal VoWiFi IMS client (SIP over the ePDG tunnel, no sec-agree IPsec): REGISTER with IMS-AKA via the CP.

usage (inside the tunnel namespace, as root):  ip netns exec ims python3 ims_client.py SRC_IPV6 PCSCF_IPV6
IMSI/IMEI/number are never printed in full."""
import base64, hashlib, os, random, re, socket, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usim_cp


def md5h(b):
    return hashlib.md5(b if isinstance(b, bytes) else b.encode()).hexdigest()


def parse(data):
    head, _, body = data.partition('\r\n\r\n')
    lines = head.split('\r\n')
    hdr = {}
    for ln in lines[1:]:
        if ':' in ln:
            k, v = ln.split(':', 1)
            hdr.setdefault(k.strip().lower(), []).append(v.strip())
    return lines[0], hdr, body


class Ims:
    def __init__(self, src, pcscf, imei=None):
        if imei is None:
            imei = usim_cp.imei()
        if not isinstance(imei, str) or not re.fullmatch(r'[0-9]{15}', imei):
            raise ValueError('A 15-digit device IMEI is required')
        self.src, self.pcscf = src, pcscf
        self.imsi = usim_cp.imsi()
        self.imei = imei
        mnc = '%03d' % int(self.imsi[3:5])
        self.domain = 'ims.mnc%s.mcc%s.3gppnetwork.org' % (mnc, self.imsi[:3])
        self.impu = 'sip:%s@%s' % (self.imsi, self.domain)
        self.impi = '%s@%s' % (self.imsi, self.domain)
        self.call_id = '%016x' % random.getrandbits(64)
        self.tag = '%08x' % random.getrandbits(32)
        self.cseq = 0
        self.spi_c, self.spi_s = 0x10000 + random.getrandbits(16), 0x20000 + random.getrandbits(16)
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.sock.bind((src, 5060))
        self.sock.settimeout(10)
        self.service_route = []

    def mask(self, t):
        return t.replace(self.imsi, self.imsi[:6] + '*' * 9)

    def register(self, auth=None, expires=600000):
        self.cseq += 1
        lines = ['REGISTER sip:%s SIP/2.0' % self.domain,
                 'Via: SIP/2.0/UDP [%s]:5060;branch=z9hG4bK%08x;rport' % (self.src, random.getrandbits(32)),
                 'Max-Forwards: 70',
                 'From: <%s>;tag=%s' % (self.impu, self.tag), 'To: <%s>' % self.impu,
                 'Call-ID: %s' % self.call_id, 'CSeq: %d REGISTER' % self.cseq,
                 'Contact: <sip:%s@[%s]:5060>;+sip.instance="<urn:gsma:imei:%s-%s-%s-0>";+g.3gpp.smsip'
                 % (self.imsi, self.src, self.imei[:8], self.imei[8:14], self.imei[14:] or '0'),
                 'Expires: %d' % expires, 'Supported: path, sec-agree', 'Require: sec-agree', 'Proxy-Require: sec-agree',
                 'Security-Client: ipsec-3gpp; alg=hmac-sha-1-96; ealg=null; spi-c=%d; spi-s=%d; port-c=5062; port-s=5063' % (self.spi_c, self.spi_s),
                 'P-Access-Network-Info: IEEE-802.11; i-wlan-node-id=000000000000',
                 'Authorization: ' + (auth or 'Digest username="%s", realm="ims.telekom.de", nonce="", uri="sip:%s", response="", algorithm=AKAv1-MD5' % (self.impi, self.domain)),
                 'User-Agent: S10-Linux IMS client']
        self.sock.sendto(('\r\n'.join(lines) + '\r\nContent-Length: 0\r\n\r\n').encode(), (self.pcscf, 5060))
        data, _ = self.sock.recvfrom(65535)
        return parse(data.decode('latin1'))

    def login(self):
        status, hdr, _ = self.register()
        print('REGISTER 1 ->', status)
        if '401' not in status:
            for k, v in hdr.items():
                print('  %s: %s' % (k, self.mask(' | '.join(v))[:200]))
        for attempt in range(3):
            if '401' not in status:
                return status, hdr
            chal = hdr['www-authenticate'][0]
            nonce_b64 = re.search(r'nonce="([^"]+)"', chal).group(1)
            realm = re.search(r'realm="([^"]+)"', chal).group(1)
            nonce = base64.b64decode(nonce_b64)
            res, ck, ik = usim_cp.res_ck_ik(nonce[:16].hex(), nonce[16:32].hex())
            uri = 'sip:' + self.domain
            if ck is None:
                print('SQN sync failure, sending AUTS')
                auth = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="", algorithm=AKAv1-MD5, auts="%s"'
                        % (self.impi, realm, nonce_b64, uri, base64.b64encode(bytes.fromhex(res)).decode()))
            else:
                cn, nc = '%08x' % random.getrandbits(32), '00000001'
                ha1 = md5h(self.impi.encode() + b':' + realm.encode() + b':' + bytes.fromhex(res))
                resp = md5h('%s:%s:%s:%s:auth:%s' % (ha1, nonce_b64, nc, cn, md5h('REGISTER:' + uri)))
                auth = ('Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s", algorithm=AKAv1-MD5, '
                        'qop=auth, nc=%s, cnonce="%s"' % (self.impi, realm, nonce_b64, uri, resp, nc, cn))
            status, hdr, _ = self.register(auth)
            print('REGISTER %d ->' % (self.cseq), status)
        return status, hdr


if __name__ == '__main__':
    c = Ims(sys.argv[1], sys.argv[2])
    status, hdr = c.login()
    for k in ('service-route', 'path', 'p-associated-uri', 'expires', 'contact', 'warning', 'reason', 'allow', 'p-charging-vector'):
        for v in hdr.get(k, []):
            print('  %s: %s' % (k, re.sub(r'\+49\d+', '+49***', c.mask(v))))
