#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Shannon GSM SMS bridge. No dial capability.

IPC layouts: stock G973FXXSGHWC2 IpcRxIncomingMsg @44bfd0,
IpcTxDeliveryReport @44a918, IpcTxSetDeviceReady @44afa0.
Keep PDU bytes private; ModemManager decodes GSM/UCS2/multipart messages.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import time


def submit_pdu(pdu, expected_tpdu, default_smsc=None):
    """Validate SMS-SUBMIT framing without interpreting private text/number."""
    if not pdu or pdu[0] > 12 or 1 + pdu[0] >= len(pdu):
        raise ValueError('invalid SMSC length')
    head = 1 + pdu[0]
    tpdu = pdu[head:]
    if len(tpdu) != expected_tpdu or not 7 <= len(tpdu) <= 244 or tpdu[0] & 3 != 1:
        raise ValueError('invalid SMS-SUBMIT length/type')
    # Destination, PID, DCS, optional validity period, UDL and user data.
    if not 1 <= tpdu[2] <= 20 or not tpdu[3] & 0x80:
        raise ValueError('invalid destination framing')
    end = 4 + (tpdu[2] + 1) // 2
    validity = (0, 7, 1, 7)[(tpdu[0] >> 3) & 3]
    if end + 3 + validity > len(tpdu):
        raise ValueError('truncated SMS-SUBMIT')
    dcs = tpdu[end + 1]
    # General data coding groups only: GSM7, 8bit and UCS2. Reject unsupported
    # compression/reserved groups instead of inventing their data length.
    if dcs & 0xe0 or ((dcs >> 2) & 3) == 3:
        raise ValueError('unsupported data coding')
    alphabet = (dcs >> 2) & 3
    udl = tpdu[end + 2 + validity]
    size = (udl * 7 + 7) // 8 if alphabet == 0 else udl
    if size > 140 or len(tpdu) != end + 3 + validity + size:
        raise ValueError('invalid user data length')
    if tpdu[0] & 0x40 and (size == 0 or tpdu[end + 3 + validity] + 1 > size):
        raise ValueError('truncated user data header')
    if pdu[0] == 0:
        if not default_smsc or not 1 <= default_smsc[0] <= 12 or len(default_smsc) != 1 + default_smsc[0]:
            raise LookupError('default SMSC unavailable')
        pdu = bytes(default_smsc) + tpdu
    if len(pdu) > 255:
        raise ValueError('IPC PDU length overflow')
    return bytes(pdu)


def incoming(payload):
    """Return (transaction id, full GSM PDU); reject truncated/other formats."""
    if len(payload) < 7 or payload[0] != 2:
        raise ValueError('unsupported SMS envelope')
    size = payload[5]
    if size == 0 or len(payload) != 6 + size:
        raise ValueError('invalid SMS length')
    pdu = bytes(payload[6:])
    if pdu[0] > 12 or 1 + pdu[0] >= len(pdu):
        raise ValueError('invalid SMSC length')
    if pdu[1 + pdu[0]] & 3 not in (0, 2):
        raise ValueError('not an incoming GSM TPDU')
    return payload[4], pdu


class SMSBridge:
    LIMIT = 1000

    def __init__(self, tx, path='/var/lib/s10-modem/sms.json', request=None, ims=None):
        self.tx, self.path = tx, Path(path)
        self.ims = ims
        self.request = request
        self.input = None
        self.pending = None
        self.tx_inflight = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.records, self.next_id = {}, 1
        self.notifications = []
        self.enabled = False
        self.cnmi = (0, 0, 0, 0, 0)
        self.ready_sent = False
        if self.path.exists():
            state = json.loads(self.path.read_text())
            if state.get('version') != 1:
                raise ValueError('unknown SMS store version')
            self.next_id = state['next_id']
            self.tx_inflight = bool(state.get('tx_inflight', False))
            self.records = {int(k): v for k, v in state['records'].items()}
            if len(self.records) > self.LIMIT or self.next_id <= max(self.records, default=0):
                raise ValueError('invalid SMS store')
            for record in self.records.values():
                pdu = bytes.fromhex(record['pdu'])
                if not pdu or record['status'] not in (0, 1):
                    raise ValueError('invalid SMS record')

    def persist(self, records, next_id, inflight=None):
        """Atomic private file + directory fsync, before acknowledging to CP."""
        fd, name = tempfile.mkstemp(prefix='.sms-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump({'version': 1, 'next_id': next_id, 'records': records,
                           'tx_inflight': self.tx_inflight if inflight is None else inflight}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.records, self.next_id = records, next_id
        if inflight is not None:
            self.tx_inflight = inflight

    def on_ipc(self, main, sub, typ, payload, seq=0, ack=0):
        # Stock GetRxData dispatches SMS_SEND by subcommand; hardware reports
        # the final result as NOTI, whose ack is not the request sequence.
        # There is exactly one in-flight send, retained even after timeout.
        # RESP still needs its matching ack; never accept an unrelated RESP.
        if (main, sub) == (4, 1) and self.pending and 'ims' not in self.pending and (
                typ == 3 or (typ == 2 and ack == self.pending['seq'])):
            if len(payload) < 5 or payload[0] != 2 or len(payload) != 5 + payload[4]:
                return  # malformed reply is not evidence of send success/failure
            result = int.from_bytes(payload[1:3], 'little')
            at = self.pending['at']
            # Clear durable uncertainty only for the correctly matched final result.
            self.persist(self.records, self.next_id, inflight=False)
            self.pending = None
            if at is not None:
                if result == 0:
                    at.lines(['+CMGS: %d' % payload[3]])
                else:
                    self.cms_error(at, 500)
            return
        if (main, sub, typ) != (4, 2, 3):
            return
        tpid, pdu = incoming(payload)
        text = pdu.hex().upper()
        index = next((k for k, v in self.records.items()
                      if v['pdu'] == text and v['tpid'] == tpid), None)
        if index is None:
            if len(self.records) >= self.LIMIT:
                raise OSError('SMS store full; not acknowledging')
            index = self.next_id
            records = dict(self.records)
            records[index] = {'pdu': text, 'tpid': tpid, 'status': 0}
            self.persist(records, index + 1)
        # GSM success: network_type, LE16 result=0, transaction id, pdu_len=0.
        self.tx(4, 6, 1, bytes([2, 0, 0, tpid, 0]))
        if index not in self.notifications:
            self.notifications.append(index)

    def poll_ims(self):
        """Handle imsd events: final result of a send, and incoming SMS-DELIVER TPDUs."""
        if self.ims is None:
            return
        for ev in self.ims.poll():
            if ev['op'] == 'sent' and self.pending and self.pending.get('ims') == ev['id']:
                at = self.pending['at']
                mr = self.pending['mr']
                self.persist(self.records, self.next_id, inflight=False)
                self.pending = None
                if at is not None:
                    if ev['ok']:
                        at.lines(['+CMGS: %d' % mr])
                    else:
                        self.cms_error(at, 500)
            elif ev['op'] == 'deliver':
                ok = False
                try:
                    pdu = bytes.fromhex(ev['smsc']) + bytes.fromhex(ev['tpdu'])
                    if pdu[0] > 12 or 1 + pdu[0] >= len(pdu) or pdu[1 + pdu[0]] & 3 != 0:
                        raise ValueError('not an SMS-DELIVER')
                    text = pdu.hex().upper()
                    index = next((k for k, v in self.records.items() if v['pdu'] == text and v['tpid'] == 256), None)
                    if index is None:
                        if len(self.records) >= self.LIMIT:
                            raise OSError('SMS store full')
                        index = self.next_id
                        records = dict(self.records)
                        records[index] = {'pdu': text, 'tpid': 256, 'status': 0}
                        self.persist(records, index + 1)
                    if index not in self.notifications:
                        self.notifications.append(index)
                    ok = True
                except Exception:
                    ok = False
                try:
                    self.ims.ack(ev['id'], ok)
                except OSError:
                    pass

    def flush(self, at):
        now = time.monotonic()
        if self.input and not self.input.get('expired') and now >= self.input['deadline']:
            # Drain late private PDU bytes through Ctrl-Z/ESC, never AT logging.
            self.input['expired'] = True
            self.input['hex'].clear()
            self.cms_error(at, 500)
        if self.pending and self.pending['at'] is not None and now >= self.pending['deadline']:
            self.cms_error(self.pending['at'], 500)
            self.pending['at'] = None  # retain correlation for a possible late final reply
        if not self.enabled:
            return
        while self.notifications:
            index = self.notifications[0]
            if index in self.records:
                at.out('\r\n+CMTI: "ME",%d\r\n' % index)
            self.notifications.pop(0)

    @staticmethod
    def cms_error(at, code):
        at.out('\r\n+CMS ERROR: %d\r\n' % code)

    def input_byte(self, ch, at):
        """Consume prompt bytes before AT parsing/logging; never echo/log a PDU."""
        if self.input is None:
            return False
        state = self.input
        if state.get('expired'):
            if ch in (26, 27):
                self.input = None
            return True
        if ch == 27:
            self.input = None
            at.ok()
        elif ch == 26:
            self.input = None
            try:
                if state['invalid'] or not state['hex'] or len(state['hex']) % 2:
                    raise ValueError('invalid hex PDU')
                pdu = bytes.fromhex(state['hex'].decode('ascii'))
                smsc = None
                if pdu and pdu[0] == 0 and self.request is not None:
                    reply = self.request(4, 10, 2)
                    if reply and 1 <= reply[0] <= 12 and len(reply) >= reply[0] + 1:
                        smsc = bytes(reply[:reply[0] + 1])
                pdu = submit_pdu(pdu, state['length'], smsc)
            except LookupError:
                self.cms_error(at, 330)
                return True
            except (ValueError, UnicodeError):
                self.cms_error(at, 304)
                return True
            except Exception:
                self.cms_error(at, 500)
                return True
            # Persist before submitting: after timeout/crash never blindly resend.
            try:
                self.persist(self.records, self.next_id, inflight=True)
                if self.ims is not None and self.ims.registered:
                    # CS is denied on this network: SMS over IP (TS 24.341) through imsd.
                    head = 1 + pdu[0]
                    rid = self.ims.send(pdu[:head], pdu[head:])
                    self.pending = {'ims': rid, 'mr': pdu[head + 1], 'at': at, 'deadline': time.monotonic() + 75}
                else:
                    seq = self.tx(4, 1, 1, bytes([2, 2, 0, len(pdu)]) + pdu)
                    self.pending = {'seq': seq, 'at': at, 'deadline': time.monotonic() + 55}
            except Exception:
                self.cms_error(at, 500)
        elif ch not in (10, 13):
            if ch not in b'0123456789abcdefABCDEF' or len(state['hex']) >= 510:
                state['invalid'] = True
            else:
                state['hex'].append(ch)
        return True

    @staticmethod
    def tpdu_length(record):
        pdu = bytes.fromhex(record['pdu'])
        return len(pdu) - 1 - pdu[0]

    def handle(self, c, at):
        """Handle only SMS commands; no fabricated send success."""
        def error(code=303):
            at.out('\r\n+CMS ERROR: %d\r\n' % code)
            return True
        if re.fullmatch(r'\+CMGS=\d+', c):
            length = int(c[6:])
            if self.input is not None or self.tx_inflight or self.pending is not None:
                return error(512)
            if not 7 <= length <= 244:
                return error(304)
            self.input = {'length': length, 'hex': bytearray(), 'invalid': False,
                          'deadline': time.monotonic() + 30}
            at.out('\r\n> ')
        elif c == '+CMGF=?':
            at.lines(['+CMGF: (0)'])
        elif c == '+CMGF?':
            at.lines(['+CMGF: 0'])
        elif c == '+CMGF=0':
            at.ok()
        elif c.startswith('+CMGF='):
            return error()
        elif c == '+CPMS=?':
            # ME is the durable AP-side store, not the SIM memory.
            at.lines(['+CPMS: ("ME"),("ME"),("ME")'])
        elif c == '+CPMS?':
            part = '"ME",%d,%d' % (len(self.records), self.LIMIT)
            at.lines(['+CPMS: ' + ','.join([part] * 3)])
        elif c.startswith('+CPMS='):
            memories = c[6:].split(',')
            if not 1 <= len(memories) <= 3 or any(x != '"ME"' for x in memories):
                return error(302)
            part = '%d,%d' % (len(self.records), self.LIMIT)
            at.lines(['+CPMS: ' + ','.join([part] * 3)])
        elif c == '+CNMI=?':
            at.lines(['+CNMI: (0,2),(0,1),(0,2),(0,1),(0)'])
        elif c == '+CNMI?':
            at.lines(['+CNMI: ' + ','.join(map(str, self.cnmi))])
        elif c.startswith('+CNMI='):
            try:
                values = tuple(map(int, c[6:].split(',')))
            except ValueError:
                return error()
            if len(values) != 5 or any(v not in allowed for v, allowed in zip(
                    values, ((0, 2), (0, 1), (0, 2), (0, 1), (0,)))):
                return error()
            if values[0] == 2 and values[1] == 1 and not self.ready_sent:
                self.tx(4, 7, 3, b'')
                self.ready_sent = True
            self.cnmi = values
            self.enabled = values[0] == 2 and values[1] == 1
            at.ok()
        elif re.fullmatch(r'\+CMGL=[014]', c):
            status = int(c[-1])
            result = []
            for index, record in sorted(self.records.items()):
                if status == 4 or record['status'] == status:
                    result.extend(['+CMGL: %d,%d,,%d' %
                                   (index, record['status'], self.tpdu_length(record)), record['pdu']])
            # Listing does not mutate the unread flag.
            at.lines(result)
        elif re.fullmatch(r'\+CMGR=\d+', c):
            index = int(c[6:])
            record = self.records.get(index)
            if record is None:
                return error(321)
            result = ['+CMGR: %d,,%d' % (record['status'], self.tpdu_length(record)), record['pdu']]
            records = dict(self.records)
            records[index] = dict(record, status=1)
            self.persist(records, self.next_id)
            at.lines(result)
        elif re.fullmatch(r'\+CMGD=\d+(,0)?', c):
            index = int(c[6:].split(',')[0])
            if index not in self.records:
                return error(321)
            records = dict(self.records)
            del records[index]
            self.persist(records, self.next_id)
            at.ok()
        elif c.startswith(('+CMGS', '+CMGW', '+CMSS', '+CMGD', '+CMGL', '+CMGR', '+CNMA')):
            return error()  # SIM storage sends/writes/bulk deletion not implemented
        else:
            return False
        return True
