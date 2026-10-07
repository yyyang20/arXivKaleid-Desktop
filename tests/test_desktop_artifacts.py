# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""只在自产隔离根中注入故障；不读取用户数据、不构建、不联网。"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import local_artifacts as artifacts
from scripts import verify_public_release as release_check
from scripts import run_local_checks as local_checks
import build_windows_portable as build
import portable_licenses as licenses


class ArtifactFixture(unittest.TestCase):
    def setUp(self):
        scratch = artifacts.test_scratch(ROOT)
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch, prefix='artifact-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def read(self, run):
        return json.loads((run.path / 'run.json').read_text(encoding='utf-8'))

    def write(self, path, data=b'synthetic'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class ArtifactTests(ArtifactFixture):
    def test_defaults_and_protected_inputs_separate_meter(self):
        self.assertEqual(artifacts.WORK_LIMIT, 4 * 1024**3)
        self.assertEqual(artifacts.EVIDENCE_LIMIT, 512 * 1024**2)
        source = self.write(self.root / '.desktop-build/inputs/fixed', b'12345')
        with artifacts.ArtifactRun('build', root=self.root) as run:
            run.protect(source)
            run.protect(source)  # 重复登记不重复计量。
        self.assertEqual(self.read(run)['protected_input_bytes'], 5)
        self.assertNotIn(source.relative_to(self.root).as_posix(), json.dumps(artifacts.cleanup_plan(self.root, run.record['id'])['targets']))

    def test_twenty_successes_rotate_without_deleting_previous_or_inputs(self):
        old = self.write(self.root / 'release/old.zip')
        input_file = self.write(self.root / '.desktop-build/inputs/license.txt')
        historical = self.write(self.root / '.desktop-build/history-audit/keep')
        user = self.write(self.root / '.desktop-runtime/keep')
        formal = self.write(self.root / 'release/current.zip')
        for index in range(20):
            with artifacts.ArtifactRun('build', root=self.root) as run:
                run.protect(input_file)
                source = self.write(run.work / 'report.json', b'{}')
                run.capture(source, 'report.json')
                run.record['commit'] = str(index) * 40
                run.record['version'] = 'alpha 11' if index < 10 else 'alpha 12'
                if index == 0:
                    run.preserve_move(old, 'previous-release.zip')
            self.assertFalse(run.work.exists())
            self.assertEqual(self.read(run)['state'], 'succeeded')
            self.assertEqual(artifacts.file_hash(run.evidence / 'report.json'), hashlib.sha256(b'{}').hexdigest())
            if index == 0:
                first = run
            entries = artifacts.records(self.root)
            self.assertEqual(sum(r['state'] == 'succeeded' for _, r in entries), min(index + 1, 2))
            self.assertLessEqual(len(entries), 3)
            self.assertFalse(any((p / 'work').exists() for p, _ in entries))
        self.assertEqual(self.read(first)['state'], 'released')
        self.assertFalse(first.evidence.exists())
        self.assertEqual((first.preserved / 'previous-release.zip').read_bytes(), b'synthetic')
        for protected in (input_file, historical, user, formal):
            self.assertEqual(protected.read_bytes(), b'synthetic')

    def test_ten_ordinary_failures_then_repair_keep_one_diagnostic(self):
        previous = None
        for index in range(10):
            error = RuntimeError('synthetic unittest failure')
            with self.assertRaises(RuntimeError) as caught:
                with artifacts.ArtifactRun('tests', root=self.root) as run:
                    self.write(run.work / 'rebuildable')
                    self.write(run.evidence / 'diagnostic', str(index).encode())
                    run.mark_ordinary_failure(error, 'unittest_failure')
                    raise error
            self.assertIs(caught.exception, error)
            self.assertEqual(self.read(run)['evidence_class'], 'ordinary')
            self.assertFalse(run.work.exists())
            self.assertEqual(len(artifacts.records(self.root)), 1)
            if previous:
                self.assertFalse(previous.path.exists())
            previous = run
        for _ in range(4):
            with artifacts.ArtifactRun('tests', root=self.root) as success:
                self.write(success.evidence / 'report')
        self.assertEqual(len(artifacts.records(self.root)), 3)
        self.assertEqual((previous.evidence / 'diagnostic').read_bytes(), b'9')
        self.assertEqual(list((self.root / artifacts.TEST_ROOT).iterdir()), [])

    def test_v1_success_compatibility_and_category_isolation(self):
        legacy = []
        for _ in range(2):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                self.write(run.evidence / 'report')
            record = self.read(run)
            record['schema'] = artifacts.LEGACY_SCHEMA
            for field in ('evidence_class', 'protection_reason', 'completed_utc'):
                record.pop(field)
            artifacts.save_record(self.root, record)
            legacy.append(run)
        with artifacts.ArtifactRun('build', root=self.root) as build_run:
            self.write(build_run.evidence / 'report')
        self.assertTrue(all(r.path.exists() for r in legacy))
        with artifacts.ArtifactRun('tests', root=self.root) as run:
            self.write(run.evidence / 'report')
        self.assertFalse(legacy[0].path.exists())
        self.assertTrue(legacy[1].path.exists())
        self.assertTrue(build_run.path.exists())
        with artifacts.ArtifactRun('tests', root=self.root):
            pass
        self.assertFalse(legacy[1].path.exists())

    def test_v1_failure_and_unclassified_success_remain_protected(self):
        with self.assertRaises(ValueError):
            with artifacts.ArtifactRun('build', root=self.root) as failed:
                self.write(failed.evidence / 'diagnostic')
                raise ValueError('unknown')
        record = self.read(failed)
        record['schema'] = artifacts.LEGACY_SCHEMA
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'retention_limit'):
            with artifacts.ArtifactRun('build', root=self.root):
                pass
        for _ in range(4):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                self.write(run.evidence / 'report')
                if _ == 0:
                    protected = run
            if _ == 0:
                record = self.read(run)
                record.update(schema=artifacts.LEGACY_SCHEMA, review_pending=True)
                artifacts.save_record(self.root, record)
                # 待审不能用于同类绕过；其他类别继续运行。
                with self.assertRaisesRegex(RuntimeError, 'retention_limit'):
                    with artifacts.ArtifactRun('tests', root=self.root):
                        pass
                record['review_pending'] = False
                record['outcome'] = 'unknown'
                artifacts.save_record(self.root, record)
        self.assertTrue(protected.evidence.exists())

    def test_marker_is_bound_to_exception_and_review_overrides_it(self):
        for kind, review in (('tests', False), ('visual', True)):
            first = RuntimeError('ordinary')
            other = ValueError('unknown')
            with self.assertRaises(ValueError):
                with artifacts.ArtifactRun(kind, root=self.root, review=review) as run:
                    self.write(run.evidence / 'diagnostic')
                    run.mark_ordinary_failure(first, 'unittest_failure')
                    raise other
            self.assertEqual(self.read(run)['evidence_class'], 'protected')
            with self.assertRaisesRegex(RuntimeError, 'retention_limit'):
                with artifacts.ArtifactRun(kind, root=self.root):
                    pass

    def test_rotation_refuses_changed_hash_unknown_file_or_empty_directory(self):
        for name in ('report', 'unknown', 'unknown-dir'):
            with self.subTest(tamper=name):
                # 每种反例使用独立自产根，保护记录无需在测试中绕过。
                root = self.root / name
                root.mkdir()
                for _ in range(2):
                    with artifacts.ArtifactRun('tests', root=root) as run:
                        self.write(run.evidence / 'report')
                    if _ == 0:
                        first = run
                target = first.evidence / name
                target.mkdir() if name == 'unknown-dir' else self.write(target, b'changed')
                with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
                    with artifacts.ArtifactRun('tests', root=root) as current:
                        self.write(current.evidence / 'report')
                self.assertTrue(first.evidence.exists())
                self.assertEqual(self.read(current)['state'], 'cleanup_failed')
                with self.assertRaisesRegex(RuntimeError, 'unfinished'):
                    with artifacts.ArtifactRun('build', root=root):
                        pass

    def test_rotation_failure_preserves_original_failure_and_blocks_next(self):
        with artifacts.ArtifactRun('tests', root=self.root) as first:
            self.write(first.evidence / 'report')
        with artifacts.ArtifactRun('tests', root=self.root):
            pass
        remove = artifacts.remove_exact
        def occupied(root, path, **kwargs):
            if path == first.evidence:
                raise PermissionError('occupied')
            remove(root, path, **kwargs)
        error = RuntimeError('primary')
        with patch.object(artifacts, 'remove_exact', side_effect=occupied):
            with self.assertRaises(RuntimeError) as caught:
                # 普通失败也会执行同类成功证据轮换。
                with artifacts.ArtifactRun('tests', root=self.root) as run:
                    self.write(run.evidence / 'diagnostic')
                    run.mark_ordinary_failure(error, 'unittest_failure')
                    # 人工构造一个额外已正常结束的成功记录，模拟轮换待处理。
                    first_record = self.read(first)
                    clone_id = 'tests-' + 'f' * 32
                    clone_path = self.root / artifacts.REGISTRY / clone_id
                    clone_path.mkdir()
                    (clone_path / 'evidence').mkdir()
                    (clone_path / 'preserved').mkdir()
                    first_record.update(id=clone_id, work=artifacts.TEST_ROOT + '/' + clone_id,
                                        evidence_hashes={})
                    artifacts.save_record(self.root, first_record)
                    raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(self.read(first)['state'], 'cleanup_failed')
        self.assertEqual(self.read(run)['failure_type'], 'RuntimeError')
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_rotation_interrupt_journal_stops_next_entry(self):
        for _ in range(2):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                self.write(run.evidence / 'report')
            if _ == 0:
                first = run
        remove = artifacts.remove_exact
        def interrupt(root, path, **kwargs):
            if path == first.evidence:
                raise KeyboardInterrupt()
            remove(root, path, **kwargs)
        with patch.object(artifacts, 'remove_exact', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                with artifacts.ArtifactRun('tests', root=self.root):
                    pass
        self.assertEqual(self.read(first)['state'], 'rotating')
        self.assertTrue(first.evidence.exists())
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_interrupt_during_work_cleanup_or_before_rotation_blocks_all_kinds(self):
        for stage in ('work', 'rotation'):
            root = self.root / stage
            root.mkdir()
            remove = artifacts.remove_exact
            def interrupt(root, path, **kwargs):
                if path.name.startswith('tests-'):
                    raise KeyboardInterrupt()
                remove(root, path, **kwargs)
            target = patch.object(artifacts, 'remove_exact', side_effect=interrupt) if stage == 'work' else \
                patch.object(artifacts, 'rotate_evidence', side_effect=KeyboardInterrupt)
            with target, self.assertRaises(KeyboardInterrupt):
                with artifacts.ArtifactRun('tests', root=root) as run:
                    self.write(run.work / 'partial')
                    self.write(run.evidence / 'report')
            self.assertEqual(self.read(run)['state'], 'running' if stage == 'work' else 'finishing')
            self.assertEqual(run.work.exists(), stage == 'work')
            self.assertTrue((run.evidence / 'report').exists())
            with self.assertRaisesRegex(RuntimeError, 'unfinished'):
                with artifacts.ArtifactRun('build', root=root):
                    pass

    def test_only_complete_unittest_failure_is_ordinary(self):
        footer = 'Ran 12 tests in 0.023s\n\nFAILED (failures=1, errors=2)\n'
        self.assertTrue(local_checks.ordinary_test_failure(1, footer))
        self.assertTrue(local_checks.ordinary_test_failure(1, footer.replace('\n', '\r\n')))
        self.assertTrue(local_checks.ordinary_test_failure(1, 'Ran 1 test in 0.1s\n\nFAILED (unexpected successes=1)\n'))
        for code, text in ((0, footer), (2, footer), (-1, footer), (1, 'FAILED (failures=1)'),
                           (1, 'Traceback: startup failed'), (1, 'Ran 0 tests in 0.1s\n\nFAILED (errors=1)')):
            self.assertFalse(local_checks.ordinary_test_failure(code, text))

    def test_entry_points_explicitly_classify_only_normal_failure(self):
        footer = b'Ran 1 test in 0.1s\n\nFAILED (failures=1)\n'
        for code in (1, 2):
            root = self.root / ('tests-' + str(code))
            root.mkdir()
            def fake_tests(command, **kwargs):
                kwargs['stdout'].write(footer)
                return subprocess.CompletedProcess(command, code)
            with patch.object(local_checks, 'ROOT', root), patch.object(sys, 'argv', ['run_local_checks']), \
                    patch.object(artifacts.ArtifactRun, 'process', side_effect=fake_tests):
                with self.assertRaisesRegex(RuntimeError, 'offline_tests_failed'):
                    local_checks.main()
            _, record = artifacts.records(root)[0]
            self.assertEqual(record['evidence_class'], 'ordinary' if code == 1 else 'protected')
        for code in (1, 2):
            root = self.root / ('build-' + str(code))
            root.mkdir()
            with patch.object(build, 'ROOT', root), patch.object(build, 'prepare_resources'), \
                    patch.object(build, 'prepare_curl'):
                with self.assertRaisesRegex(RuntimeError, 'pyinstaller_failed'):
                    with artifacts.ArtifactRun('build', root=root) as run:
                        with patch.object(run, 'process', return_value=subprocess.CompletedProcess([], code)):
                            try:
                                build.build_portable({}, run)
                            finally:
                                run.finish_evidence(lambda: run.capture(run.work / 'pyinstaller.log', 'pyinstaller.log'))
            self.assertEqual(self.read(run)['evidence_class'], 'ordinary' if code == 1 else 'protected')
            self.assertTrue((run.evidence / 'pyinstaller.log').exists())

    def test_rotation_windows_occupied_evidence_and_link_are_protected(self):
        for mode in ('occupied', 'link'):
            root = self.root / mode
            root.mkdir()
            user = self.write(root / 'user-data/keep')
            for _ in range(2):
                with artifacts.ArtifactRun('tests', root=root) as run:
                    self.write(run.evidence / 'report')
                if _ == 0:
                    first = run
            handle = None
            try:
                if mode == 'occupied':
                    handle = (first.evidence / 'report').open('rb')
                else:
                    link = first.evidence / 'link'
                    if os.name == 'nt':
                        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(user.parent)],
                            capture_output=True, cwd=root)
                        self.assertEqual(result.returncode, 0)
                    else:
                        link.symlink_to(user.parent, target_is_directory=True)
                with self.assertRaises((RuntimeError, ValueError)):
                    with artifacts.ArtifactRun('tests', root=root):
                        pass
                self.assertTrue(first.evidence.exists())
                self.assertEqual(user.read_bytes(), b'synthetic')
                with self.assertRaises((RuntimeError, ValueError)):
                    with artifacts.ArtifactRun('build', root=root):
                        pass
            finally:
                if handle:
                    handle.close()

    def test_v2_timestamp_and_scope_corruption_never_rotates(self):
        for field, value in (('completed_utc', None), ('completed_utc', 'bad'),
                             ('work', '.desktop-runtime'), ('id', 'tests-invalid'),
                             ('evidence_class', 'other')):
            root = self.root / (field + str(value).replace('.', '-'))
            root.mkdir()
            with artifacts.ArtifactRun('tests', root=root) as run:
                self.write(run.evidence / 'report')
            record = self.read(run)
            record[field] = value
            # 损坏只在自产夹具；不调用可执行清理入口。
            (run.path / 'run.json').write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                with artifacts.operation_lock(root):
                    artifacts.rotate_evidence(root, 'tests')
            self.assertTrue(run.evidence.exists())

    def test_failure_preserves_diagnosis_and_prevents_continuous_failures(self):
        with self.assertRaisesRegex(ValueError, 'original-failure'):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                self.write(run.work / 'rebuildable.sqlite')
                self.write(run.evidence / 'diagnostic.json', b'{}')
                raise ValueError('original-failure')
        self.assertFalse(run.work.exists())
        record = self.read(run)
        self.assertEqual((record['state'], record['failure_type']), ('failed', 'ValueError'))
        self.assertNotIn('original-failure', json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, 'retention_limit'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_cleanup_failure_keeps_original_error_and_blocks_all_kinds(self):
        with patch.object(artifacts, 'remove_exact', side_effect=PermissionError('file occupied')):
            with self.assertRaisesRegex(ValueError, 'primary'):
                with artifacts.ArtifactRun('build', root=self.root) as run:
                    self.write(run.work / 'cache')
                    raise ValueError('primary')
        self.assertEqual(self.read(run)['state'], 'cleanup_failed')
        self.assertTrue(run.work.exists())
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('visual', root=self.root):
                pass
        plan = artifacts.cleanup_plan(self.root, run.record['id'])
        with self.assertRaisesRegex(RuntimeError, 'still_active'):
            artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'], stopped=True)

    def test_cleanup_failure_cannot_be_success(self):
        with patch.object(artifacts, 'remove_exact', side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
                with artifacts.ArtifactRun('build', root=self.root) as run:
                    pass
        self.assertEqual(self.read(run)['state'], 'cleanup_failed')

    @unittest.skipUnless(os.name == 'nt', 'Windows 文件占用测试')
    def test_windows_real_file_occupation_preserves_diagnostics(self):
        handle = None
        try:
            with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
                with artifacts.ArtifactRun('build', root=self.root) as run:
                    path = self.write(run.work / 'locked-cache')
                    handle = path.open('rb')
                    self.write(run.evidence / 'diagnosis.json', b'{}')
            self.assertTrue(path.exists())
            self.assertEqual(self.read(run)['state'], 'cleanup_failed')
            self.assertTrue((run.evidence / 'diagnosis.json').exists())
        finally:
            if handle is not None:
                handle.close()

    def test_protected_hash_change_blocks_cleanup(self):
        protected = self.write(self.root / 'stable/input')
        with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
            with artifacts.ArtifactRun('build', root=self.root) as run:
                run.protect(protected)
                protected.write_bytes(b'changed')
                self.write(run.work / 'needed')
        self.assertTrue(run.work.exists())

    def test_portable_evidence_keeps_screenshots_excludes_synthetic_secret(self):
        import validate_windows_portable as validator
        with artifacts.ArtifactRun('portable', root=self.root, review=True) as run:
            self.write(run.work / 'copy/runtime/work/screen.png')
            self.write(run.work / 'copy/runtime/work/portable-check.json', b'{"ok": true}')
            self.write(run.work / 'copy/runtime/config/secret.dat', b'synthetic-ciphertext')
            self.write(run.work / 'copy/runtime/work/database.sqlite', b'synthetic')
            validator.collect_validation_evidence(run)
        self.assertFalse(run.work.exists())
        self.assertTrue((run.evidence / 'copy/runtime/work/screen.png').exists())
        self.assertFalse(any(p.suffix in ('.dat', '.sqlite') for p in run.evidence.rglob('*')))

    def test_all_portable_stages_mocked_then_seven_copies_released(self):
        import validate_windows_portable as validator
        source = self.root / 'dist/synthetic'
        self.write(source / 'arXivKaleid.exe', b'synthetic executable')
        self.write(source / '_internal/prompts/relevance_round2_v16.txt')
        self.write(source / '_internal/prompts/research_requirements_round1_v1.txt')
        original = artifacts.inventory(self.root, source, hashes=True)
        calls = []
        class Restart:
            pid = os.getpid()
            active = True
            def poll(self): return None if self.active else 0
            def wait(self, timeout=None):
                self.active = False
                return 0
        with artifacts.ArtifactRun('portable', root=self.root, review=True) as run:
            run.protect(source)
            def process(command, **kwargs):
                copy_root = Path(command[0]).parent
                calls.append(command[1:])
                if '--portable-recovery-check' in command:
                    self.write(copy_root / 'runtime/work/recovery-check.json', json.dumps({
                        'synthetic_dpapi_recovered': True, 'history_restored_exactly': True,
                        'deleted_history_stays_deleted': True, 'research_requirements_restored_exactly': True}).encode())
                    code = 0
                elif '--visual-qa' in command:
                    mode = copy_root.name.split(' ')[-1]
                    dpr = 1.5 if mode == 'native' else int(mode.rsplit('-', 1)[-1]) / 100
                    self.write(copy_root / 'runtime/work/screen.png')
                    self.write(copy_root / 'runtime/work/portable-check.json', json.dumps({'ok': True,
                        'visual_qa': {'screen': 'primary', 'captures': [{'screen': 'primary', 'dpr': dpr}]}}).encode())
                    code = 0
                else:
                    code = 2 if copy_root.name.startswith('隔离副本') else 1
                return subprocess.CompletedProcess(command, code)
            with patch.object(validator, 'ROOT', self.root), patch.object(validator, 'verify_tree'), \
                    patch.object(validator, 'close_own_window', return_value=True), \
                    patch.object(validator.subprocess, 'Popen', side_effect=lambda *a, **kw: Restart()), \
                    patch.object(run, 'process', side_effect=process):
                validator.validate_portable(run, source, {'commit': 'a' * 40}, type('Args', (), {'arxiv': False})())
                self.assertEqual(len(list(run.work.iterdir())), 12)  # 七份副本及五份过程日志。
                validator.collect_validation_evidence(run)
        self.assertFalse(run.work.exists())
        self.assertEqual(artifacts.inventory(self.root, source, hashes=True), original)
        summary = json.loads((run.evidence / 'validation.json').read_text(encoding='utf-8'))
        self.assertEqual(len(summary['runs']), 5)
        self.assertTrue(summary['used_runtime_rejected'])
        self.assertEqual(sum('--portable-recovery-check' in c for c in calls), 5)
        self.assertEqual(len(run.children), 15)

    def test_evidence_failure_keeps_work_and_original_failure(self):
        with self.assertRaisesRegex(ValueError, 'primary'):
            with artifacts.ArtifactRun('build', root=self.root) as run:
                self.write(run.work / 'needed-diagnostic')
                try:
                    raise ValueError('primary')
                finally:
                    run.finish_evidence(lambda: (_ for _ in ()).throw(OSError('copy-failed')))
        self.assertTrue(run.work.exists())
        self.assertEqual(self.read(run)['state'], 'cleanup_failed')

    def test_record_save_failure_does_not_mask_primary(self):
        with self.assertRaisesRegex(ValueError, 'primary'):
            with artifacts.ArtifactRun('build', root=self.root) as run:
                patcher = patch.object(run, 'save', side_effect=OSError)
                patcher.start()
                self.addCleanup(patcher.stop)
                raise ValueError('primary')

    def test_manual_review_requires_explicit_closure(self):
        with artifacts.ArtifactRun('portable', root=self.root, review=True) as run:
            self.write(run.evidence / 'screen.png')
        plan = artifacts.cleanup_plan(self.root, run.record['id'])
        artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'])
        self.assertTrue((run.evidence / 'screen.png').exists())
        self.assertEqual(self.read(run)['state'], 'awaiting_review')
        plan = artifacts.cleanup_plan(self.root, run.record['id'], review_complete=True)
        artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'], review_complete=True)
        self.assertFalse(run.path.exists())

    def test_failed_manual_evidence_cannot_be_deleted_without_closure(self):
        with self.assertRaises(ValueError):
            with artifacts.ArtifactRun('visual', root=self.root, review=True) as run:
                self.write(run.evidence / 'screen.png')
                raise ValueError('synthetic failure')
        plan = artifacts.cleanup_plan(self.root, run.record['id'])
        artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'])
        self.assertTrue((run.evidence / 'screen.png').exists())
        self.assertEqual(self.read(run)['state'], 'failed')

    def test_manual_cleanup_failure_records_unfinished_state(self):
        with artifacts.ArtifactRun('build', root=self.root) as run:
            self.write(run.evidence / 'report')
        plan = artifacts.cleanup_plan(self.root, run.record['id'])
        with patch.object(artifacts, 'remove_exact', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'])
        self.assertEqual(self.read(run)['state'], 'cleanup_failed')
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_plan_changes_unknown_files_and_users_are_rejected(self):
        user_data = self.write(self.root / '.desktop-runtime/history/user.sqlite')
        with artifacts.ArtifactRun('build', root=self.root) as run:
            self.write(run.evidence / 'report')
        plan = artifacts.cleanup_plan(self.root, run.record['id'])
        self.write(run.evidence / 'report', b'changed')
        with self.assertRaisesRegex(RuntimeError, 'plan_changed'):
            artifacts.apply_plan(self.root, run.record['id'], plan['plan_sha256'])
        self.write(run.path / 'unknown-user-file')
        with self.assertRaisesRegex(RuntimeError, 'unknown_artifact_entry'):
            artifacts.cleanup_plan(self.root, run.record['id'])
        for target in (user_data, self.root / 'release', self.root / '.desktop-build'):
            with self.assertRaisesRegex(RuntimeError, 'delete_scope_invalid'):
                artifacts.remove_exact(self.root, target)
        self.assertEqual(user_data.read_bytes(), b'synthetic')
        for identifier in ('../../release', 'build-bad', '.desktop-runtime'):
            with self.assertRaises(RuntimeError):
                artifacts.cleanup_plan(self.root, identifier)

    def test_record_cannot_change_cleanup_scope(self):
        with artifacts.ArtifactRun('build', root=self.root) as run:
            pass
        record = self.read(run)
        record['work'] = '.desktop-runtime'
        (run.path / 'run.json').write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'record_invalid'):
            artifacts.cleanup_plan(self.root, run.record['id'])

    def test_real_link_inside_work_is_not_followed(self):
        destination = self.write(self.root / 'user-data/keep').parent
        with self.assertRaises(RuntimeError):
            with artifacts.ArtifactRun('build', root=self.root) as run:
                link = run.work / 'link'
                if os.name == 'nt':
                    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(destination)],
                        capture_output=True, cwd=self.root)
                    self.assertEqual(result.returncode, 0)
                else:
                    link.symlink_to(destination, target_is_directory=True)
        self.assertEqual((destination / 'keep').read_bytes(), b'synthetic')
        self.assertEqual(self.read(run)['state'], 'cleanup_failed')

    def test_capacity_gates_before_and_between_stages(self):
        with self.assertRaisesRegex(RuntimeError, 'capacity_exceeded'):
            with artifacts.ArtifactRun('build', root=self.root, work_limit=0):
                pass
        with self.assertRaisesRegex(RuntimeError, 'capacity_exceeded'):
            with artifacts.ArtifactRun('build', root=self.root, evidence_limit=8) as run:
                self.write(run.evidence / 'large', b'0' * 9)
                run.checkpoint()
        self.assertEqual(self.read(run)['failure_type'], 'RuntimeError')
        with patch.object(artifacts.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaisesRegex(RuntimeError, 'disk_space'):
                artifacts.check_capacity(self.root, work_limit=10**8, evidence_limit=10**8)

    def test_timeout_stops_own_process_before_cleanup(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                run.process([sys.executable, '-B', '-c', 'import time; time.sleep(30)'], timeout=0.15)
        self.assertIsNotNone(run.children[0].poll())
        self.assertFalse(run.work.exists())
        self.assertEqual(self.read(run)['failure_type'], 'TimeoutExpired')

    def test_live_child_prevents_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
            with artifacts.ArtifactRun('tests', root=self.root) as run:
                child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'])
                self.addCleanup(child.wait)
                self.addCleanup(child.kill)
                run.children.append(child)
                self.write(run.work / 'used-file')
        self.assertTrue(run.work.exists())

    def test_real_process_lock_and_forced_interrupt_stop_next_entry(self):
        code = ('import sys,time,os; from pathlib import Path; '
                'from scripts.local_artifacts import ArtifactRun; '
                'r=ArtifactRun("build",root=Path(sys.argv[1])); r.__enter__(); '
                '(r.work/"partial").write_bytes(b"partial"); '
                'print("ready",flush=True); time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root)], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with self.assertRaisesRegex(RuntimeError, 'operation_busy'):
                with artifacts.ArtifactRun('tests', root=self.root):
                    pass
            child.kill()
            child.wait(timeout=10)
            with self.assertRaisesRegex(RuntimeError, 'unfinished'):
                with artifacts.ArtifactRun('tests', root=self.root):
                    pass
            path, record = artifacts.records(self.root)[0]
            plan = artifacts.cleanup_plan(self.root, record['id'])
            self.assertTrue(any('partial' in p for files in plan['targets'].values() for p in files))
            with self.assertRaisesRegex(RuntimeError, 'stopped_confirmation'):
                artifacts.apply_plan(self.root, record['id'], plan['plan_sha256'])
            artifacts.apply_plan(self.root, record['id'], plan['plan_sha256'], stopped=True)
            self.assertFalse((path / 'work').exists())
            with artifacts.ArtifactRun('tests', root=self.root):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_environment_restored_and_scratch_cannot_escape(self):
        previous = {key: os.environ.get(key) for key in ('TEMP', 'TMP')}
        with artifacts.ArtifactRun('visual', root=self.root) as run:
            with artifacts.temporary_environment(run):
                self.assertEqual(Path(tempfile.gettempdir()), run.work / 'temp')
        self.assertEqual(previous, {key: os.environ.get(key) for key in previous})
        for relative in ('../outside', '.desktop-runtime', 'C:/outside'):
            with patch.dict(os.environ, ARXIVKALEID_TEST_SCRATCH=relative):
                with self.assertRaises(RuntimeError):
                    artifacts.test_scratch(self.root)

    def test_mock_build_keeps_old_files_and_produces_verified_output(self):
        old = self.write(self.root / ('dist/' + build.NAME + '/old'), b'old-dist')
        zip_old = self.write(self.root / ('release/' + build.NAME + '.zip'), b'old-zip')
        self.write(self.root / ('release/' + build.NAME + '.zip.sha256'), b'old-sha')
        frozen = {'commit': 'a' * 40, 'branch': 'main', 'purpose': 'public-release', 'working_tree_clean': True}
        with artifacts.ArtifactRun('build', root=self.root) as run:
            def fake_process(*args, **kwargs):
                self.write(run.work / 'dist/arXivKaleid/arXivKaleid.exe', b'new-exe')
                return subprocess.CompletedProcess(args, 0)
            with patch.object(build, 'ROOT', self.root), patch.object(build, 'prepare_resources'), \
                    patch.object(build, 'prepare_curl'), patch.object(build, 'inspect_python_archive', return_value={}), \
                    patch.object(build, 'copy_public_documents'), patch.object(licenses, 'collect_licenses'), \
                    patch.object(build, 'frozen_identity', return_value=frozen), \
                    patch.object(build, 'pinned_requirements', return_value={}), \
                    patch.object(build.importlib.metadata, 'version', return_value='test'), \
                    patch.object(build, 'verify_tree', side_effect=lambda p: {q.relative_to(p).as_posix(): artifacts.file_hash(q) for q in p.rglob('*') if q.is_file()}), \
                    patch.object(run, 'process', side_effect=fake_process):
                build.build_portable(frozen, run)
        self.assertFalse(run.work.exists())
        self.assertEqual((run.preserved / 'previous-dist/old').read_bytes(), b'old-dist')
        self.assertEqual((run.preserved / 'previous-release.zip').read_bytes(), b'old-zip')
        self.assertFalse(old.exists())
        self.assertEqual(self.read(run)['outputs']['sha256'], artifacts.file_hash(zip_old))
        with zipfile.ZipFile(zip_old) as archive:
            self.assertEqual(archive.read(build.NAME + '/arXivKaleid.exe'), b'new-exe')


class LicenseAndReleaseTests(ArtifactFixture):
    # 只继承隔离夹具，避免重复执行生命周期测试。
    def license_fixture(self):
        prefix = self.root / 'environment'
        metadata = {}
        for name in ('python', 'tzdata'):
            metadata[name] = {'name': name, 'version': '1', 'build': '0', 'sha256': name * 8}
            self.write(prefix / 'conda-meta' / (name + '.json'), json.dumps(metadata[name]).encode())
        archive = self.root / 'old-release.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr('old/licenses/components.json', json.dumps([
                {'name': n, 'version': '1', 'distributed_files': ['binary']} for n in metadata]))
            for name in metadata:
                stream.writestr('old/licenses/' + name + '/LICENSE', name + ' original license')
        output = self.root / '.desktop-build/inputs/conda-licenses/fixed/manifest.json'
        digest = artifacts.file_hash(archive)
        manifest = licenses.prepare_license_inputs(self.root, archive, digest, prefix, output)
        return archive, digest, output, manifest, metadata

    def test_license_manifest_exact_input_and_tamper_rejected(self):
        archive, digest, output, manifest, metadata = self.license_fixture()
        result = licenses.license_input_files(self.root, output, metadata)
        self.assertEqual(set(result), {'python', 'tzdata'})
        self.assertEqual(artifacts.file_hash(archive), digest)
        self.assertEqual(result['python'][0][1].read_text(), 'python original license')
        for mutate in (
            lambda m: m['packages'][0].update(build='different'),
            lambda m: m['packages'].pop(),
            lambda m: m['packages'][0]['files'][0].update(path='../outside'),
            lambda m: m['packages'][0]['files'][0].update(member='old/licenses/tzdata/LICENSE'),
            lambda m: m.update(archive_sha256='0' * 64),
        ):
            changed = copy.deepcopy(manifest)
            mutate(changed)
            output.write_text(json.dumps(changed), encoding='utf-8')
            with self.assertRaises((ValueError, RuntimeError)):
                licenses.license_input_files(self.root, output, metadata)
        output.write_text(json.dumps(manifest), encoding='utf-8')
        result['python'][0][1].write_text('tampered')
        with self.assertRaisesRegex(RuntimeError, 'hash_mismatch'):
            licenses.license_input_files(self.root, output, metadata)

    def test_license_missing_no_overwrite_and_source_preserved(self):
        archive, digest, output, manifest, metadata = self.license_fixture()
        with self.assertRaisesRegex(RuntimeError, 'output_invalid_or_exists'):
            licenses.prepare_license_inputs(self.root, archive, digest, self.root / 'environment', output)
        self.assertEqual(artifacts.file_hash(archive), digest)
        missing = artifacts.checked_path(self.root, manifest['packages'][0]['files'][0]['path'])
        missing.unlink()  # 只移除本测试刚创建的许可副本，原归档保持不变。
        with self.assertRaises(FileNotFoundError):
            licenses.license_input_files(self.root, output, metadata)

    def release_fixture(self):
        repo, tag, commit = 'owner/project', 'v1', 'a' * 40
        zip_path = self.root / 'arXivKaleid-1-windows-x64.zip'
        checksum = self.root / (zip_path.name + '.sha256')
        with zipfile.ZipFile(zip_path, 'w') as stream:
            stream.writestr(zip_path.stem + '/BUILD_INFO.json', json.dumps({'commit': commit,
                'purpose': 'public-release', 'branch': 'main', 'working_tree_clean': True, 'version': '1'}))
        checksum.write_text(artifacts.file_hash(zip_path) + '  ' + zip_path.name + '\n', encoding='ascii')
        release = dict(id=1, tag_name=tag, name='test', body='## 本次更新\n- 合成验证\nGPL-3.0-only\n' +
            f'https://github.com/{repo}/archive/refs/tags/{tag}.zip', draft=False, prerelease=True, immutable=True, assets=[])
        downloads = {}
        for index, path in enumerate((zip_path, checksum)):
            url = f'https://github.com/{repo}/releases/download/{tag}/{path.name}'
            downloads[url] = path.read_bytes()
            release['assets'].append(dict(id=index, name=path.name, size=path.stat().st_size,
                digest='sha256:' + artifacts.file_hash(path), state='uploaded', browser_download_url=url))
        return repo, tag, commit, zip_path, checksum, release, downloads

    def test_public_assets_mock_network_and_mismatch(self):
        repo, tag, commit, zip_path, checksum, release, downloads = self.release_fixture()
        result = release_check.verify_assets(release, repo, tag, commit, zip_path, checksum, get=downloads.__getitem__)
        self.assertTrue(result['assets_equal_local'])
        calls = []
        def wrong(url):
            calls.append(url)
            return b'bad'
        with self.assertRaisesRegex(RuntimeError, 'bytes_mismatch'):
            release_check.verify_assets(release, repo, tag, commit, zip_path, checksum, get=wrong)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(RuntimeError, 'build_identity_mismatch'):
            release_check.verify_assets(release, repo, tag, 'b' * 40, zip_path, checksum, get=downloads.__getitem__)

    def test_public_source_exact_bytes_and_inventory(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as stream:
            stream.writestr('project-tag/file.txt', b'content')
        with patch.object(release_check.subprocess, 'check_output', side_effect=[b'file.txt\n', b'content']):
            self.assertEqual(release_check.verify_source(data.getvalue(), self.root, 'a' * 40)['source_files'], 1)
        with patch.object(release_check.subprocess, 'check_output', side_effect=[b'file.txt\n', b'changed']):
            with self.assertRaisesRegex(RuntimeError, 'source_bytes_mismatch'):
                release_check.verify_source(data.getvalue(), self.root, 'a' * 40)

    def test_complete_public_check_mock_preserves_inputs_and_cleans_downloads(self):
        repo, tag, commit, zip_path, checksum, release, downloads = self.release_fixture()
        baseline = self.write(self.root / 'history.json', b'{"releases": [], "tags": {}}')
        source = io.BytesIO()
        with zipfile.ZipFile(source, 'w') as stream:
            stream.writestr('project-tag/file.txt', b'content')
        downloads[f'https://github.com/{repo}/archive/refs/tags/{tag}.zip'] = source.getvalue()
        calls = []
        def get(url):
            calls.append(url)
            if url in downloads:
                return downloads[url]
            if '/releases/tags/' in url:
                return json.dumps(release).encode()
            if '/git/ref/tags/' in url:
                return json.dumps({'object': {'type': 'commit', 'sha': commit}}).encode()
            return b'[]'
        with patch.object(release_check, 'ROOT', self.root), \
                patch.object(release_check, 'verify_source', return_value={'source_files': 1}):
            release_check.main(['--repo', repo, '--tag', tag, '--commit', commit, '--zip', zip_path.name,
                '--checksum', checksum.name, '--history-baseline', baseline.name], get=get)
        path, record = artifacts.records(self.root)[0]
        self.assertEqual(record['state'], 'succeeded')
        self.assertFalse((path / 'work').exists())
        self.assertEqual(len(record['downloads']), 3)
        self.assertTrue((path / 'evidence/release.json').exists())
        self.assertEqual(artifacts.file_hash(zip_path), record['protected_hashes'][zip_path.name])
        self.assertEqual(calls.count(f'https://api.github.com/repos/{repo}/releases/tags/{tag}'), 2)

    def test_history_baseline_change_stops_before_download(self):
        repo, tag, commit, zip_path, checksum, release, downloads = self.release_fixture()
        baseline = self.write(self.root / 'history.json', b'{"releases": [], "tags": {"old": "different"}}')
        calls = []
        def get(url):
            calls.append(url)
            return b'[]'
        with patch.object(release_check, 'ROOT', self.root):
            with self.assertRaisesRegex(RuntimeError, 'historical_release_changed'):
                release_check.main(['--repo', repo, '--tag', tag, '--commit', commit, '--zip', zip_path.name,
                    '--checksum', checksum.name, '--history-baseline', baseline.name], get=get)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(url.startswith('https://api.github.com/') for url in calls))
        path, record = artifacts.records(self.root)[0]
        self.assertEqual(record['state'], 'failed')
        self.assertFalse((path / 'work').exists())


if __name__ == '__main__':
    unittest.main()
