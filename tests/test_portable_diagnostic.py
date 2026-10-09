# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""完整运行冻结诊断路径的离线故障注入；使用 Windows 合成 DPAPI。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
import importlib.util, json, shutil, sys, hashlib
from pathlib import Path
from unittest.mock import patch
root, copy, failure = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == 'failure'
sys.path.insert(0, str(root))
from desktop import app, paths
from desktop.diagnostics import DesktopDiagnostics
internal = copy / '_internal'
for name in ['config.json', 'assets/app-icon.ico', 'assets/user-guide.svg',
             'prompts/relevance_round1_v23.txt', 'prompts/relevance_round2_v18.txt',
             'prompts/research_prompt_round1_v2.txt', 'prompts/research_prompt_round2_v2.txt',
             'docs/desktop/USER_GUIDE.md']:
    target = internal / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / name, target)
(copy / 'BUILD_INFO.json').write_text(json.dumps({'resource_hashes': {
    name: hashlib.sha256((internal / name).read_bytes()).hexdigest()
    for name in ('docs/desktop/USER_GUIDE.md', 'assets/user-guide.svg')
}}), encoding='utf-8')
spec = importlib.util.spec_from_file_location('portable_check', root / 'packaging/windows/portable_check.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
if sys.argv[3] == 'ime-failure':
    def isolation_failure():
        raise RuntimeError('synthetic_ime_isolation_failure')
    diagnostic.disable_diagnostic_ime = isolation_failure
original_close, injected = DesktopDiagnostics.close, []
def fail_late(self):
    original_close(self)
    if failure:
        for file in (copy / 'runtime/logs').glob('desktop-*.jsonl'):
            file.unlink()
        injected.append(True)
with patch.object(sys, 'frozen', True, create=True), \
        patch.object(sys, '_MEIPASS', str(internal), create=True), \
        patch.object(sys, 'executable', str(copy / 'arXivKaleid.exe')), \
        patch.object(paths, 'configure_timezone'), \
        patch.object(paths, 'curl_executable', return_value=str(copy / '_internal/vendor/curl/bin/curl.exe')), \
        patch.object(diagnostic.subprocess, 'check_output', return_value=b'curl synthetic\nProtocols: https\n'), \
        patch.object(DesktopDiagnostics, 'close', fail_late):
    code = diagnostic.run()
    rejected = diagnostic.run()
report = json.loads((copy / 'runtime/work/portable-check.json').read_text(encoding='utf-8'))
print('RESULT:' + json.dumps({'code': code, 'ok': report['ok'], 'injected': bool(injected),
                            'minimal_protocol_free_evaluation': report.get('minimal_protocol_free_evaluation'),
                            'round2_independent_handoff': report.get('round2_independent_handoff'),
                            'research_requirements_requests_and_hashes': report.get('research_requirements_requests_and_hashes'),
                            'diagnostic_ime_thread_only': report.get('diagnostic_ime_thread_only'),
                            'failure_type': report.get('failure_type'), 'used_runtime': rejected}))
'''


@unittest.skipUnless(sys.platform == 'win32' and importlib.util.find_spec('qfluentwidgets'),
                     'requires Windows DPAPI and installed Fluent GUI')
class PortableDiagnosticTests(unittest.TestCase):
    def exercise(self, mode):
        from scripts.local_artifacts import test_scratch
        scratch = test_scratch(ROOT)
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            env = dict(os.environ, QT_QPA_PLATFORM='offscreen', TEMP=folder, TMP=folder)
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', FIXTURE,
                                     str(ROOT), folder, mode], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
            line = next(line for line in result.stdout.decode('utf-8').splitlines() if line.startswith('RESULT:'))
            return json.loads(line[7:])

    def test_post_event_loop_failure_cannot_report_success(self):
        result = self.exercise('failure')
        self.assertTrue(result['injected'])
        self.assertEqual(result['failure_type'], 'AssertionError')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 1)

    def test_success_and_used_runtime_refusal(self):
        result = self.exercise('success')
        self.assertTrue(result['ok'])
        self.assertEqual(result['code'], 0)
        self.assertTrue(result['minimal_protocol_free_evaluation'])
        self.assertTrue(result['round2_independent_handoff'])
        self.assertTrue(result['research_requirements_requests_and_hashes'])
        self.assertTrue(result['diagnostic_ime_thread_only'])
        self.assertEqual(result['used_runtime'], 2)

    def test_ime_isolation_failure_rejects_diagnostic(self):
        result = self.exercise('ime-failure')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 1)
        self.assertEqual(result['failure_type'], 'RuntimeError')
        self.assertIsNone(result['diagnostic_ime_thread_only'])
