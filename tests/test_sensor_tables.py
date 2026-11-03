# SPDX-License-Identifier: GPL-2.0-only
"""Check explicit generator inputs/outputs and source-hash rejection."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SensorTableGeneratorTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('sensor_table_generator_test', ROOT/'scripts/prepare-s5k-tables.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(Path, 'write_text', side_effect=AssertionError('import must not write')):
            spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        reference, output = root/'reference', root/'output'
        reference.mkdir()
        data = b'const u32 test_global[] = {\n0x1000, 0x22, 0x01,\nI2C_MODE_DELAY, 5, 0,\n};\n'
        (reference/'fixture.h').write_bytes(data)
        return reference, output, data

    def test_import_is_read_only(self):
        self.load()

    def test_explicit_paths_generate_only_in_requested_directory(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference, output, data = self.fixture(root)
            with patch.object(module,'MODELS', {'test':('fixture.h',[('global','test_global')])}), patch.object(module,'SHA', {'fixture.h':hashlib.sha256(data).hexdigest()}), contextlib.redirect_stdout(io.StringIO()):
                module.main(['--reference-dir',str(reference),'--output-dir',str(output)])
            result = (output/'test-tables.h').read_text()
            self.assertIn('{ 0x1000, 0x0022, 1 }',result)
            self.assertIn('{ 0xffff, 0x0005, 0 }',result)
            self.assertEqual((reference/'fixture.h').read_bytes(),data)

    def test_changed_source_is_rejected_before_output_header(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as tmp:
            reference, output, data = self.fixture(Path(tmp))
            with patch.object(module,'MODELS', {'test':('fixture.h',[('global','test_global')])}), patch.object(module,'SHA', {'fixture.h':'0'*64}):
                with self.assertRaises(SystemExit):
                    module.main(['--reference-dir',str(reference),'--output-dir',str(output)])
            self.assertFalse((output/'test-tables.h').exists())


if __name__ == '__main__':
    unittest.main()
