# SPDX-License-Identifier: GPL-2.0-only
"""SIP call control (MMTel voice, TS 24.229) for imsd: one call at a time, UAC and UAS.
Signalling only; media is handled by callmedia.py (RTP/AMR) which imsd starts when a call is established."""
import random
import re
import time

CODECS = [('AMR-WB', 16000, 98), ('AMR', 8000, 97)]


def digits(uri):
    return re.sub(r'[^0-9+]', '', uri.split(';')[0])


def mask(text):
    return re.sub(r'(\+?\d{3})\d{5,}', r'\1*****', text)


class Calls:
    def __init__(self, ims, emit, log, start_media, stop_media):
        self.ims, self.emit, self.log = ims, emit, log
        self.start_media, self.stop_media = start_media, stop_media
        self.call = None

    # ----------------------------------------------------------------- helpers
    def sdp(self, port, codec=None, dtmf_pt=101):
        """Offer (codec None: all) or answer (codec = (name, pt, rate) chosen from the offer)."""
        pts = [(n, r, p) for n, r, p in CODECS] if codec is None else [(codec[0], codec[2], codec[1])]
        lines = ['v=0', 'o=- %d %d IN IP6 %s' % (int(time.time()), int(time.time()), self.ims.src), 's=-',
                 'c=IN IP6 %s' % self.ims.src, 't=0 0',
                 'm=audio %d RTP/AVP %s %d' % (port, ' '.join(str(p) for _, _, p in pts), dtmf_pt)]
        for name, rate, pt in pts:
            lines.append('a=rtpmap:%d %s/%d/1' % (pt, name, rate))
            lines.append('a=fmtp:%d octet-align=1;mode-change-capability=2;max-red=0' % pt)
        lines += ['a=rtpmap:%d telephone-event/8000' % dtmf_pt, 'a=fmtp:%d 0-15' % dtmf_pt,
                  'a=ptime:20', 'a=maxptime:240', 'a=sendrecv']
        return ('\r\n'.join(lines) + '\r\n').encode()

    @staticmethod
    def parse_sdp(body):
        text = body.decode('latin1')
        m = re.search(r'c=IN IP6 (\S+)', text)
        a = re.search(r'm=audio (\d+) RTP/AVP ([\d ]+)', text)
        if not m or not a:
            return None
        pts = [int(x) for x in a.group(2).split()]
        names = {int(p): n.upper() for p, n in re.findall(r'a=rtpmap:(\d+) ([\w-]+)/', text)}
        chosen = None
        for name, rate, _ in CODECS:                # our preference order
            for pt in pts:
                if names.get(pt) == name.upper() and chosen is None:
                    chosen = (name, pt, rate)
        dtmf = next((p for p in pts if names.get(p) == 'TELEPHONE-EVENT'), None)
        return {'addr': m.group(1), 'port': int(a.group(1)), 'codec': chosen, 'dtmf': dtmf}

    def req(self, method, ruri, c, extra=(), body=b'', ctype=None, to_tag=True, cseq=None, branch=None):
        ims = self.ims
        local = ('<%s>;tag=%s' % (ims.pref_id or ims.impu, c['local_tag']))
        remote = '<%s>' % c['remote_uri'] + (';tag=' + c['remote_tag'] if c.get('remote_tag') and to_tag else '')
        if c['dir'] == 'in':
            local, remote = ('<%s>;tag=%s' % (c['local_uri'], c['local_tag']),
                             '%s' % c['from_hdr'])
        route = c.get('route') or ([ims.service_route] if ims.service_route else [])
        lines = ['%s %s SIP/2.0' % (method, ruri),
                 'Via: SIP/2.0/UDP [%s]:%d;branch=%s;rport' % (ims.src, ims.us, branch or 'z9hG4bK%08x' % random.getrandbits(32)),
                 'Max-Forwards: 70', 'From: ' + local, 'To: ' + remote, 'Call-ID: ' + c['call_id'],
                 'CSeq: %d %s' % (cseq if cseq is not None else c['cseq'], method)]
        lines += ['Route: ' + r for r in route]
        lines += ['Supported: 100rel, timer, path, sec-agree', 'Require: sec-agree', 'Proxy-Require: sec-agree',
                  'Security-Verify: ' + ims.sec_verify,
                  'P-Preferred-Identity: <%s>' % (ims.pref_id or ims.impu),
                  'P-Access-Network-Info: ' + ims.pani,
                  'Allow: INVITE, ACK, CANCEL, BYE, UPDATE, PRACK, MESSAGE, OPTIONS',
                  'User-Agent: SM-G973F-G973FXXSGHWC2 Samsung IMS 6.0 DBT'] + list(extra)
        if ctype:
            lines.append('Content-Type: ' + ctype)
        lines.append('Content-Length: %d' % len(body))
        return ('\r\n'.join(lines) + '\r\n\r\n').encode() + body

    def send(self, data):
        self.ims.sc.sendto(data, (self.ims.pcscf, self.ims.p_port_s))

    def state(self, st, **kw):
        c = self.call
        self.emit(dict({'op': 'call', 'state': st, 'dir': c['dir'] if c else None,
                        'number': c.get('number') if c else None}, **kw))

    def end(self, why):
        c, self.call = self.call, None
        if c:
            self.stop_media()
            self.emit({'op': 'call', 'state': 'ended', 'dir': c['dir'], 'number': c.get('number'), 'why': why})
            self.log('call ended:', why)

    # --------------------------------------------------------------- outgoing
    def dial(self, number):
        if self.call:
            return False
        n = number.strip().replace(' ', '')
        if n == 'self':
            n = digits(self.ims.pref_id or '')
        if n.startswith('00'):
            n = '+' + n[2:]
        elif n.startswith('0'):
            n = '+49' + n[1:]
        ims = self.ims
        c = self.call = {'dir': 'out', 'number': n, 'call_id': '%016x@%s' % (random.getrandbits(64), ims.src),
                         'local_tag': '%08x' % random.getrandbits(32), 'remote_uri': 'tel:' + n, 'cseq': 1,
                         'state': 'dialing', 'rtp_port': random.randrange(40000, 50000) & ~1, 'started': time.time()}
        c['branch'] = 'z9hG4bK%08x' % random.getrandbits(32)
        extra = ['Contact: ' + ims.contact(ims.us), 'Session-Expires: 1800;refresher=uac', 'Min-SE: 90',
                 'Accept-Contact: *;+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel"',
                 'P-Preferred-Service: urn:urn-7:3gpp-service.ims.icsi.mmtel']
        c['invite'] = self.req('INVITE', c['remote_uri'], c, extra, self.sdp(c['rtp_port']), 'application/sdp',
                               to_tag=False, branch=c['branch'])
        self.send(c['invite'])
        self.log('INVITE sent, call', mask(n))
        self.state('dialing')
        return True

    def hangup(self):
        c = self.call
        if not c:
            return
        if c['state'] in ('dialing', 'ringing') and c['dir'] == 'out':
            extra = []
            self.send(self.req('CANCEL', c['remote_uri'], c, extra, to_tag=False, branch=c['branch']))
            c['cancelled'] = True
            c['cancel_at'] = time.time()
        elif c['state'] == 'active':
            c['cseq'] += 1
            self.send(self.req('BYE', c.get('target') or c['remote_uri'], c, cseq=c['cseq']))
            self.end('local hangup')
        elif c['dir'] == 'in' and c['state'] == 'ringing':
            self.reply_invite('603 Decline')
            self.end('declined')

    # ------------------------------------------------------------- responses
    def on_response(self, status, hdr, body):
        c = self.call
        code = int(status.split()[1])
        cseq = (hdr.get('cseq') or [''])[0]
        if not c:
            return
        if 'INVITE' in cseq and c['dir'] == 'out':
            m = re.search(r'tag=([^;>\s]+)', (hdr.get('to') or [''])[0])
            if code < 200:
                if body and b'm=audio' in body:
                    c['remote'] = self.parse_sdp(body)
                    self.log('early SDP', c['remote'])
                if code in (180, 183):
                    c['state'] = 'ringing'
                    self.log('INVITE ->', status[:40])
                    self.state('ringing')
                    if code == 183 or 'require' in hdr and '100rel' in ' '.join(hdr['require']):
                        pass
                    if 'rseq' in hdr:
                        # reliable provisional response: PRACK
                        c['cseq'] += 1
                        if m:
                            c['remote_tag'] = m.group(1)
                        rack = '%s %s INVITE' % (hdr['rseq'][0].split()[0], re.sub(r'\D*(\d+).*', r'\1', cseq))
                        self.send(self.req('PRACK', c.get('target') or c['remote_uri'], c, ['RAck: ' + rack], cseq=c['cseq']))
                return
            if 200 <= code < 300:
                if c.get('cancelled'):
                    c['remote_tag'] = m.group(1) if m else ''
                    self.send_ack(hdr)
                    c['cseq'] += 1
                    self.send(self.req('BYE', c.get('target') or c['remote_uri'], c, cseq=c['cseq']))
                    return self.end('cancelled')
                c['remote_tag'] = m.group(1) if m else ''
                ct = re.search(r'<([^>]+)>', (hdr.get('contact') or [''])[0])
                c['target'] = ct.group(1) if ct else c['remote_uri']
                c['route'] = [r for rr in reversed(hdr.get('record-route', [])) for r in [rr]] or None
                self.send_ack(hdr)
                c['state'] = 'active'
                if body and b'm=audio' in body:
                    c['remote'] = self.parse_sdp(body)
                self.log('call established', c.get('remote'))
                self.state('active')
                if c.get('remote') and c['remote']['codec']:
                    self.start_media(c)
                return
            # failure
            self.ack_failure(hdr)
            self.log('INVITE failed', status[:60])
            self.end('%s' % status[8:60])
        elif 'BYE' in cseq or ('CANCEL' in cseq and code >= 200):
            if 'BYE' in cseq and not self.call:
                return

    def send_ack(self, hdr):
        c = self.call
        self.send(self.req('ACK', c.get('target') or c['remote_uri'], c, cseq=c['cseq'] if False else 1))

    def ack_failure(self, hdr):
        c = self.call
        m = re.search(r'tag=([^;>\s]+)', (hdr.get('to') or [''])[0])
        c['remote_tag'] = m.group(1) if m else ''
        self.send(self.req('ACK', c['remote_uri'], c, cseq=1, branch=c['branch']))

    # -------------------------------------------------------------- requests
    def on_request(self, method, hdr, body, peer, raw):
        ims = self.ims
        c = self.call
        if method == 'INVITE':
            if c:
                self.log('INVITE while busy -> 486; headers/content omitted')
                return ims.reply(hdr, '486 Busy Here', peer)
            if 'precondition' in ' '.join(hdr.get('require', [])):
                self.log('INVITE requires preconditions -> 420')
                return ims.reply(hdr, '420 Bad Extension', peer, ['Unsupported: precondition'])
            offer = self.parse_sdp(body)
            if not offer or not offer['codec']:
                return ims.reply(hdr, '488 Not Acceptable Here', peer)
            frm = (hdr.get('p-asserted-identity') or hdr.get('from') or [''])[0]
            number = digits(re.search(r'(sip:[^>;@]+|tel:[^>;]+)', frm).group(1)) if re.search(r'(sip:[^>;@]+|tel:[^>;]+)', frm) else '?'
            self.call = c = {'dir': 'in', 'number': number, 'call_id': hdr['call-id'][0], 'local_tag': '%08x' % random.getrandbits(32),
                             'from_hdr': hdr['from'][0], 'local_uri': re.search(r'<?(sip:[^>;]+|tel:[^>;]+)', hdr['to'][0]).group(1),
                             'remote_uri': re.search(r'<([^>]+)>', hdr['from'][0]).group(1), 'cseq': 1,
                             'state': 'ringing', 'invite_hdr': hdr, 'peer': peer, 'remote': offer,
                             'target': (re.search(r'<([^>]+)>', (hdr.get('contact') or [''])[0]) or [None, None])[1],
                             'route': list(hdr.get('record-route', [])) or None,
                             'rtp_port': random.randrange(40000, 50000) & ~1, 'record_route': hdr.get('record-route', [])}
            self.log('incoming INVITE; headers/content omitted')
            ims.reply(hdr, '100 Trying', peer)
            self.reply_invite('180 Ringing')
            self.state('ringing', number=number)
        elif method == 'ACK':
            if c and c['dir'] == 'in' and c['state'] == 'answering':
                c['state'] = 'active'
                self.state('active')
                self.start_media(c)
        elif method == 'BYE':
            ims.reply(hdr, '200 OK', peer)
            if c:
                self.end('remote hangup')
        elif method == 'CANCEL':
            ims.reply(hdr, '200 OK', peer)
            if c and c['dir'] == 'in' and c['state'] == 'ringing':
                self.reply_invite('487 Request Terminated')
                self.end('caller cancelled')
        elif method in ('PRACK', 'UPDATE', 'INFO'):
            ims.reply(hdr, '200 OK', peer)
        else:
            ims.reply(hdr, '405 Method Not Allowed', peer, ['Allow: INVITE, ACK, CANCEL, BYE, MESSAGE, OPTIONS, PRACK, UPDATE'])

    def reply_invite(self, status, body=b''):
        c = self.call
        h = c['invite_hdr']
        extra = ['Contact: ' + self.ims.contact(self.ims.us)] + ['Record-Route: ' + r for r in c['record_route']]
        if body:
            extra += ['Content-Type: application/sdp']
        self.ims.reply(h, status, c['peer'], extra, body=body, to_tag=c['local_tag'])

    def tick(self):
        """Housekeeping: give up a cancelled call that the network never answers."""
        c = self.call
        if c and c.get('cancelled') and time.time() - c['cancel_at'] > 6:
            self.end('cancelled (no answer from network)')
        elif c and c['state'] == 'dialing' and time.time() - c['started'] > 40:
            self.end('no response from network')

    def answer(self):
        c = self.call
        if not c or c['dir'] != 'in' or c['state'] != 'ringing':
            return False
        o = c['remote']
        c['state'] = 'answering'
        self.reply_invite('200 OK', self.sdp(c['rtp_port'], o['codec'], o['dtmf'] or 101))
        return True
