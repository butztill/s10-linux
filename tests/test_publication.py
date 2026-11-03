# SPDX-License-Identifier: GPL-2.0-only
"""Exercise publication boundaries, blockers and reproducible export."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publication', Path(__file__).resolve().parents[1] / 'scripts/publication.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = ['README.md', 'hello.py', 'LICENSES/GPL-2.0-only.txt']
        self.policy = {'include': ['**'], 'public_documents': ['README.md']}
        self.put('README.md', '<!-- SPDX-License-Identifier: GPL-2.0-only -->\n# Example\n')
        self.put('hello.py', '# SPDX-License-Identifier: GPL-2.0-only\nprint(42)\n')
        self.put('LICENSES/GPL-2.0-only.txt', 'fixture license text\n')

    def put(self, name, data):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
        return p

    def audit(self):
        with patch.object(publication, 'inventory', return_value=self.files):
            return publication.audit(self.root, self.policy)

    def test_clean_export_has_original_sources_and_manifest_and_is_reproducible(self):
        report = self.audit()
        self.assertTrue(report['ready'], report['issues'])
        a, b = self.root / 'a.tar.gz', self.root / 'b.tar.gz'
        publication.export(self.root, report, a)
        publication.export(self.root, report, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        with tarfile.open(a) as archive:
            self.assertEqual(archive.extractfile('hello.py').read(), (self.root/'hello.py').read_bytes())
            self.assertIn(b'hello.py', archive.extractfile('SOURCE-SHA256SUMS').read())
            self.assertTrue(all(x.uid == 0 and x.mtime == 0 for x in archive))

    def test_private_and_binary_and_internal_notes_are_excluded(self):
        self.files += ['keys/test', 'stock-fw/vendor.c', 'output.dtb', 'docs/old.md', '../outside', 'sensor.s5k']
        report = self.audit()
        self.assertTrue(report['ready'])
        self.assertEqual(len(report['excluded']), 6)

    def test_exported_source_reaudits_without_git_and_detects_changes(self):
        report = self.audit()
        archive = self.root/'source.tar.gz'
        publication.export(self.root, report, archive)
        unpack = self.root/'unpacked'
        unpack.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(unpack, filter='data')
        self.assertTrue(publication.audit(unpack, self.policy)['ready'])
        (unpack/'hello.py').write_text('# SPDX-License-Identifier: GPL-2.0-only\nchanged\n')
        self.assertTrue(any(x['code'] == 'invalid-source-inventory'
                            for x in publication.audit(unpack, self.policy)['issues']))

    def test_archive_manifest_path_escape_is_rejected_before_reading_payload(self):
        self.put('SOURCE-SHA256SUMS', 'a'*64 + '  ../outside\n')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read outside')):
            with self.assertRaises(ValueError):
                publication.inventory(self.root, self.policy)

    def test_untracked_content_is_not_added_by_inventory(self):
        self.put('untracked.py', 'local only')
        with patch.object(publication.subprocess, 'check_output', return_value=b'hello.py\0'):
            self.assertEqual(publication.inventory(self.root, self.policy), ['hello.py'])

    def test_missing_license_and_review_block_export_without_creating_output(self):
        self.put('hello.py', 'print(42)\n')
        self.policy['review'] = [{'paths':['hello.py'], 'reason':'provenance unresolved'}]
        report = self.audit()
        codes = {x['code'] for x in report['issues']}
        self.assertIn('license-review', codes)
        self.assertIn('provenance-review', codes)
        output = self.root / 'out.tar.gz'
        with self.assertRaises(ValueError): publication.export(self.root, report, output)
        self.assertFalse(output.exists())

    def test_copied_patch_context_is_not_a_license_grant(self):
        self.files += ['change.patch']
        self.put('change.patch', '--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n // SPDX-License-Identifier: GPL-2.0-only\n')
        self.assertTrue(any(x['path'] == 'change.patch' and x['code'] == 'license-review' for x in self.audit()['issues']))

    def test_sensitive_content_is_reported_without_values(self):
        content = '-----BEGIN' + ' PRIVATE KEY-----\nexample-material'
        self.put('hello.py', '# SPDX-License-Identifier: GPL-2.0-only\n' + content)
        report = self.audit()
        self.assertTrue(any(x['code'] == 'private-key' for x in report['issues']))
        self.assertNotIn('example-material', json.dumps(report))

    def test_symlink_and_parent_symlink_are_rejected(self):
        self.put('target', '# SPDX-License-Identifier: GPL-2.0-only\n')
        (self.root/'hello.py').unlink()
        (self.root/'hello.py').symlink_to(self.root/'target')
        self.assertTrue(any(x['code'] == 'symlink' for x in self.audit()['issues']))
        (self.root/'outside').symlink_to('/tmp', target_is_directory=True)
        self.files += ['outside/does-not-exist']
        self.assertTrue(any(x['path'] == 'outside/does-not-exist' and x['code'] == 'symlink' for x in self.audit()['issues']))

    def test_changed_input_and_existing_output_are_not_exported(self):
        report = self.audit()
        self.put('hello.py', '# SPDX-License-Identifier: GPL-2.0-only\nchanged\n')
        output = self.root/'out.tar.gz'
        with self.assertRaises(ValueError): publication.export(self.root, report, output)
        self.assertFalse(output.exists())
        report = self.audit()
        output.write_bytes(b'preserve')
        with self.assertRaises(FileExistsError): publication.export(self.root, report, output)
        self.assertEqual(output.read_bytes(), b'preserve')

    def test_document_links_and_language_are_reviewed(self):
        self.put('README.md', '<!-- SPDX-License-Identifier: GPL-2.0-only -->\nDieses [link](missing.md)\n')
        codes = {x['code'] for x in self.audit()['issues']}
        self.assertIn('public-document-language', codes)
        self.assertIn('public-document-link', codes)

    def test_missing_license_text_is_a_blocker(self):
        (self.root/'LICENSES/GPL-2.0-only.txt').unlink()
        self.assertTrue(any(x['code'] == 'missing-license-text' for x in self.audit()['issues']))

    def test_document_link_to_unselected_local_file_is_blocked(self):
        self.put('helper.py', '# SPDX-License-Identifier: GPL-2.0-only\n')
        self.put('README.md', '<!-- SPDX-License-Identifier: GPL-2.0-only -->\n[Helper](helper.py)\n')
        self.assertTrue(any(x['code'] == 'public-document-link' for x in self.audit()['issues']))
        self.files += ['helper.py']
        self.assertTrue(self.audit()['ready'])

    def test_empty_selection_and_global_review_block_export(self):
        self.policy['blocker'] = [{'id': 'source-review', 'reason': 'not reviewed'}]
        self.assertFalse(self.audit()['ready'])
        self.files = []
        self.assertTrue(any(x['code'] == 'empty-selection' for x in self.audit()['issues']))

    def test_required_source_must_be_selected_even_if_present_locally(self):
        self.put('helper.py', '# SPDX-License-Identifier: GPL-2.0-only\n')
        self.policy['source_dependency'] = [{'owner': 'hello.py', 'requires': ['helper.py']}]
        self.assertTrue(any(x['code'] == 'missing-source-dependency' for x in self.audit()['issues']))
        self.files += ['helper.py']
        self.assertTrue(self.audit()['ready'])
        self.policy['exclude'] = ['helper.py']
        self.assertTrue(any(x['code'] == 'missing-source-dependency' for x in self.audit()['issues']))

    def test_deferred_owner_does_not_require_its_sources(self):
        self.policy['source_dependency'] = [{'owner': 'deferred.py', 'requires': ['missing.py']}]
        self.assertTrue(self.audit()['ready'])

    def source_manifest(self, body):
        name = 'release/components.toml'
        self.put(name, '# SPDX-License-Identifier: GPL-2.0-only\nversion = 1\n' + body)
        self.files += [name]
        self.policy['source_manifest'] = name
        self.policy['required_components'] = ['library']

    def test_version_label_is_not_an_immutable_component_pin(self):
        self.source_manifest('[[component]]\nid="library"\nversion="1.0"\n')
        self.assertTrue(any(x['code'] == 'component-source-pins' for x in self.audit()['issues']))

    def test_pinned_component_and_required_id_are_checked(self):
        self.source_manifest('[[component]]\nid="library"\nupstream="https://example.invalid/repo"\nrevision="' + 'a'*40 + '"\n')
        self.assertTrue(self.audit()['ready'])
        self.policy['required_components'] += ['missing']
        self.assertTrue(any(x['code'] == 'component-source-pins' for x in self.audit()['issues']))

    def test_component_archive_hash_and_unique_id_are_checked(self):
        entry = '[[component]]\nid="library"\narchive="https://example.invalid/source.tar.gz"\narchive_sha256="' + 'b'*64 + '"\n'
        self.source_manifest(entry)
        self.assertTrue(self.audit()['ready'])
        with (self.root/self.policy['source_manifest']).open('a') as file:
            file.write(entry)
        self.assertTrue(any(x['code'] == 'component-source-pins' for x in self.audit()['issues']))

    def decisions(self, target, codes):
        name = 'release/reviews.json'
        self.files += [name, name + '.license']
        self.policy['review_decisions'] = name
        self.put(name + '.license', '# SPDX-License-Identifier: GPL-2.0-only\n')
        document = {'version': 1, 'files': [{
            'path': target, 'sha256': hashlib.sha256((self.root/target).read_bytes()).hexdigest(),
            'resolved_codes': codes,
            'evidence': 'Explicitly reviewed synthetic test fixture, not captured subscriber data.'}]}
        self.put(name, json.dumps(document))
        return document

    def test_hash_bound_review_and_json_license_sidecar(self):
        self.put('hello.py', '# SPDX-License-Identifier: GPL-2.0-only\nimei="' + '1'*15 + '"\n')
        self.decisions('hello.py', ['subscriber-identifier'])
        report = self.audit()
        self.assertTrue(report['ready'], report['issues'])
        self.assertEqual(len(report['resolved_findings']), 1)
        self.assertEqual(next(x for x in report['files'] if x['path']=='release/reviews.json')['license_expressions'], ['GPL-2.0-only'])
        with (self.root/'hello.py').open('a') as output:
            output.write('# changed\n')
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertTrue(any(x['code'] == 'invalid-review-record' for x in report['issues']))
        self.assertTrue(any(x['code'] == 'subscriber-identifier' for x in report['issues']))

    def test_review_record_cannot_waive_private_keys(self):
        self.put('hello.py', '# SPDX-License-Identifier: GPL-2.0-only\n-----BEGIN'+' PRIVATE KEY-----\n')
        self.decisions('hello.py', ['private-key'])
        report = self.audit()
        self.assertFalse(report['ready'])
        self.assertTrue(any(x['code'] == 'private-key' for x in report['issues']))
        self.assertTrue(any(x['code'] == 'invalid-review-record' for x in report['issues']))

    def test_named_embedded_payload_is_blocked_despite_license_tag(self):
        self.files += ['sensor.h']
        self.put('sensor.h', '// SPDX-License-Identifier: GPL-2.0-only\nstatic const unsigned char sensor_burst0[] = {1,2,3};\n')
        self.assertTrue(any(x['code']=='embedded-payload-review' for x in self.audit()['issues']))

    def test_payload_in_a_patch_is_still_reviewed(self):
        self.files += ['sensor.patch']
        self.put('sensor.patch', '# SPDX-License-Identifier: GPL-2.0-only\n--- a/x.h\n+++ b/x.h\n@@ -0,0 +1 @@\n+static const unsigned char sensor_burst0[] = {1,2,3};\n')
        self.assertTrue(any(x['path']=='sensor.patch' and x['code']=='embedded-payload-review' for x in self.audit()['issues']))

    def test_component_license_requirement_is_not_a_blanket_relicense(self):
        self.policy['license_requirement'] = [{'paths':['hello.py'], 'expressions':['GPL-2.0-or-later'], 'reason':'Shared bridge needs the confirmed compatible grant.'}]
        self.assertTrue(any(x['code']=='license-compatibility-review' for x in self.audit()['issues']))
        self.put('LICENSES/GPL-2.0-or-later.txt','fixture license text\n')
        self.files += ['LICENSES/GPL-2.0-or-later.txt']
        self.put('hello.py','# SPDX-License-Identifier: GPL-2.0-or-later\nprint(42)\n')
        self.assertTrue(self.audit()['ready'])


if __name__ == '__main__':
    unittest.main()
