# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""指定版本 PDF 的重定向、来源证据、内容哈希与旧缓存保护。"""
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.local_artifacts import test_scratch
from pdf_processing import (SelectedPaper, PdfDownloadError, fetch_pdf_bytes,
                            download_or_reuse_pdf, build_online_pdf_url)

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    status = 200
    def __init__(self, url, content):
        super().__init__(content)
        self.url = url
    def geturl(self):
        return self.url


class VersionPdfTests(unittest.TestCase):
    def setUp(self):
        scratch = test_scratch(ROOT)
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paper = SelectedPaper('gr-qc/9901001', 2, 'Synthetic')
        self.url = build_online_pdf_url(self.paper)
        self.content = b'%PDF-1.7\n' + b'x'*2048
        self.destination = self.root / 'pdfs/version-evidence-v1/2026-02-01/gr-qc_9901001v2.pdf'

    def opener(self, request, **kwargs):
        self.assertEqual(request.full_url, self.url)
        return Response(self.url+'.pdf', self.content)

    def test_exact_old_id_and_optional_pdf_suffix(self):
        self.assertEqual(fetch_pdf_bytes(self.url, opener=self.opener), self.content)

    def test_wrong_version_unversioned_or_foreign_redirect_rejected(self):
        for url in (self.url.replace('v2', 'v5'), self.url[:-2], 'https://example.org/pdf/gr-qc/9901001v2'):
            with self.subTest(url=url), self.assertRaisesRegex(PdfDownloadError, 'identity_mismatch'):
                fetch_pdf_bytes(self.url, opener=lambda *a, **k: Response(url, self.content))

    def test_download_provenance_reuse_and_hash_mismatch(self):
        result = download_or_reuse_pdf(self.root, self.paper, self.destination, opener=self.opener)
        self.assertEqual(result.status, 'downloaded')
        record = json.loads(self.destination.with_suffix('.provenance.json').read_text(encoding='utf-8'))
        self.assertEqual((record['arxiv_id'], record['version']), ('gr-qc/9901001', 2))
        fail_network = lambda *a, **k: self.fail('reuse must not call network')
        self.assertEqual(download_or_reuse_pdf(self.root, self.paper, self.destination, opener=fail_network).status,
                         'reused_existing_pdf')
        self.destination.write_bytes(self.content + b'changed')
        result = download_or_reuse_pdf(self.root, self.paper, self.destination, opener=fail_network)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_type, 'existing_pdf_evidence_invalid')

    def test_unproven_old_pdf_not_overwritten_or_trusted(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(self.content)
        result = download_or_reuse_pdf(self.root, self.paper, self.destination,
                    opener=lambda *a, **k: self.fail('must preserve old cache'))
        self.assertEqual(result.status, 'failed')
        self.assertEqual(self.destination.read_bytes(), self.content)

    def test_wrong_provenance_version_rejected(self):
        download_or_reuse_pdf(self.root, self.paper, self.destination, opener=self.opener)
        proof = self.destination.with_suffix('.provenance.json')
        record = json.loads(proof.read_text(encoding='utf-8'))
        record['version'] = 5
        proof.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(download_or_reuse_pdf(self.root, self.paper, self.destination, opener=self.opener).status, 'failed')
