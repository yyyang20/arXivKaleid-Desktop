from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_builder_tests', ROOT / 'scripts/build_windows_portable.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


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
                         {'pypdf': '6.14.2', 'PySide6': '6.9.2'})
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

    def test_curl_manifest_records_fixed_official_archive(self):
        manifest = json.loads((ROOT / 'packaging/windows/curl-manifest.json').read_text())
        self.assertEqual(manifest['architecture'], 'x64')
        self.assertTrue(manifest['url'].startswith('https://curl.se/windows/'))
        self.assertEqual(len(manifest['sha256']), 64)
        self.assertIn(manifest['version'], manifest['archive'])
