# SPDX-License-Identifier: GPL-2.0-only
"""Check recovered source closure and transport bytes without modem access."""
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'userspace/modem'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoveredTransportTests(unittest.TestCase):
    def ipc(self):
        link = load('recovered_s10cp_test', SOURCE / 's10cp.py')
        with patch.dict(sys.modules, {'s10cp': link}):
            return load('recovered_ipc_test', SOURCE / 'ipc.py')

    def test_full_daemon_import_needs_no_device_or_network(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for p in SOURCE.glob('*.py'):
                shutil.copyfile(p, folder / p.name)
            with patch.dict(sys.modules), patch.object(sys, 'path', [temp, *sys.path]), \
                    patch('os.open', side_effect=AssertionError('device access during import')), \
                    patch.object(socket, 'socket', side_effect=AssertionError('network access during import')):
                module = load('source_closure_test', folder / 's10modemd.py')
                self.assertTrue(callable(module.Modem))
                self.assertTrue(callable(module.AT))
                # Log-file creation is confined to this disposable source copy.
                sys.modules['rfsd'].LOG.close()
                module.LOG.close()

    def test_ipc_tx_matches_wire_fixture(self):
        module = self.ipc()
        link = Mock()
        ipc = module.Ipc(link)
        self.assertEqual(ipc.tx(5, 3, 2, b'\x01\x02'), 1)
        link.send.assert_called_once_with(0xeb, b'\x09\x00\x01\x00\x05\x03\x02\x01\x02')

    def test_ipc_sequence_wraps_as_one_byte(self):
        module = self.ipc()
        link = Mock()
        ipc = module.Ipc(link)
        ipc.seq = 255
        self.assertEqual(ipc.tx(1, 9, 2), 0)
        self.assertEqual(link.send.call_args.args[1], b'\x07\x00\x00\x00\x01\x09\x02')

    def test_ipc_rx_decodes_complete_fixture_and_ignores_short_header(self):
        module = self.ipc()
        link = Mock()
        link.poll_dl.return_value = [b'\x09\x00\x14\x02\x0d\x09\x03\xaa\xbb', b'\x01']
        self.assertEqual(module.Ipc(link).rx(), [(0x0d, 9, 3, 20, 2, b'\xaa\xbb')])

    def ps_link(self):
        module = load('recovered_ps_link_test', SOURCE / 's10cp.py')
        link = module.Link.__new__(module.Link)
        link.ring = {0x0b: {'id': 11, 0: {'len': 4, 'bsz': 8, 'buf': 100, 'size_v': 200}}}
        link.rbp = Mock(side_effect=lambda direction, write, index: 1 if write else 0)
        link.wr, link.set_rbp, link.irq = Mock(), Mock(), Mock()
        return link

    def test_ps_packet_has_length_and_pdp_channel_without_sipc_header(self):
        link = self.ps_link()
        self.assertTrue(link.send_ps(b'\x60\x00\x00', pdp_ch=0x0a))
        self.assertEqual(link.wr.call_args_list[0].args, (108, b'\x60\x00\x00'))
        self.assertEqual(link.wr.call_args_list[1].args, (204, b'\x03\x00\x0a\x00'))
        link.set_rbp.assert_called_once_with(0, 1, 11, 2)
        link.irq.assert_called_once_with(0x81)

    def test_ps_full_ring_and_oversize_packet_do_not_write(self):
        for full in (False, True):
            link = self.ps_link()
            if full:
                link.rbp = Mock(side_effect=lambda direction, write, index: 3 if write else 0)
            self.assertFalse(link.send_ps(b'abc' if full else b'x' * 9))
            link.wr.assert_not_called()
            link.set_rbp.assert_not_called()
            link.irq.assert_not_called()

    def test_rfs_response_header_preserves_length_command_and_sequence(self):
        tree = ast.parse((SOURCE / 'rfsd.py').read_text())
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'hdr']
        namespace = {'struct': struct}
        exec(compile(tree, 'actual-rfs-header', 'exec'), namespace)
        self.assertEqual(namespace['hdr'](0x11, 0x2a, b'ab'), b'\x08\x00\x00\x00\x11\x2aab')


if __name__ == '__main__':
    unittest.main()
