# SPDX-License-Identifier: GPL-2.0-only
"""Check generated CRC tables against the complete previous protocol table."""
import ast
import hashlib
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SensorCRCTests(unittest.TestCase):
    def test_both_copies_preserve_all_256_transport_values(self):
        for name in ('userspace/sensors/s10shub.py', 'phone-rootfs/usr/local/lib/s10-sensors/s10shub.py'):
            tree = ast.parse((ROOT/name).read_text())
            function = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == '_crc8_table')
            namespace = {}
            exec(compile(ast.Module(body=[function], type_ignores=[]), name, 'exec'), namespace)
            table = namespace['_crc8_table']()
            self.assertEqual(len(table), 256)
            self.assertEqual(hashlib.sha256(table).hexdigest(), EXPECTED)

EXPECTED = '9e215ef0372d169ae9b9ac210908fc0eaf6b0600408513a204d7c6bb74511412'

if __name__ == '__main__':
    unittest.main()
