#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise actual daemon classes with synthetic IPC; no modem or SIM access."""
import ast
import ipaddress
import os
from pathlib import Path
import struct
import tempfile
import time
import traceback
import unittest
from unittest.mock import Mock, patch
from s10sms import SMSBridge

source = Path(__file__).with_name('s10modemd.py').read_text()
tree = ast.parse(source)
tree.body = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ('Modem', 'AT')]
ns = {'struct': struct, 'time': time, 'traceback': traceback,
      'RUN': '/unused-test-path', 'log': lambda *a: None}
exec(compile(tree, 'actual-modem-classes', 'exec'), ns)
Modem, AT = ns['Modem'], ns['AT']


class Tests(unittest.TestCase):
    def setUp(self):
        self.m = Modem.__new__(Modem)
        self.m.pdp, self.m.trace = {}, []
        self.m.ipc = Mock()
        # Production constructs the voice dispatcher before the AT interface.
        # Data-path tests need its non-voice pass-through, not a missing field
        # that the daemon's exception handler converts into a generic ERROR.
        self.m.voice = Mock()
        self.m.voice.handle.return_value = False
        self.m.request = Mock(side_effect=AssertionError('SET must not await RESP'))
        self.at = AT.__new__(AT)
        self.at.m, self.at.pdp, self.at.cmee = self.m, {}, 0
        self.at.lines, self.at.err = Mock(), Mock()

    def test_ipv6_notifications(self):
        p = bytearray(0x9c)
        p[0] = 1
        struct.pack_into('<H', p, 1, 0x2040)
        for offset, ip in ((0x23, '2001:db8::2'), (0x63, '2001:db8::53'), (0x73, '2001:db8::54')):
            p[offset:offset+16] = ipaddress.IPv6Address(ip).packed
        struct.pack_into('<I', p, 0x98, 1500 << 16)
        self.m.on_ipc(0x0d, 9, 3, 0, 0, p)
        self.m.on_ipc(0x0d, 0x10, 3, 0, 0, bytes([1, 1, 0]))
        self.at.handle('AT+S10IPCFG=1')
        self.at.lines.assert_called_once_with([
            '+S10IPCFG: 6,2001:db8::2,64,,2001:db8::53,2001:db8::54,1500'])
        self.at.handle('AT+CGCONTRDP=1')  # IPv6-only must not access absent prefix/gw keys.
        self.at.err.assert_not_called()

    def test_disconnected_has_no_stale_config(self):
        self.m.pdp[1] = {'status': 0, 'ip6': '2001:db8::2'}
        self.at.handle('AT+S10IPCFG=1')
        self.at.err.assert_called_once_with(30)
        self.at.lines.assert_not_called()

    def test_async_binary_commands(self):
        self.m.define_pdp(1, 'example.invalid', 6)
        args = self.m.ipc.tx.call_args.args
        self.assertEqual(args[:3], (0x0d, 1, 3))
        self.assertEqual(len(args[3]), 149 - 7)
        self.assertEqual(args[3][:3], bytes([1, 1, 6]))
        self.m.activate_pdp(1)
        args = self.m.ipc.tx.call_args.args
        self.assertEqual(args[:3], (0x0d, 4, 3))
        self.assertEqual(len(args[3]), 248 - 7)
        self.assertEqual(args[3][:2], bytes([1, 1]))
        self.m.deactivate_pdp(1)
        self.assertEqual(self.m.ipc.tx.call_args.args[3][:2], bytes([0, 1]))

    def test_registration(self):
        for raw, expected in ((2, 1), (3, 2), (4, 3), (6, 5), (7, 4), (9, 3), (10, 4)):
            self.m.reg = {3: bytes([0x21, 3, raw])}
            self.assertEqual(self.m.reg_stat(3), expected)

    def test_only_the_implemented_data_channel_is_advertised(self):
        self.at.handle('AT+CGDCONT=?')
        self.assertIn('(1-1)', self.at.lines.call_args.args[0][0])
        self.at.handle('AT+CGDCONT=2,"IPV4V6","example.invalid"')
        self.at.err.assert_called_once_with(4)

    def test_teardown_waits_for_the_real_notification(self):
        self.m.pdp[1] = {'status': 1}
        def pump():
            if self.m.pump.call_count == 2:
                self.m.pdp[1]['status'] = 3
        self.m.pump = Mock(side_effect=pump)
        self.m.wait_evt = Mock()
        self.assertTrue(self.m.wait_pdp_down(1))
        self.assertEqual(self.m.pump.call_count, 2)
        self.assertFalse(self.m.wait_pdp_down(1, timeout=0))

    def test_identity_notification_payloads_are_not_logged(self):
        logger = Mock()
        old = ns['log']
        ns['log'] = logger
        try:
            for main in (2, 4, 5, 10):
                self.m.on_ipc(main, 3, 3, 0, 0, bytes([1, 2, 3, 4]))
                self.assertIn('payload omitted', logger.call_args.args[0])
                self.assertNotIn('01 02 03 04', logger.call_args.args[0])
                self.assertEqual(self.m.trace[-1][-1], b'')
        finally:
            ns['log'] = old

    def test_imsi_decoder_fails_closed_without_logging(self):
        value = b'1' * 15  # synthetic; never print subscriber identifiers
        self.m.request = Mock(return_value=value + b'\0')
        self.assertEqual(self.m.get_imsi(), value.decode())
        self.m.request.return_value = bytes([15]) + value
        self.assertEqual(self.m.get_imsi(), value.decode())
        for invalid in (bytes(16), b'0' * 15, bytes([255]) * 15, b'12x34', b'1' * 17):
            self.m.request.return_value = invalid
            self.assertIsNone(self.m.get_imsi())

    def test_sms_prompt_never_echoes_or_logs_pdu(self):
        with tempfile.TemporaryDirectory() as directory:
            self.m.sms = SMSBridge(Mock(return_value=7), Path(directory) / 'sms.json')
            self.at.buf, self.at.echo, self.at.master = b'', True, 123
            self.at.out = Mock()
            logger = Mock()
            old = ns['log']
            ns['log'], ns['os'] = logger, os
            try:
                with patch('os.write') as write:
                    self.at.feed(b'AT+CMGS=10\r02912101000481214300000141\x1a')
                echoed = b''.join(c.args[1] for c in write.call_args_list)
                self.assertNotIn(b'029121', echoed)
                self.assertEqual(logger.call_args.args, ('AT<', '<private command omitted>'))
                self.assertIsNotNone(self.m.sms.pending)
            finally:
                ns['log'] = old


unittest.main()
