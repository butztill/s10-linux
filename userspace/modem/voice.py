# SPDX-License-Identifier: GPL-2.0-only
"""Voice call AT commands (ModemManager generic voice) on top of imsd (VoWiFi/IMS, see userspace/vowifi).
Dial/answer/hangup are forwarded to imsd over the ImsLink socket; call state comes back as 'call' events."""
import re
import time


class VoiceBridge:
    def __init__(self, ims):
        self.ims = ims
        self.call = None          # dict(dir 0|1, stat 0 active|2 dialing|3 alerting|4 incoming, number)
        self.last_ring = 0

    def _events(self, at=None):
        if self.ims.sock is None:
            self.ims.poll()          # connects / reads; SMS bridge may poll too, call events are queued either way
        if self.call and (self.ims.sock is None or not self.ims.registered):
            self.call = None         # imsd restarted or lost registration: its call state is gone
            if at is not None:
                at.out('\r\nNO CARRIER\r\n')
        evs, self.ims.call_events = self.ims.call_events, []
        for ev in evs:
            st = ev['state']
            if st == 'ended':
                had = self.call is not None
                self.call = None
                if had and at is not None:
                    at.out('\r\nNO CARRIER\r\n')
            elif st == 'dialing':
                self.call = {'dir': 0, 'stat': 2, 'number': ev.get('number') or ''}
            elif st == 'ringing':
                if ev.get('dir') == 'in':
                    self.call = {'dir': 1, 'stat': 4, 'number': ev.get('number') or ''}
                elif self.call:
                    self.call['stat'] = 3
            elif st == 'active' and self.call:
                self.call['stat'] = 0

    def flush(self, at):
        self._events(at)
        c = self.call
        if c and c['stat'] == 4 and time.monotonic() - self.last_ring > 3:
            self.last_ring = time.monotonic()
            n = c['number']
            at.out('\r\nRING\r\n\r\n+CLIP: "%s",%d,,,,0\r\n' % (n, 145 if n.startswith('+') else 129))

    def handle(self, c, at):
        if c.startswith('D') and c.endswith(';'):
            number = re.sub(r'[^0-9+*#]', '', c[1:-1].lstrip('TP'))
            if self.call or not self.ims.registered or not number:
                at.out('\r\nNO CARRIER\r\n')
                return True
            try:
                self.ims.command('dial', number=number)
            except OSError:
                at.out('\r\nNO CARRIER\r\n')
                return True
            self.call = {'dir': 0, 'stat': 2, 'number': number}
            at.ok()
        elif c == 'A':
            try:
                self.ims.command('answer')
            except OSError:
                return at.err() or True
            at.ok()
        elif c in ('H', 'H0', '+CHUP') or re.fullmatch(r'\+CHLD=[01]\d?', c):
            try:
                self.ims.command('hangup')
            except OSError:
                pass
            self.call = None
            at.ok()
        elif c == '+CLCC':
            self._events(at)
            if self.call:
                n = self.call['number']
                at.lines(['+CLCC: 1,%d,%d,0,0,"%s",%d' % (self.call['dir'], self.call['stat'], n, 145 if n.startswith('+') else 129)])
            else:
                at.ok()
        elif c == '+CHLD=?':
            at.lines(['+CHLD: (0,1,2,3)'])
        elif c == '+CVHU=?':
            at.lines(['+CVHU: (0-2)'])
        elif c.startswith(('+CVHU=', '+CCWA=', '+VTS=', '+CSSN=', '+CHLD=')):
            at.ok()
        elif c == '+CLIP?':
            at.lines(['+CLIP: 1,1'])
        elif c == '+CCWA?':
            at.lines(['+CCWA: 1'])
        else:
            return False
        return True
