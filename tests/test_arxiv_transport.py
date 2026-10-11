# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""单次传输期限与失败证据回归；全部网络为离线故障注入。"""
import logging
from datetime import date
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import main
from desktop.diagnostics import sanitize_details
from scripts.local_artifacts import test_scratch

ROOT = Path(__file__).resolve().parents[1]


class ArxivTransportTests(unittest.TestCase):
    def setUp(self):
        scratch = test_scratch(ROOT)
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.events = []
        self.logger = logging.Logger('transport-test')

    def request(self, margin=15):
        return main.fetch_arxiv_metadata('https://export.arxiv.org/api/query?id_list=2506.15886',
            90, self.cache, 5, self.logger, curl_extra_args=('--disable',),
            subprocess_timeout_margin_seconds=margin,
            diagnostic_observer=lambda kind, details: self.events.append((kind, details)))

    def test_network_budget_and_process_guard_are_separate_no_retry(self):
        def complete(args, **kwargs):
            self.assertEqual(args[args.index('--max-time')+1], '90')
            self.assertEqual(kwargs['timeout'], 105)
            self.assertIn('--http1.1', args)
            return subprocess.CompletedProcess(args, 0, b'<xml/>\n__ARXIV_KALEID_CURL_STATUS__:200', b'')
        with patch('main.subprocess.run', side_effect=complete) as operation:
            self.assertEqual(self.request(), b'<xml/>')
        operation.assert_called_once()

    def test_curl_timeout_keeps_partial_counts_and_safe_headers(self):
        def timeout(args, **kwargs):
            Path(args[args.index('--dump-header')+1]).write_bytes(
                b'HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nSet-Cookie: private-canary\r\n\r\n')
            return subprocess.CompletedProcess(args, 28, b'<partial-private-canary>', b'private-stderr-canary')
        with patch('main.subprocess.run', side_effect=timeout) as operation:
            with self.assertRaisesRegex(RuntimeError, '^arxiv_api_timeout$'):
                self.request()
        operation.assert_called_once()
        kind, details = self.events[0]
        self.assertEqual(kind, 'curl_failure')
        self.assertEqual(details['timeout_source'], 'curl')
        self.assertEqual(details['body_bytes'], len(b'<partial-private-canary>'))
        self.assertEqual(details['response_headers'], {'content-type': ['text/xml']})
        self.assertNotIn('private', str(details))
        self.assertFalse(list(self.cache.glob('*.tmp')))

    def test_python_guard_timeout_is_observed_and_preserves_error(self):
        with patch('main.subprocess.run', side_effect=subprocess.TimeoutExpired(
                ['curl'], 105, output=b'<incomplete>', stderr=b'private-canary')) as operation:
            with self.assertRaisesRegex(RuntimeError, '^arxiv_api_timeout$') as caught:
                self.request()
        operation.assert_called_once()
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(self.events[0][1]['timeout_source'], 'python_subprocess')
        self.assertEqual(self.events[0][1]['body_bytes'], 12)
        self.assertNotIn('private', str(self.events))

    def test_observer_failure_does_not_replace_timeout(self):
        with patch('main.subprocess.run', side_effect=subprocess.TimeoutExpired(['curl'], 105)), \
                patch('main._arxiv_incomplete_transfer_diagnostic', side_effect=ValueError('diagnostic-failed')):
            with self.assertRaisesRegex(RuntimeError, '^arxiv_api_timeout$'):
                self.request()

    def test_invalid_process_margin_fails_before_network(self):
        with patch('main.subprocess.run') as operation:
            for margin in (True, -1, 61, '15', float('nan')):
                with self.assertRaisesRegex(RuntimeError, 'arxiv_metadata_subprocess_margin_invalid'):
                    self.request(margin)
        operation.assert_not_called()

    def test_header_lists_survive_without_expanding_privacy_boundary(self):
        actual = sanitize_details({'response_headers': {
            'Content-Type': ['text/xml'], 'server': 'Google Frontend',
            'set-cookie': ['private-canary'], 'via': ['bad\nvalue'], 'age': ['7', 'private-canary'],
            'date': ['非 ASCII'], 'x-cache': ['x' * 129]}})
        self.assertEqual(actual, {'response_headers': {
            'content-type': 'text/xml', 'server': 'Google Frontend', 'age': '7'}})

    def test_real_validation_cases_use_current_version_and_performance_dates(self):
        from scripts.validate_arxiv_history import validation_cases
        from desktop.arxiv_versions import timestamp
        current = {'version': 6, 'updated': '2026-08-03T12:17:58Z'}
        cases = validation_cases(current, date(2026, 10, 11))
        self.assertEqual(cases, [(date(2026, 10, 11), None), (date(2026, 10, 8), None),
            (date(2026, 8, 3), 6), (date(2026, 7, 10), None)])
        current = {'version': 7, 'updated': '2026-08-04T16:00:00Z'}
        self.assertEqual(validation_cases(current, date(2026, 10, 11))[-1], (date(2026, 8, 5), 7))
