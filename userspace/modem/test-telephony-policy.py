#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual DeviceCapa policy with a synthetic IPC transport; no phone/SIM."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telephony_policy import BUILD, TelephonyPolicy


class Modem:
    def __init__(self):
        self.ipc = self
        self.seq = 0
        self.resp = {}
        self.events = []
        self.commands = []
        self.current = b'\x03\x00'
        self.pin, self.cfun, self.sms, self.version = 0, 1, None, BUILD
        self.reg = {3: bytes([33, 3, 2])}
        self.now = 0
        self.results = []
        self.get_values = []
        self.before_reply = None
        self.sent_journal = None

    def tx(self, main, sub, typ, payload):
        self.seq += 1
        self.commands.append((main, sub, typ, payload))
        if typ == 2:
            reply = self.get_values.pop(0) if self.get_values else self.current
            tag = (main, sub)
        else:
            if self.sent_journal:
                self.sent_journal()  # inspect ordering at exact TX boundary
            self.current = payload
            result = self.results.pop(0) if self.results else 0x8000
            reply = None if result is None else bytes([main, sub, typ]) + result.to_bytes(2, 'little')
            tag = (0x80, 1)
        if reply is not None:
            self.events.append((self.seq, (*tag, reply)))
        return self.seq

    def pump(self):
        if self.before_reply:
            callback, self.before_reply = self.before_reply, None
            callback(self)
        if self.events:
            seq, response = self.events.pop(0)
            self.resp[seq] = response

    def wait_evt(self, timeout):
        self.now += timeout


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.modem = Modem()
        self.clock = patch('telephony_policy.time.monotonic', lambda: self.modem.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.logs = []

    def policy(self, **changes):
        config = dict(enabled=True, firmware=BUILD, eutran_voice_preference=0, utran_voice_preference=0)
        config.update(changes)
        (self.directory / 'telephony-policy.json').write_text(json.dumps(config))
        return TelephonyPolicy(self.directory, lambda *args: self.logs.append(args))

    def test_missing_and_disabled_do_not_transmit(self):
        for policy in (TelephonyPolicy(self.directory), self.policy(enabled=False)):
            policy.consider(self.modem)
        self.assertEqual(self.modem.commands, [])

    def test_invalid_configuration_fails_closed(self):
        for changes in ({'eutran_voice_preference': 4}, {'eutran_voice_preference': True},
                        {'utran_voice_preference': 1}, {'firmware': 'other'},
                        {'enabled': 1}, {'unexpected': 0}):
            policy = self.policy(**changes)
            self.assertEqual(policy.state, 'invalid-config')
            policy.consider(self.modem)
        self.assertEqual(self.modem.commands, [])

    def test_malformed_json_fails_closed(self):
        (self.directory / 'telephony-policy.json').write_text('{')
        policy = TelephonyPolicy(self.directory)
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'invalid-config')
        self.assertEqual(self.modem.commands, [])

    def test_wait_for_unlocked_sim_and_registered_lte(self):
        policy = self.policy()
        for pin, cfun, reg in ((1, 1, b'\x21\x03\x02'), (0, 4, b'\x21\x03\x02'),
                               (0, 1, b'\x01\x03\x02'), (0, 1, b'\x21\x03\x04')):
            self.modem.pin, self.modem.cfun, self.modem.reg[3] = pin, cfun, reg
            policy.consider(self.modem)
        self.assertEqual(self.modem.commands, [])
        self.modem.pin, self.modem.cfun, self.modem.reg[3] = 0, 1, b'\x21\x03\x02'
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'applied')

    def test_firmware_mismatch_never_sends(self):
        policy = self.policy()
        self.modem.version = 'other'
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'firmware-mismatch')
        self.assertEqual(self.modem.commands, [])

    def test_no_change_while_sms_is_uncertain(self):
        class SMS:
            tx_inflight = True
        self.modem.sms = SMS()
        self.policy().consider(self.modem)
        self.assertEqual(self.modem.commands, [])

    def test_exact_packet_and_durable_rollback_before_set(self):
        policy = self.policy()
        def journal():
            saved = json.loads(policy.journal.read_text())
            self.assertEqual(saved['original'], [3, 0])
            self.assertEqual(saved['target'], [0, 0])
            self.assertEqual(policy.journal.stat().st_mode & 0o777, 0o600)
        self.modem.sent_journal = journal
        policy.consider(self.modem)
        self.assertEqual(self.modem.commands, [(10, 45, 2, b''), (10, 45, 3, b'\0\0'), (10, 45, 2, b'')])
        self.assertEqual(policy.state, 'applied')
        for _ in range(5):
            policy.consider(self.modem)
        self.assertEqual(len(self.modem.commands), 3)

    def test_bad_baseline_prevents_set(self):
        for value in (b'\x04\0', b'\0\x01', b'\0', b'\0\0\0'):
            self.modem.current = value
            self.policy().consider(self.modem)
        self.assertTrue(all(command[2] == 2 for command in self.modem.commands))

    def test_journal_failure_prevents_set(self):
        policy = self.policy()
        with patch('telephony_policy.atomic_json', side_effect=OSError):
            policy.consider(self.modem)
        self.assertEqual(policy.state, 'journal-error')
        self.assertTrue(all(command[2] == 2 for command in self.modem.commands))

    def test_rejection_restores_stored_value(self):
        policy = self.policy()
        self.modem.results = [0x8001, 0x8000]
        policy.consider(self.modem)
        self.assertEqual(self.modem.current, b'\x03\0')
        self.assertEqual(policy.state, 'restored')
        self.assertTrue(any(log[1] == 'not-applied' and log[2]['result'] == 0x8001 for log in self.logs))

    def test_lost_ack_not_treated_as_success_and_no_blind_retry(self):
        policy = self.policy()
        self.modem.results = [None, 0x8000]
        policy.consider(self.modem)
        self.assertTrue(any(log[1] == 'unconfirmed' for log in self.logs))
        self.assertEqual(self.modem.current, b'\x03\0')
        count = len(self.modem.commands)
        policy.consider(self.modem)
        self.assertEqual(len(self.modem.commands), count)

    def test_ack_without_matching_readback_is_not_applied(self):
        policy = self.policy()
        self.modem.get_values = [b'\x03\0', b'\x03\0']
        policy.consider(self.modem)
        self.assertTrue(any(log[1] == 'not-applied' for log in self.logs))
        self.assertEqual(self.modem.current, b'\x03\0')

    def test_wrong_ack_and_tags_are_ignored(self):
        policy = self.policy()
        self.modem.events = [(255, (10, 45, b'\0\0')), (1, (4, 1, b'\0\0'))]
        self.assertEqual(policy.get(self.modem), b'\x03\0')
        self.assertIn(255, self.modem.resp)

    def test_wrong_generic_original_tag_is_not_accepted(self):
        policy = self.policy()
        self.modem.before_reply = lambda modem: modem.events.insert(0, (1, (0x80, 1, b'\x0a\x2c\x03\0\x80')))
        self.assertEqual(policy.exchange(self.modem, 3, b'\0\0'), 0x8000)

    def test_radio_event_during_baseline_prevents_set(self):
        policy = self.policy()
        self.modem.before_reply = lambda modem: policy.radio_changed()
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'radio-changed-before-set')
        self.assertTrue(all(command[2] == 2 for command in self.modem.commands))

    def test_recheck_and_daemon_restart_do_not_rewrite_or_lose_original(self):
        policy = self.policy()
        policy.consider(self.modem)
        policy.radio_changed()
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'already-configured')
        restarted = TelephonyPolicy(self.directory)
        restarted.consider(self.modem)
        self.assertEqual(restarted.original, b'\x03\0')
        self.assertEqual(sum(command[2] == 3 for command in self.modem.commands), 1)

    def test_lte_loss_restores_original_without_selecting_rat(self):
        policy = self.policy()
        policy.consider(self.modem)
        self.modem.reg[3] = bytes([1, 3, 2])
        policy.consider(self.modem)
        self.assertEqual(self.modem.current, b'\x03\0')
        self.assertEqual(policy.state, 'restored')
        self.assertFalse(json.loads((self.directory / 'telephony-policy.json').read_text())['enabled'])
        restarted = TelephonyPolicy(self.directory)
        self.assertEqual(restarted.state, 'disabled')
        self.assertTrue(all(command[:2] == (10, 45) for command in self.modem.commands))

    def test_registration_grace_then_restore(self):
        policy = self.policy()
        policy.consider(self.modem)
        self.modem.reg[3] = b'\x21\x03\x04'
        self.modem.now += 30
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'applied')
        self.modem.now += 61
        policy.consider(self.modem)
        self.assertEqual(policy.state, 'restored')

    def test_no_overwrite_of_unrelated_external_setting(self):
        policy = self.policy()
        policy.consider(self.modem)
        self.modem.current = b'\x02\0'
        self.assertFalse(policy.restore(self.modem))
        self.assertEqual(policy.state, 'restore-conflict')
        self.assertEqual(self.modem.current, b'\x02\0')

    def test_changed_config_requires_explicit_restore(self):
        policy = self.policy()
        policy.consider(self.modem)
        changed = self.policy(eutran_voice_preference=2)
        changed.consider(self.modem)
        self.assertEqual(changed.state, 'journal-error')
        self.assertEqual(sum(command[2] == 3 for command in self.modem.commands), 1)


unittest.main()
