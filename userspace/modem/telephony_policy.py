# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in G973FXXSGHWC2 DeviceCapa transaction; no SMS/RAT/IMS commands.

CP 4103e796: SET 0a/2d [EUTRAN 0..3, UTRAN ignored]. GET 4103e71e
returns [registry 0x221, 0]. Generic response: 80/01 RESP, original tag,
LE16 result (8000 accepted). Acceptance queues NS_PREFERED_DOMAIN_SET_REQ;
it does NOT establish registration, IMS availability or SMS delivery.
"""
import json
import os
from pathlib import Path
import tempfile
import time

BUILD = 'G973FXXSGHWC2'
VALID_STATES = (2, 6)  # stock RIL: home, roaming


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def pair(value):
    return (isinstance(value, (bytes, bytearray, list, tuple)) and len(value) == 2
            and type(value[0]) is int and 0 <= value[0] < 4
            and type(value[1]) is int and value[1] == 0)


class TelephonyPolicy:
    def __init__(self, directory, logger=lambda *args: None):
        self.directory = Path(directory)
        self.journal = self.directory / 'telephony-policy-state.json'
        self.log = logger
        self.config = None
        self.state = 'disabled'
        self.attempted = False
        self.original = None
        self.applied_at = None
        self.busy = False
        self.reset_during_transaction = False
        self.inhibited = False
        self.set_issued = False
        try:
            cfg = json.loads((self.directory / 'telephony-policy.json').read_text())
            if not isinstance(cfg, dict) or type(cfg.get('enabled')) is not bool:
                raise ValueError('enabled')
            if not cfg['enabled']:
                return
            target = [cfg.get('eutran_voice_preference'), cfg.get('utran_voice_preference')]
            if not pair(target) or cfg.get('firmware') != BUILD:
                raise ValueError('policy')
            if set(cfg) != {'enabled', 'firmware', 'eutran_voice_preference', 'utran_voice_preference'}:
                raise ValueError('unknown option')
            self.config = bytes(target)
            self.state = 'waiting'
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.state = 'invalid-config'
            self.log('Telephony policy invalid; no DeviceCapa commands')

    def record(self, state, **enums):
        self.state = state
        self.log('Telephony policy', state, enums)

    def radio_changed(self):
        # A PWR 01/07 notification is a reason to re-read, not proof of CP reset.
        if self.busy:
            self.reset_during_transaction = True
        else:
            self.attempted = False
            self.applied_at = None

    def disable(self):
        # A failed experiment must stay off across daemon/phone restarts.
        self.inhibited = True
        try:
            atomic_json(self.directory / 'telephony-policy.json',
                        {'enabled': False, 'firmware': BUILD,
                         'eutran_voice_preference': self.config[0],
                         'utran_voice_preference': 0})
        except OSError:
            self.log('Telephony policy disable could not be saved; rollback journal retained')

    def exchange(self, modem, typ, payload=b'', timeout=3):
        seq = modem.ipc.tx(0x0a, 0x2d, typ, payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            modem.pump()  # continue RFS and data servicing, including during SET
            response = modem.resp.pop(seq, None)
            if response is not None:
                main, sub, reply = response
                if typ == 2 and (main, sub) == (0x0a, 0x2d):
                    return bytes(reply)
                if (main, sub) == (0x80, 1) and len(reply) == 5 and reply[:3] == bytes([10, 45, typ]):
                    return int.from_bytes(reply[3:5], 'little')
                # Same ack but wrong tag is not evidence for this transaction.
            modem.wait_evt(0.02)
        return None

    def get(self, modem):
        reply = self.exchange(modem, 2)
        return reply if pair(reply) else None

    def remember(self, current):
        if self.journal.exists():
            saved = json.loads(self.journal.read_text())
            if (not isinstance(saved, dict) or saved.get('firmware') != BUILD
                    or saved.get('version') != 1 or not pair(saved.get('original'))
                    or not pair(saved.get('target'))):
                raise ValueError('invalid journal')
            if saved['target'] != list(self.config) or list(current) not in (saved['original'], saved['target']):
                raise ValueError('policy changed; explicit restore required')
            self.original = bytes(saved['original'])
        else:
            atomic_json(self.journal, {'version': 1, 'firmware': BUILD,
                                      'original': list(current), 'target': list(self.config)})
            self.original = current

    def restore(self, modem):
        """Restore only our journalled setting; never overwrite an unrelated change."""
        if not pair(self.original):
            self.record('restore-unavailable')
            return False
        current = self.get(modem)
        if current == self.original:
            self.record('restored', preference=current[0])
            return True
        if current != self.config:
            self.record('restore-conflict')
            return False
        result = self.exchange(modem, 3, self.original)
        readback = self.get(modem)
        if result == 0x8000 and readback == self.original:
            self.record('restored', preference=readback[0])
            return True
        self.record('restore-unconfirmed', result=result)
        return False

    def consider(self, modem):
        if self.config is None or self.inhibited:
            return
        if self.attempted:
            if self.applied_at is not None:
                packet = modem.reg.get(3)
                non_lte = packet is not None and len(packet) >= 3 and packet[0] != 0x21
                lost = (packet is None or len(packet) < 3 or packet[2] not in VALID_STATES)
                if non_lte or (lost and time.monotonic() - self.applied_at > 90):
                    self.busy = True
                    try:
                        self.disable()
                        self.restore(modem)
                    finally:
                        self.applied_at = None
                        self.busy = False
            return
        if modem.pin != 0 or modem.cfun != 1:
            return
        if modem.sms is not None and modem.sms.tx_inflight:
            return
        packet = modem.reg.get(3)
        if packet is None or len(packet) < 3 or packet[0] != 0x21 or packet[2] not in VALID_STATES:
            return  # first application is allowed only on registered LTE
        self.attempted = True
        if not modem.version or BUILD not in modem.version:
            self.record('firmware-mismatch')
            return
        self.busy = True
        self.set_issued = False
        self.reset_during_transaction = False
        try:
            current = self.get(modem)
            if current is None:
                self.record('baseline-unavailable')
                return
            try:
                self.remember(current)  # durable rollback record BEFORE any SET
            except (OSError, ValueError, TypeError):
                self.record('journal-error')
                return
            if self.reset_during_transaction:
                self.record('radio-changed-before-set')
                return
            if current == self.config:
                self.record('already-configured', preference=current[0])
                self.applied_at = time.monotonic()
                return
            self.set_issued = True
            result = self.exchange(modem, 3, self.config)
            readback = self.get(modem)
            if result == 0x8000 and readback == self.config and not self.reset_during_transaction:
                self.record('applied', preference=readback[0])
                self.applied_at = time.monotonic()
            else:
                self.record('unconfirmed' if result is None else 'not-applied', result=result)
                # Firmware may store before returning an error: check and undo.
                self.disable()
                self.restore(modem)
        except (OSError, ValueError, TypeError):
            self.record('transport-error')
            # A SET may already have reached CP. Keep the durable journal;
            # do not guess whether it applied or retry on this radio epoch.
            if self.set_issued:
                self.disable()
        finally:
            self.busy = False
