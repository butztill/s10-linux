#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic SMS only: no device, real subscriber or network access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from s10sms import SMSBridge, incoming, submit_pdu

# SMSC absent, GSM SMS-DELIVER with synthetic origin 1234 and empty user data.
PDU = bytes.fromhex('00000481214300006201010000000000')


def envelope(pdu=PDU, tpid=7):
    return bytes([2, 1, 0, 0, tpid, len(pdu)]) + pdu


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'sms.json'
        self.tx = Mock()
        self.sms = SMSBridge(self.tx, self.path)
        self.at = Mock()

    def receive(self, payload=None):
        self.sms.on_ipc(4, 2, 3, envelope() if payload is None else payload)

    def test_durable_before_ack_and_private(self):
        def ack(*args):
            restored = SMSBridge(Mock(), self.path)
            self.assertEqual(restored.records[1]['pdu'], PDU.hex().upper())
        self.tx.side_effect = ack
        self.receive()
        self.tx.assert_called_once_with(4, 6, 1, bytes([2, 0, 0, 7, 0]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_no_ack_on_disk_failure(self):
        with patch('s10sms.os.replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.receive()
        self.tx.assert_not_called()
        self.assertFalse(self.sms.records)

    def test_bounds_and_other_formats(self):
        for payload in (b'', bytes([1]) + envelope()[1:], envelope()[:-1], envelope() + b'X',
                        envelope(bytes([255, 0])), envelope(bytes([0, 1]))):
            with self.assertRaises(ValueError):
                incoming(payload)
        self.sms.on_ipc(4, 2, 2, envelope())
        self.tx.assert_not_called()

    def test_retransmit_is_deduplicated_across_restart(self):
        self.receive()
        self.sms = SMSBridge(self.tx, self.path)
        self.receive()
        self.assertEqual(len(self.sms.records), 1)
        self.assertEqual(self.tx.call_count, 2)

    def test_notifications_and_ready(self):
        self.receive()
        self.sms.flush(self.at)
        self.at.out.assert_not_called()
        self.sms.handle('+CNMI=2,1,2,1,0', self.at)
        self.sms.flush(self.at)
        self.at.out.assert_called_once_with('\r\n+CMTI: "ME",1\r\n')
        self.sms.handle('+CNMI=2,1,2,1,0', self.at)
        self.assertEqual(sum(c.args == (4, 7, 3, b'') for c in self.tx.call_args_list), 1)

    def test_list_read_and_delete_survive_restart(self):
        self.receive()
        self.sms.handle('+CMGL=4', self.at)
        self.at.lines.assert_called_with(['+CMGL: 1,0,,15', PDU.hex().upper()])
        self.sms.handle('+CMGR=1', self.at)
        self.assertEqual(SMSBridge(Mock(), self.path).records[1]['status'], 1)
        self.sms.handle('+CMGD=1', self.at)
        self.assertFalse(SMSBridge(Mock(), self.path).records)

    def test_capacity_never_acknowledges_unstored_message(self):
        self.sms.LIMIT = 0
        with self.assertRaises(OSError):
            self.receive()
        self.tx.assert_not_called()

    def test_no_fake_send_or_bulk_delete_success(self):
        for cmd in ('+CMGW=10', '+CMSS=1', '+CMGD=1,4', '+CMGF=1'):
            self.assertTrue(self.sms.handle(cmd, self.at))
        self.at.ok.assert_not_called()
        self.tx.assert_not_called()

    def test_store_corruption_is_not_silently_overwritten(self):
        self.path.write_text('not json')
        with self.assertRaises(ValueError):
            SMSBridge(self.tx, self.path)
        self.assertEqual(self.path.read_text(), 'not json')

    # Synthetic destination and SMSC. Nothing in these tests reaches a modem.
    SUBMIT = bytes.fromhex('02912101000481214300000141')

    def send(self, pdu=None):
        pdu = self.SUBMIT if pdu is None else pdu
        self.sms.handle('+CMGS=%d' % (len(pdu) - 1 - pdu[0]), self.at)
        for ch in pdu.hex().encode() + b'\x1a':
            self.assertTrue(self.sms.input_byte(ch, self.at))

    def test_send_waits_for_correlated_final_reply(self):
        self.tx.return_value = 17
        self.send()
        self.tx.assert_called_once_with(4, 1, 1, bytes([2, 2, 0, len(self.SUBMIT)]) + self.SUBMIT)
        self.at.lines.assert_not_called()
        self.assertTrue(SMSBridge(Mock(), self.path).tx_inflight)
        for main, sub, typ, ack, data in ((4, 1, 2, 18, b'\x02\0\0\x07\0'),
                                         (4, 1, 2, 17, b'\x02\0\0\x07\x01'),
                                         (128, 1, 2, 17, b'\0')):
            self.sms.on_ipc(main, sub, typ, data, ack=ack)
        self.at.lines.assert_not_called()
        self.sms.on_ipc(4, 1, 2, b'\x02\0\0\x07\0', ack=17)
        self.at.lines.assert_called_once_with(['+CMGS: 7'])
        self.assertFalse(SMSBridge(Mock(), self.path).tx_inflight)

    def test_send_failure_and_late_reply(self):
        self.tx.return_value = 9
        self.send()
        with patch('s10sms.time.monotonic', return_value=self.sms.pending['deadline'] + 1):
            self.sms.flush(self.at)
        self.assertTrue(self.sms.tx_inflight)
        self.at.reset_mock()
        self.sms.handle('+CMGS=10', self.at)
        self.at.out.assert_called_once_with('\r\n+CMS ERROR: 512\r\n')
        self.at.reset_mock()
        self.sms.on_ipc(4, 1, 2, b'\x02\x12\0\x07\0', ack=9)
        self.at.out.assert_not_called()
        self.at.lines.assert_not_called()
        self.assertFalse(self.sms.tx_inflight)
        self.send()
        self.sms.on_ipc(4, 1, 2, b'\x02\x12\0\x07\0', ack=9)
        self.at.out.assert_called_with('\r\n+CMS ERROR: 500\r\n')

    def test_hardware_async_final_has_no_request_ack(self):
        self.tx.return_value = 91
        self.send()
        self.sms.on_ipc(4, 1, 3, b'\x02\0\0\x07\0', seq=125, ack=0)
        self.at.lines.assert_called_once_with(['+CMGS: 7'])
        self.assertFalse(self.sms.tx_inflight)
        self.at.reset_mock()
        # Repeated unsolicited result cannot complete another absent send.
        self.sms.on_ipc(4, 1, 3, b'\x02\0\0\x07\0', seq=125, ack=0)
        self.at.lines.assert_not_called()

    def test_no_send_on_disk_failure(self):
        with patch.object(self.sms, 'persist', side_effect=OSError()):
            self.send()
        self.tx.assert_not_called()
        self.at.lines.assert_not_called()

    def test_uncertain_transmit_blocks_after_reload(self):
        self.tx.side_effect = OSError()
        self.send()
        restarted = SMSBridge(Mock(), self.path)
        self.assertTrue(restarted.tx_inflight)
        restarted.handle('+CMGS=10', self.at)
        self.at.out.assert_called_with('\r\n+CMS ERROR: 512\r\n')

    def test_default_smsc_and_no_fabricated_default(self):
        pdu = b'\0' + self.SUBMIT[3:]
        self.send(pdu)
        self.tx.assert_not_called()
        self.at.out.assert_called_with('\r\n+CMS ERROR: 330\r\n')
        self.sms.request = Mock(return_value=self.SUBMIT[:3] + bytes(9))
        self.send(pdu)
        self.assertEqual(self.tx.call_args.args[3][4:], self.SUBMIT)

    def test_smsc_request_error_is_not_private_exception(self):
        self.sms.request = Mock(side_effect=OSError('synthetic'))
        self.send(b'\0' + self.SUBMIT[3:])
        self.tx.assert_not_called()
        self.at.out.assert_called_with('\r\n+CMS ERROR: 500\r\n')

    def test_prompt_cancel_invalid_and_timeout_drain(self):
        for terminator in (b'\x1b', b'ZZ\x1a'):
            self.sms.handle('+CMGS=10', self.at)
            for ch in terminator:
                self.assertTrue(self.sms.input_byte(ch, self.at))
            self.assertIsNone(self.sms.input)
        self.sms.handle('+CMGS=10', self.at)
        with patch('s10sms.time.monotonic', return_value=self.sms.input['deadline'] + 1):
            self.sms.flush(self.at)
        for ch in b'0123456789abcdef\r\n\x1a':
            self.assertTrue(self.sms.input_byte(ch, self.at))
        self.assertIsNone(self.sms.input)
        self.tx.assert_not_called()

    def test_submit_alphabets_headers_and_lengths(self):
        # Plain GSM7, UCS2 and concatenated GSM7 all retain exact bytes.
        for tpdu in ('01000481214300000141', '0100048121430008020041',
                     '41000481214300000805000301020141'):
            pdu = self.SUBMIT[:3] + bytes.fromhex(tpdu)
            self.assertEqual(submit_pdu(pdu, len(pdu) - 3), pdu)
        for pdu in (self.SUBMIT[:-1], self.SUBMIT + b'\0',
                    self.SUBMIT[:3] + bytes.fromhex('410004812143000001ff')):
            with self.assertRaises(ValueError):
                submit_pdu(pdu, len(pdu) - 3)


    def test_ims_incoming_is_stored_before_ack(self):
        ims = Mock(registered=True)
        deliver = {'op': 'deliver', 'id': 5, 'smsc': '00', 'tpdu': PDU[1:].hex()}
        ims.poll.return_value = [deliver]
        sms = SMSBridge(Mock(), self.path, ims=ims)
        ims.ack.side_effect = lambda i, ok: self.assertEqual(
            SMSBridge(Mock(), self.path).records[1]['tpid'], 256)
        sms.poll_ims()
        ims.ack.assert_called_once_with(5, True)
        self.assertEqual(sms.notifications, [1])

    def test_ims_send_result_maps_to_cmgs(self):
        ims = Mock(registered=True)
        ims.send.return_value = 3
        ims.poll.return_value = []
        sms = SMSBridge(Mock(), self.path, ims=ims)
        sms.pending = {'ims': 3, 'mr': 9, 'at': self.at, 'deadline': 1e18}
        ims.poll.return_value = [{'op': 'sent', 'id': 3, 'ok': True, 'detail': ''}]
        sms.poll_ims()
        self.at.lines.assert_called_once_with(['+CMGS: 9'])
        self.assertIsNone(sms.pending)


unittest.main()
