# SPDX-License-Identifier: GPL-2.0-only
"""Protect module staging from private workspace headers and path escapes."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('module_builder_test', ROOT/'scripts/build-external-modules.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ModuleSourceTests(unittest.TestCase):
    def test_kernel_compiler_metadata_accepts_tabs_without_copying_host_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            header=Path(temp)/'compile.h'
            header.write_text('#define LINUX_COMPILE_BY\t"private-owner"\n'
                              '#define LINUX_COMPILE_HOST\t"private-host"\n'
                              '#define LINUX_COMPILER\t\t"clang version 23.1.3, LLD 23.1.3"\n')
            self.assertEqual(builder.compiler_declaration(header),
                             '"clang version 23.1.3, LLD 23.1.3"')

    def test_stages_only_public_sources_and_records_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo'; source=root/'drivers/example';source.mkdir(parents=True)
            files={'Makefile':b'obj-m += example.o\n','example.c':b'int example;\n',
                   'public.h':b'/* public */\n','private-tables.h':b'not reviewed\n',
                   'example.mod.c':b'stale generated source\n'}
            for name,data in files.items():(source/name).write_bytes(data)
            selected={'drivers/example/'+name for name in files if name!='private-tables.h'}
            target=Path(temp)/'stage'
            records=builder.stage_module(root,'drivers/example',target,selected)
            self.assertEqual({p.name for p in target.iterdir()},{'Makefile','example.c','public.h'})
            self.assertEqual({x['path'] for x in records},
                             {'drivers/example/Makefile','drivers/example/example.c','drivers/example/public.h'})
            for entry in records:
                self.assertEqual(entry['sha256'],hashlib.sha256((root/entry['path']).read_bytes()).hexdigest())

    def test_rejects_selected_escape_or_symlink(self):
        for invalid in ('escape','symlink'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                root=Path(temp)/'repo';source=root/'drivers/example';source.mkdir(parents=True)
                (source/'Makefile').write_text('obj-m += example.o\n')
                outside=Path(temp)/'outside.h';outside.write_text('outside\n')
                if invalid=='escape':
                    (root/'outside.h').write_text('must not be staged through traversal\n')
                    name='drivers/example/../../outside.h'
                else:
                    (source/'external.h').symlink_to(outside)
                    name='drivers/example/external.h'
                with self.assertRaises(ValueError):
                    builder.stage_module(root,'drivers/example',Path(temp)/'stage/deep/module',
                                         {'drivers/example/Makefile',name})


if __name__ == '__main__':unittest.main()
