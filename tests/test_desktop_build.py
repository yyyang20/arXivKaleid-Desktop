# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_builder_tests', ROOT / 'scripts/build_windows_portable.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
license_spec = importlib.util.spec_from_file_location('portable_license_tests', ROOT / 'scripts/portable_licenses.py')
license_collector = importlib.util.module_from_spec(license_spec)
with patch.dict(sys.modules, {'build_windows_portable': builder}):
    license_spec.loader.exec_module(license_collector)


class DesktopBuildTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.codex-validation'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_wrong_checksum_never_executes_or_writes_download(self):
        target = self.root / 'curl.zip'
        with patch.object(builder.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')), patch.object(builder.subprocess, 'run') as execute:
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                builder.download('https://example.invalid/test.zip', '0' * 64, target)
            execute.assert_not_called()
        self.assertFalse(target.exists())

    def test_cached_download_is_verified_without_network(self):
        target = self.root / 'curl.zip'
        target.write_bytes(b'valid synthetic')
        with patch.object(builder.urllib.request, 'urlopen', side_effect=AssertionError('network')):
            self.assertEqual(builder.download('unused', builder.sha(target.read_bytes()), target), b'valid synthetic')
            with self.assertRaises(RuntimeError):
                builder.download('unused', '0' * 64, target)

    def test_architecture_requires_amd64_pe(self):
        data = bytearray(100)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 60, 64)
        data[64:70] = b'PE\0\0\x64\x86'
        builder.verify_x64(bytes(data))
        for header in (b'\x4c\x01', b'\x64\xaa'):
            data[68:70] = header
            with self.assertRaises(RuntimeError):
                builder.verify_x64(bytes(data))

    def test_release_scanner_rejects_user_state_and_private_path(self):
        for name, content in (('secret.dat', b'fake'), ('data.sqlite-wal', b'fake'),
                              ('paper.pdf', b'fake'), ('diagnostic.jsonl', b'{}\n'),
                              ('runtime/cache/data', b'fake'),
                              ('tests/helper.py', b'fake'), ('screenshots/initial.png', b'fake'),
                              ('paths.txt', str(ROOT).encode('utf-8'))):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.assertRaises(RuntimeError):
                    builder.verify_tree(self.root)
                path.unlink()

    def test_resource_allowlist_matches_current_config(self):
        config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
        for name in ('round1_prompt', 'round2_prompt'):
            self.assertIn(config['paths'][name], builder.RESOURCES)
        self.assertNotIn('config/research_profile.json', builder.RESOURCES)
        self.assertNotIn('profiles/research_profile.md', builder.RESOURCES)
        self.assertNotIn('automation', config)
        self.assertNotIn('config/automation_policy.json', builder.RESOURCES)
        self.assertNotIn('config/local_secret.json', builder.RESOURCES)

    def test_desktop_requirements_include_pdf_runtime_with_fixed_versions(self):
        self.assertEqual(builder.pinned_requirements('requirements-desktop.txt'),
                         {'pypdf': '6.14.2', 'PySide6': '6.9.2',
                          'PySide6-Fluent-Widgets': '1.11.3',
                          'PySideSix-Frameless-Window': '0.8.2',
                          'darkdetect': '0.8.0', 'pywin32': '312'})
        (self.root / 'requirements.txt').write_text('-r other.txt\n', encoding='utf-8')
        (self.root / 'other.txt').write_text('-r requirements.txt\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'cycle'):
            builder.pinned_requirements('requirements.txt', root=self.root)
        for value in ('-r ../outside.txt', 'package>=1.0', 'package==1\npackage==2'):
            (self.root / 'requirements.txt').write_text(value, encoding='utf-8')
            with self.assertRaises((ValueError, RuntimeError)):
                builder.pinned_requirements('requirements.txt', root=self.root)

    def test_public_documents_are_copied_to_release_root(self):
        builder.copy_public_documents(self.root)
        for name in ('README.md', 'EULA.txt', 'PRIVACY.md', 'SECURITY.md'):
            self.assertIn(name, builder.PUBLIC_DOCUMENTS)
            self.assertIn(name, builder.REQUIRED_RELEASE_FILES)
            self.assertEqual(
                (self.root / name).read_bytes(),
                (ROOT / 'docs/public_release' / name).read_bytes(),
            )
        self.assertFalse((self.root / 'RELEASE_CHECKLIST.md').exists())
        self.assertNotIn('THIRD_PARTY_NOTICES.txt', builder.PUBLIC_DOCUMENTS)
        self.assertIn('THIRD_PARTY_NOTICES.txt', builder.REQUIRED_RELEASE_FILES)
        self.assertIn('LICENSE', builder.REQUIRED_RELEASE_FILES)
        self.assertEqual((self.root / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())

    def test_application_license_missing_is_rejected_without_guessing(self):
        with patch.object(builder, 'ROOT', self.root):
            documents = self.root / 'docs/public_release'
            documents.mkdir(parents=True)
            for name in builder.PUBLIC_DOCUMENTS:
                (documents / name).write_text('synthetic document', encoding='utf-8')
            destination = self.root / 'release'
            destination.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'application_license_missing'):
                builder.copy_public_documents(destination)

    def test_source_manifest_rejects_version_license_and_inventory_drift(self):
        items = builder.source_archives()
        self.assertEqual({i['component'] for i in items}, {'QtBase', 'PySide6 and Shiboken6', 'pypdf',
                         'QtSvg', 'PySide6-Fluent-Widgets', 'PySideSix-Frameless-Window', 'darkdetect', 'pywin32'})
        self.assertEqual(next(i['license'] for i in items if i['component'] == 'pypdf'), 'BSD-3-Clause')
        for name in ('requirements.txt', 'requirements-desktop.txt'):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        manifest = self.root / 'packaging/windows/source-manifest.json'
        manifest.parent.mkdir(parents=True)
        for field, value in (('version', '0.0'), ('license', 'LGPL-3.0-only'), ('sha256', 'bad')):
            changed = json.loads(json.dumps(items))
            changed[-1][field] = value
            manifest.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                builder.source_archives(self.root)
        manifest.write_text(json.dumps(items[:-1]), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'inventory'):
            builder.source_archives(self.root)

    def test_legal_resources_reject_altered_license_source_link_and_archive(self):
        builder.copy_public_documents(self.root)
        archive = self.root / 'licenses/sources/synthetic.tar.gz'
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b'exact source')
        item = {'component': 'pypdf', 'archive': archive.name, 'sha256': builder.sha(b'exact source')}
        with patch.object(builder, 'source_archives', return_value=[item]):
            builder.verify_legal_resources(self.root)
            archive.write_bytes(b'altered source')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                builder.verify_legal_resources(self.root)
            archive.unlink()
            with self.assertRaisesRegex(RuntimeError, 'corresponding_source_missing'):
                builder.verify_legal_resources(self.root)
        with patch.object(builder, 'source_archives', return_value=[]):
            (self.root / 'README.md').write_text('floating main source', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'source_link_mismatch'):
                builder.verify_legal_resources(self.root)
            builder.copy_public_documents(self.root)
            (self.root / 'LICENSE').write_text('different license', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'license_mismatch'):
                builder.verify_legal_resources(self.root)

    def test_source_collection_preserves_gzip_xz_archives_and_original_licenses(self):
        # 用合成源码验证两种压缩格式，不访问真实网络或构建环境。
        items, payloads = [], {}
        for compression, component, license_name in (('gz', 'pypdf', 'BSD-3-Clause'),
                                                       ('xz', 'QtBase', 'LGPL-3.0-only')):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:' + compression) as archive:
                content = license_name.encode('utf-8')
                member = tarfile.TarInfo(component + '-1/LICENSE')
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            name = component + '.tar.' + compression
            payloads[name] = buffer.getvalue()
            items.append({'component': component, 'version': '1', 'license': license_name,
                          'archive': name, 'url': 'https://example.invalid/' + name,
                          'sha256': builder.sha(payloads[name])})
        licenses = self.root / 'licenses'
        licenses.mkdir()
        with patch.object(license_collector, 'source_archives', return_value=items), \
                patch.object(license_collector, 'download', side_effect=lambda url, digest, target: payloads[target.name]):
            records = license_collector.collect_source_archives(self.root, licenses)
        for item, record in zip(items, records):
            self.assertEqual(record['license'], item['license'])
            self.assertEqual((licenses / 'sources' / item['archive']).read_bytes(), payloads[item['archive']])
            self.assertEqual((licenses / item['component'] / 'LICENSE').read_text(), item['license'])

    def test_curl_manifest_records_fixed_official_archive(self):
        manifest = json.loads((ROOT / 'packaging/windows/curl-manifest.json').read_text())
        self.assertEqual(manifest['architecture'], 'x64')
        self.assertTrue(manifest['url'].startswith('https://curl.se/windows/'))
        self.assertEqual(len(manifest['sha256']), 64)
        self.assertIn(manifest['version'], manifest['archive'])

    def test_new_wheel_license_layouts_preserve_notices_without_collision(self):
        from types import SimpleNamespace
        dist = SimpleNamespace(files=[
            'fluent.dist-info/LICENSE',
            'win32.dist-info/licenses/com/License.txt',
            'win32.dist-info/licenses/pythonwin/License.txt',
            'win32.dist-info/METADATA', 'fluent/__init__.py',
        ])
        self.assertEqual([p.as_posix() for _, p in license_collector.wheel_license_files(dist)],
                         ['LICENSE', 'com/License.txt', 'pythonwin/License.txt'])

    def test_fluent_spec_keeps_svg_dependencies_without_full_extras(self):
        text = (ROOT / 'packaging/windows/arxivkaleid.spec').read_text(encoding='utf-8')
        for name in ('Qt6Svg.dll', 'Qt6SvgWidgets.dll', 'Qt6Xml.dll', 'qsvgicon.dll'):
            self.assertIn(name, text)
        for name in ('Qt6Network.dll', 'qsvg.dll', 'qoffscreen.dll'):
            self.assertNotIn(name, text)
        self.assertNotIn('[full]', (ROOT / 'requirements-desktop.txt').read_text())

    def test_frozen_identity_requires_clean_descendant_not_equal_main(self):
        with patch.object(builder.subprocess, 'check_output', side_effect=['', 'baseline', 'feature', 'codex/test']), \
                patch.object(builder.subprocess, 'run') as run:
            run.return_value.returncode = 0
            identity = builder.frozen_identity()
            self.assertEqual(identity['commit'], 'feature')
            self.assertEqual(identity['baseline_commit'], 'baseline')
        with patch.object(builder.subprocess, 'check_output', return_value=' M tracked.py'):
            with self.assertRaisesRegex(RuntimeError, 'clean_frozen'):
                builder.frozen_identity()
