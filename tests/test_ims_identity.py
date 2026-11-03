# SPDX-License-Identifier: GPL-2.0-only
"""Check identity configuration without SIM access or network activity."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class IMSIdentityTests(unittest.TestCase):
    def setUp(self):
        self.cp = types.SimpleNamespace(imei=Mock(return_value='1' * 15), imsi=Mock(return_value='0' * 15))
        spec = importlib.util.spec_from_file_location('s10_ims_client_test', ROOT/'userspace/vowifi/ims_client.py')
        self.client = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'usim_cp': self.cp}), patch.object(sys, 'path', list(sys.path)):
            spec.loader.exec_module(self.client)
        self.socket = Mock()
        self.patch = patch.object(self.client.socket, 'socket', self.socket)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_default_uses_modem_identity(self):
        result = self.client.Ims('::1', '::1')
        self.assertEqual(result.imei, '1' * 15)
        self.cp.imei.assert_called_once_with()

    def test_explicit_identity_does_not_query_modem_identity(self):
        result = self.client.Ims('::1', '::1', imei='2' * 15)
        self.assertEqual(result.imei, '2' * 15)
        self.cp.imei.assert_not_called()

    def test_unavailable_or_invalid_identity_fails_before_sim_and_network(self):
        for value in ('', '1' * 14, '1' * 16, 'x' * 15, 123):
            with self.subTest(value_type=type(value).__name__):
                self.cp.imei.return_value = value
                with self.assertRaises(ValueError) as caught:
                    self.client.Ims('::1', '::1')
                self.assertNotIn(str(value), str(caught.exception)) if value else None
        self.cp.imsi.assert_not_called()
        self.socket.assert_not_called()


if __name__ == '__main__':
    unittest.main()
