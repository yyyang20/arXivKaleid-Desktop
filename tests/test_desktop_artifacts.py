# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""只在自产隔离根中注入故障；不读取用户数据、不构建、不联网。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
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


@unittest.skipUnless(os.name == 'nt', 'Windows 进程身份测试')
class WindowsProcessTests(ArtifactFixture):
    def setUp(self):
        super().setUp()
        import ctypes
        from ctypes import wintypes

        # 只使用合成运行和 Win32 返回值，不触碰实际发行物或其他进程。
        with artifacts.ArtifactRun('tests', root=self.root) as run:
            pass
        self.run = run
        self.marker = self.write(run.work / 'partial')
        run.record.update(state='finishing', pid=101, children=[202],
                          created_utc='2020-01-01T00:00:00+00:00',
                          completed_utc='2020-01-02T00:00:00+00:00')
        run.save()
        self.plan = artifacts.cleanup_plan(self.root, run.record['id'])
        self.kernel = Mock()
        self.kernel.OpenProcess.side_effect = lambda access, inherit, pid: pid
        self.kernel.WaitForSingleObject.return_value = 0x102
        # FILETIME 使用 1601 年起的 100 ns 单位；两个时间分别位于记录之前和之后。
        self.old_time = 132222240000000000  # 2019-12-31 UTC
        self.new_time = 132224832000000000  # 2020-01-03 UTC
        self.creation = {101: self.new_time, 202: self.new_time}

        def get_times(handle, creation, exit_time, kernel_time, user_time):
            value = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
            value.dwHighDateTime = self.creation[handle] >> 32
            value.dwLowDateTime = self.creation[handle] & 0xffffffff
            return True

        self.kernel.GetProcessTimes.side_effect = get_times
        self.loader = patch('ctypes.WinDLL', return_value=self.kernel)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def apply(self):
        artifacts.apply_plan(self.root, self.run.record['id'], self.plan['plan_sha256'], stopped=True)

    def test_reused_windows_pids_allow_cleanup(self):
        self.apply()
        self.assertFalse(self.marker.exists())
        self.assertEqual([call.args[2] for call in self.kernel.OpenProcess.call_args_list], [101, 202])
        self.assertEqual([call.args[0] for call in self.kernel.CloseHandle.call_args_list], [101, 202])

    def test_live_windows_process_blocks_cleanup(self):
        for pid in (101, 202):
            with self.subTest(pid=pid):
                # 子进程可在运行创建后启动；它的上限必须使用完成时间。
                live_time = self.old_time if pid == 101 else 132223536000000000  # 2020-01-01 12:00 UTC
                self.creation = {101: self.new_time, 202: self.new_time, pid: live_time}
                with self.assertRaisesRegex(RuntimeError, 'artifact_resources_still_active'):
                    self.apply()
                self.assertTrue(self.marker.exists())
                self.assertEqual(self.read(self.run)['state'], 'finishing')
                self.assertEqual(self.kernel.OpenProcess.call_count, self.kernel.CloseHandle.call_count)
        # 旧记录没有子进程时间上限时，不凭 PID 推定其已经退出。
        self.creation = {101: self.new_time, 202: self.new_time}
        self.run.record['completed_utc'] = None
        self.run.save()
        with self.assertRaisesRegex(RuntimeError, 'artifact_resources_still_active'):
            self.apply()
        self.assertTrue(self.marker.exists())

    def test_unknown_windows_process_status_blocks_cleanup(self):
        cases = [
            ('open_denied', 'OpenProcess', 0),
            ('wait_failed', 'WaitForSingleObject', 0xffffffff),
            ('wait_unexpected', 'WaitForSingleObject', 1),
            ('times_failed', 'GetProcessTimes', False),
        ]
        for name, function, value in cases:
            with self.subTest(status=name), patch.object(self.kernel, function, Mock(return_value=value)), \
                    patch('ctypes.get_last_error', return_value=5):
                self.kernel.OpenProcess.reset_mock()
                self.kernel.CloseHandle.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'artifact_process_status_unknown'):
                    self.apply()
                self.assertTrue(self.marker.exists())
                self.assertEqual(self.read(self.run)['state'], 'finishing')
                self.assertEqual(self.kernel.CloseHandle.call_count, 0 if name == 'open_denied' else 1)
        for stamp in ('invalid', '2020-01-01T00:00:00'):
            with self.subTest(timestamp=stamp):
                self.run.record['created_utc'] = stamp
                self.run.save()
                with self.assertRaisesRegex(RuntimeError, 'artifact_process_status_unknown'):
                    self.apply()
                self.assertTrue(self.marker.exists())


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
                                build.build_portable({'commit': 'a' * 40}, run)
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

    def test_all_portable_stages_mocked_then_nine_copies_released(self):
        import validate_windows_portable as validator
        source = self.root / 'dist/synthetic'
        self.write(source / 'arXivKaleid.exe', b'synthetic executable')
        self.write(source / '_internal/prompts/relevance_round2_v18.txt')
        self.write(source / '_internal/prompts/research_prompt_round1_v2.txt')
        self.write(source / '_internal/docs/desktop/USER_GUIDE.md')
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
                        'synthetic_dpapi_recovered': True, 'version_metadata_restored_exactly': True,
                        'user_guide_restored': True, 'history_restored_exactly': True,
                        'deleted_history_stays_deleted': True, 'research_requirements_restored_exactly': True,
                        'categories_restored_exactly': True}).encode())
                    code = 0
                elif '--visual-qa' in command:
                    mode = copy_root.name.split(' ')[-1]
                    dpr = 1.5 if mode == 'native' else int(mode.rsplit('-', 1)[-1]) / 100
                    self.write(copy_root / 'runtime/work/screen.png')
                    self.write(copy_root / 'runtime/work/portable-check.json', json.dumps({'ok': True,
                        'round2_user_count_and_model_ranking': True,
                        'user_guide_loaded': True,
                        'visual_qa': {'user_guide': {'keyboard_link': True},
                                      'history': {'calendar_states': True, 'calendar_delete_refresh': True,
                                                  'calendar_read_error_visible': True},
                                      'categories': {'count': 51, 'snapshot_frozen': True},
                                      'screen': 'primary', 'captures': [{'screen': 'primary', 'dpr': dpr}]}}).encode())
                    code = 0
                else:
                    code = 2 if copy_root.name.startswith('隔离副本') else 1
                return subprocess.CompletedProcess(command, code)
            with patch.object(validator, 'ROOT', self.root), patch.object(validator, 'verify_tree'), \
                    patch.object(validator, 'close_own_window', return_value=True), \
                    patch.object(validator.subprocess, 'Popen', side_effect=lambda *a, **kw: Restart()), \
                    patch.object(run, 'process', side_effect=process):
                validator.validate_portable(run, source, {'commit': 'a' * 40}, type('Args', (), {'arxiv': False})())
                self.assertEqual(len(list(run.work.iterdir())), 14)  # 九份副本及五份过程日志。
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
                for name in build.RESOURCES:
                    self.write(run.work / 'dist/arXivKaleid/_internal' / name, b'synthetic-resource')
                return subprocess.CompletedProcess(args, 0)
            with patch.object(build, 'ROOT', self.root), patch.object(build, 'prepare_resources'), \
                    patch.object(build, 'reusable_output', return_value=None), \
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
            self.assertEqual(json.loads(archive.read(build.NAME + '/BUILD_INFO.json'))['build_run_id'], run.record['id'])


class CandidateCopyTests(ArtifactFixture):
    """真实 ZIP／文件删除仅在合成根内；完整链路不构建 EXE、不联网。"""
    def setUp(self):
        super().setUp()
        self.process_guard = patch.object(artifacts, 'candidate_process_active', return_value=False)
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)

    def distribution(self, payload, commit, purpose=artifacts.TECHNICAL_PURPOSE, build_id=None):
        directory = self.root / 'dist' / build.NAME
        self.write(directory / 'arXivKaleid.exe', payload)
        identity = dict(commit=commit, purpose=purpose, working_tree_clean=True,
                        branch='codex/release', version=build.__version__)
        if purpose == 'public-release':
            identity['release_workflow'] = build.RELEASE_WORKFLOW
        if build_id is not None:
            identity['build_run_id'] = build_id
        self.write(directory / 'BUILD_INFO.json', json.dumps(identity).encode())
        archive = self.root / 'release' / (build.NAME + '.zip')
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, 'w') as stream:
            for p in directory.rglob('*'):
                if p.is_file():
                    stream.write(p, build.NAME + '/' + p.relative_to(directory).as_posix())
        checksum = self.write(archive.with_suffix('.zip.sha256'),
            (artifacts.file_hash(archive) + '  ' + archive.name + '\n').encode())
        return directory, archive, checksum

    def pair(self, purpose=artifacts.TECHNICAL_PURPOSE):
        old, old_zip, old_sha = self.distribution(b'old' * 8192, '1' * 40, purpose)
        with artifacts.ArtifactRun('build', root=self.root) as run:
            run.record['commit'] = '2' * 40
            run.preserve_move(old, 'previous-dist')
            run.preserve_move(old_zip, 'previous-release.zip')
            run.preserve_move(old_sha, 'previous-release.sha256')
            current, archive, checksum = self.distribution(b'new', '2' * 40, purpose=purpose, build_id=run.record['id'])
            files = artifacts.candidate_files(self.root, current)
            run.record['outputs'] = dict(zip=archive.relative_to(self.root).as_posix(),
                checksum=checksum.relative_to(self.root).as_posix(), dist=current.relative_to(self.root).as_posix(),
                sha256=artifacts.file_hash(archive), inventory=files)
            run.save()
            artifacts.register_candidate_copy(run)
        return run

    def validate(self, build_run, *, automatic=False):
        # 反例夹具可暂停自动收尾，在提交验证证明前注入篡改；生产入口始终自动执行。
        with patch.object(artifacts, 'accept_portable_validation',
                          wraps=artifacts.accept_portable_validation if automatic else lambda *args: None):
            return self.validation_record(build_run)

    def validation_record(self, build_run):
        output = self.read(build_run)['outputs']
        current = self.root / output['dist']
        with artifacts.ArtifactRun('portable', root=self.root, review=True) as run:
            binding = artifacts.bind_portable_build(self.root, current, output['inventory'])
            run.record.update(commit='2' * 40, portable_build=binding, portable_complete=True)
            summary = dict(commit='2' * 40, complete=True, portable_build=binding,
                missing_resource_rejected=True, tampered_requirements_rejected=True,
                missing_user_guide_rejected=True, tampered_user_guide_rejected=True,
                used_runtime_rejected=True, clean_distribution_preserved=True,
                runs=[dict(ok=True, dpi_mode='native' if d is None else 'simulated', requested_dpr=d,
                           restart_cycles=3, synthetic_dpapi_restart=True, python_on_path=False)
                      for d in (None, 1, 1.25, 1.5, 2)])
            report = self.write(run.evidence / 'validation.json', json.dumps(summary).encode())
            self.write(run.evidence / 'screen.png')
            run.record['portable_report_sha256'] = artifacts.file_hash(report)
            run.save()
        return run

    def approve(self, run):
        # 完成夹具暂停的自动验证收尾，再单独检查发布人工确认入口。
        with artifacts.operation_lock(self.root):
            artifacts.accept_portable_validation(self.root, self.read(run))
        artifacts.close_review(self.root, run.record['id'])

    def test_release_approval_binds_exact_checksum_and_never_borrows_another_commit(self):
        build_run = self.pair('public-release')
        review = self.validate(build_run, automatic=True)
        output = self.read(build_run)['outputs']
        archive, checksum = self.root / output['zip'], self.root / output['checksum']
        with self.assertRaisesRegex(RuntimeError, 'not_approved'):
            artifacts.approved_portable(self.root, review.record['id'], '2' * 40, archive, checksum)
        artifacts.close_review(self.root, review.record['id'])
        proof = artifacts.approved_portable(self.root, review.record['id'], '2' * 40, archive, checksum)
        self.assertEqual(proof['checksum_sha256'], artifacts.file_hash(checksum))
        with self.assertRaisesRegex(RuntimeError, 'artifact_mismatch'):
            artifacts.approved_portable(self.root, review.record['id'], '3' * 40, archive, checksum)
        original = checksum.read_bytes()
        checksum.write_bytes(original + b'\n')  # 语义相同也不是人工验收过的同一份校验文件。
        with self.assertRaisesRegex(RuntimeError, 'artifact_mismatch'):
            artifacts.approved_portable(self.root, review.record['id'], '2' * 40, archive, checksum)
        checksum.write_bytes(original)
        archive.write_bytes(archive.read_bytes() + b'changed')
        with self.assertRaisesRegex(RuntimeError, 'checksum_mismatch'):
            artifacts.approved_portable(self.root, review.record['id'], '2' * 40, archive, checksum)

    def test_repeated_build_reuses_original_and_content_change_allows_new_c(self):
        build_run = self.pair('public-release')
        review = self.validate(build_run, automatic=True)
        artifacts.close_review(self.root, review.record['id'])
        frozen = dict(commit='2' * 40)
        with patch.object(build, 'ROOT', self.root), patch.object(build, 'verify_tree'), \
                patch.object(build, 'verify_merge_identity', return_value=[]), \
                patch.object(build, 'prepare_resources') as prepare:
            with artifacts.operation_lock(self.root):
                first = build.reusable_output(frozen)
            for commit in ('2' * 40, '3' * 40):
                with artifacts.ArtifactRun('build', root=self.root) as repeat:
                    build.build_portable(dict(commit=commit), repeat)
            self.assertEqual(first['build_id'], build_run.record['id'])
            self.assertEqual(first['commit'], '2' * 40)
            prepare.assert_not_called()
            # 原构建记录仍存在，复用审计不会轮换掉发布资格来源。
            self.assertEqual(self.read(build_run)['outputs']['sha256'], first['sha256'])
        with patch.object(build, 'ROOT', self.root), \
                patch.object(build, 'verify_merge_identity', side_effect=RuntimeError('release_content_changed')):
            with artifacts.operation_lock(self.root):
                self.assertIsNone(build.reusable_output(dict(commit='3' * 40)))
        checksum = self.root / self.read(build_run)['outputs']['checksum']
        checksum.unlink()
        with patch.object(build, 'ROOT', self.root), artifacts.operation_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                build.reusable_output(frozen)

    def test_new_c_validation_can_follow_unapproved_complete_review(self):
        first = self.pair('public-release')
        old_review = self.validate(first, automatic=True)
        self.assertNotIn('release_review_approved', self.read(old_review))
        # 新构建只替换合成包；旧待审证据继续保护，不建立拒绝状态。
        with artifacts.ArtifactRun('build', root=self.root) as new:
            output = self.read(first)['outputs']
            directory, archive, checksum = artifacts.output_paths(self.root, output)
            for path, name in ((directory, 'previous-dist'), (archive, 'previous-release.zip'),
                               (checksum, 'previous-release.sha256')):
                new.preserve_move(path, name)
            current, archive, checksum = self.distribution(b'new-C', '3' * 40,
                purpose='public-release', build_id=new.record['id'])
            new.record.update(commit='3' * 40, outputs=dict(dist=current.relative_to(self.root).as_posix(),
                zip=archive.relative_to(self.root).as_posix(), checksum=checksum.relative_to(self.root).as_posix(),
                inventory=artifacts.candidate_files(self.root, current), sha256=artifacts.file_hash(archive)))
            new.save()
        with artifacts.ArtifactRun('portable', root=self.root, review=True):
            pass
        with self.assertRaisesRegex(RuntimeError, 'not_approved'):
            artifacts.approved_portable(self.root, old_review.record['id'], '3' * 40, archive, checksum)

    def test_preflight_and_public_readback_use_same_approved_package(self):
        build_run = self.pair('public-release')
        review = self.validate(build_run, automatic=True)
        artifacts.close_review(self.root, review.record['id'])
        output = self.read(build_run)['outputs']
        archive, checksum = self.root / output['zip'], self.root / output['checksum']
        baseline = self.write(self.root / 'history.json', b'{"releases": [], "tags": {}}')
        repo, tag, commit = 'owner/project', 'v' + build.__version__, '2' * 40
        ref = dict(type='commit', sha='3' * 40)
        calls = []
        def get(url):
            calls.append(url)
            if url == f'https://api.github.com/repos/{repo}':
                return b'{"private": false, "visibility": "public"}'
            return json.dumps({'object': ref} if url.endswith('/git/ref/heads/main') else []).encode()
        args = ['--repo', repo, '--tag', tag, '--commit', commit, '--zip', output['zip'],
                '--checksum', output['checksum'], '--history-baseline', baseline.name,
                '--preflight', '--review-run-id', review.record['id']]
        with patch.object(release_check, 'ROOT', self.root), \
                patch.object(release_check, 'verify_merge_identity', return_value=['README.md']) as merged:
            release_check.main(args, get=get)
        merged.assert_called_once_with(self.root, commit, ref['sha'])
        self.assertFalse(any('/releases/tags/' in url for url in calls))
        release = dict(assets=[])
        for path in (archive, checksum):
            release['assets'].append(dict(name=path.name, size=path.stat().st_size,
                digest='sha256:' + artifacts.file_hash(path)))
        with self.assertRaisesRegex(RuntimeError, 'review_required'):
            release_check.verify_assets(release, tag, commit, archive, checksum)
        result = release_check.verify_assets(release, tag, commit, archive, checksum,
            review_run_id=review.record['id'], root=self.root)
        self.assertTrue(result['asset_digests_match_local'])
        with self.assertRaisesRegex(RuntimeError, 'not_approved'):
            release_check.verify_assets(release, tag, commit, archive, checksum,
                review_run_id=build_run.record['id'], root=self.root)

    def test_preflight_rejects_unapproved_or_changed_merge_and_remote_main(self):
        parent = self.root
        for scenario in ('unapproved', 'content', 'moving-main'):
            root = parent / scenario
            root.mkdir()
            with self.subTest(scenario=scenario), patch.object(self, 'root', root):
                build_run = self.pair('public-release')
                review = self.validate(build_run, automatic=True)
                if scenario != 'unapproved':
                    artifacts.close_review(root, review.record['id'])
                output = self.read(build_run)['outputs']
                baseline = self.write(root / 'history.json', b'{"releases": [], "tags": {}}')
                args = ['--repo', 'owner/project', '--tag', 'v' + build.__version__,
                    '--commit', '2' * 40, '--zip', output['zip'], '--checksum', output['checksum'],
                    '--history-baseline', baseline.name, '--preflight', '--review-run-id', review.record['id']]
                main_calls = []
                def get(url):
                    if url == 'https://api.github.com/repos/owner/project':
                        return b'{"private": false, "visibility": "public"}'
                    if url.endswith('/git/ref/heads/main'):
                        main_calls.append(url)
                        return json.dumps({'object': dict(type='commit',
                            sha=('4' if scenario == 'moving-main' and len(main_calls) > 1 else '3') * 40)}).encode()
                    return b'[]'
                failure = ('not_approved' if scenario == 'unapproved' else
                           'content_changed' if scenario == 'content' else 'changed_during_check')
                with patch.object(release_check, 'ROOT', root), \
                        patch.object(release_check, 'verify_merge_identity',
                            side_effect=RuntimeError('release_content_changed') if scenario == 'content' else None,
                            return_value=[]):
                    with self.assertRaisesRegex(RuntimeError, failure):
                        release_check.main(args, get=get)
                if scenario == 'unapproved':
                    self.assertEqual(main_calls, [])

    def test_legacy_approval_without_checksum_proof_is_not_new_release_qualification(self):
        build_run = self.pair('public-release')
        review = self.validate(build_run, automatic=True)
        artifacts.close_review(self.root, review.record['id'])
        record = self.read(review)
        record.pop('review_checksum_sha256')
        artifacts.save_record(self.root, record)
        output = self.read(build_run)['outputs']
        with self.assertRaisesRegex(RuntimeError, 'not_approved'):
            artifacts.approved_portable(self.root, review.record['id'], '2' * 40,
                self.root / output['zip'], self.root / output['checksum'])

    def test_automatic_cleanup_does_not_wait_for_human_release_review(self):
        build_run = self.pair()
        old = build_run.preserved / 'previous-dist'
        archive = build_run.preserved / 'previous-release.zip'
        checksum = build_run.preserved / 'previous-release.sha256'
        hashes = {p: artifacts.file_hash(p) for p in (archive, checksum)}
        review = self.validate(build_run, automatic=True)
        self.assertFalse(old.exists())
        self.assertEqual({p: artifacts.file_hash(p) for p in hashes}, hashes)
        self.assertEqual(self.read(build_run)['candidate_copy']['status'], 'deleted')
        record = self.read(review)
        self.assertEqual(record['state'], 'awaiting_review')
        self.assertTrue(record['review_pending'])
        self.assertNotIn('release_review_approved', record)
        self.assertTrue((review.evidence / 'validation.json').exists())
        self.assertFalse(artifacts.cleanup_plan(self.root, review.record['id'], review_complete=True)['targets'])
        # 保留待发布人工验收材料，仍允许下一份候选进行技术验证。
        with artifacts.ArtifactRun('portable', root=self.root, review=True):
            pass
        artifacts.close_review(self.root, review.record['id'])
        self.assertTrue(self.read(review)['release_review_approved'])

    def test_automatic_validation_rechecks_current_zip_before_cleanup(self):
        build_run = self.pair()
        real_accept = artifacts.accept_portable_validation
        def changed(root, record):
            self.distribution(b'changed-at-same-commit', '2' * 40)
            real_accept(root, record)
        with patch.object(artifacts, 'accept_portable_validation', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
                self.validation_record(build_run)
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        self.assertNotIn('candidate_validation', self.read(build_run))

    def test_automatic_validation_failure_preserves_copy_and_stops_entry(self):
        build_run = self.pair()
        (build_run.preserved / 'previous-release.sha256').unlink()
        with self.assertRaisesRegex(RuntimeError, 'artifact_cleanup_failed'):
            self.validate(build_run, automatic=True)
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        self.assertEqual(self.read(build_run)['state'], 'cleanup_failed')
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_automatic_failed_or_incomplete_validation_does_not_register_eligibility(self):
        build_run = self.pair()
        with self.assertRaisesRegex(RuntimeError, 'synthetic_failure'):
            with artifacts.ArtifactRun('portable', root=self.root, review=True) as run:
                output = self.read(build_run)['outputs']
                run.record.update(portable_build=artifacts.bind_portable_build(
                    self.root, self.root / output['dist'], output['inventory']), portable_complete=True)
                raise RuntimeError('synthetic_failure')
        self.assertNotIn('candidate_validation', self.read(build_run))
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        record = self.read(run)
        record.update(state='released', review_pending=False)
        artifacts.save_record(self.root, record)
        with artifacts.ArtifactRun('portable', root=self.root, review=True) as incomplete:
            incomplete.record.update(portable_build=run.record['portable_build'], portable_complete=False)
        self.assertNotIn('candidate_validation', self.read(build_run))
        self.assertTrue((build_run.preserved / 'previous-dist').exists())

    def test_close_review_deletes_only_verified_copy_keeps_zip_evidence_and_current(self):
        build_run = self.pair()
        review = self.validate(build_run)
        protected = [build_run.preserved / 'previous-release.zip', build_run.preserved / 'previous-release.sha256',
                     review.evidence / 'validation.json', review.evidence / 'screen.png',
                     self.root / self.read(build_run)['outputs']['zip']]
        hashes = {p: artifacts.file_hash(p) for p in protected}
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        self.approve(review)
        self.assertFalse((build_run.preserved / 'previous-dist').exists())
        self.assertEqual({p: artifacts.file_hash(p) for p in protected}, hashes)
        record = self.read(build_run)['candidate_copy']
        self.assertEqual(record['status'], 'deleted')
        self.assertEqual(record['deleted_files'], 2)
        self.assertEqual(record['validation_run'], review.record['id'])
        self.assertEqual(self.read(review)['state'], 'released')
        self.assertFalse(artifacts.cleanup_plan(self.root, review.record['id'])['targets'])
        with artifacts.ArtifactRun('tests', root=self.root):
            pass  # 重复入口不重复删除。

    def test_pending_or_failed_portable_never_unlocks_copy(self):
        build_run = self.pair()
        review = self.validate(build_run)
        with artifacts.ArtifactRun('tests', root=self.root):
            pass
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        record = self.read(review)
        record.update(state='failed', outcome='failed', failure_type='RuntimeError')
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'not_eligible'):
            self.approve(review)
        self.assertTrue((build_run.preserved / 'previous-dist').exists())

    def test_incomplete_or_changed_validation_rejected(self):
        build_run = self.pair()
        review = self.validate(build_run)
        record = self.read(review)
        record['portable_complete'] = False
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'not_eligible'):
            self.approve(review)
        record['portable_complete'] = True
        artifacts.save_record(self.root, record)
        self.write(review.evidence / 'screen.png', b'changed')
        with self.assertRaisesRegex(RuntimeError, 'not_eligible'):
            self.approve(review)

    def test_partial_validation_even_with_complete_marker_cannot_close(self):
        build_run = self.pair()
        review = self.validate(build_run)
        report = review.evidence / 'validation.json'
        summary = json.loads(report.read_text())
        summary['runs'].pop()
        report.write_text(json.dumps(summary), encoding='utf-8')
        record = self.read(review)
        record['portable_report_sha256'] = artifacts.file_hash(report)
        record['evidence_hashes'] = artifacts.inventory(self.root, review.evidence, hashes=True)
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'binding_changed'):
            self.approve(review)

    def test_close_review_respects_process_lock(self):
        build_run = self.pair()
        review = self.validate(build_run)
        with artifacts.operation_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, 'operation_busy'):
                self.approve(review)
        self.assertTrue(self.read(review)['review_pending'])

    def test_extra_credential_in_evidence_is_rejected_before_hash_read(self):
        build_run = self.pair()
        review = self.validate(build_run)
        extra = self.write(review.evidence / 'secret.dat')
        real_hash = artifacts.file_hash
        def checked_hash(path):
            self.assertNotEqual(path, extra)
            return real_hash(path)
        with patch.object(artifacts, 'file_hash', side_effect=checked_hash):
            with self.assertRaisesRegex(RuntimeError, 'not_eligible'):
                self.approve(review)
        self.assertTrue(extra.exists())

    def test_same_commit_different_zip_cannot_reuse_validation(self):
        build_run = self.pair()
        review = self.validate(build_run)
        self.distribution(b'new-but-different', '2' * 40)
        with self.assertRaisesRegex(RuntimeError, 'manifest_changed'):
            self.approve(review)
        self.assertTrue((build_run.preserved / 'previous-dist').exists())

    def test_binding_records_build_id_and_zip_and_rejects_wrong_build_info(self):
        build_run = self.pair()
        review = self.validate(build_run)
        binding = self.read(review)['portable_build']
        self.assertEqual(binding['build_id'], build_run.record['id'])
        self.assertEqual(binding['zip_sha256'], self.read(build_run)['outputs']['sha256'])
        source = self.root / self.read(build_run)['outputs']['dist']
        identity = json.loads((source / 'BUILD_INFO.json').read_text())
        identity['build_run_id'] = 'build-' + '0' * 32
        (source / 'BUILD_INFO.json').write_text(json.dumps(identity), encoding='utf-8')
        self.assertIsNone(artifacts.bind_portable_build(self.root, source, artifacts.candidate_files(self.root, source)))

    def test_public_distribution_and_unmarked_history_stay_protected(self):
        for purpose in ('public-release', artifacts.TECHNICAL_PURPOSE):
            with self.subTest(purpose=purpose), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                original = self.root
                self.root = Path(scratch)
                try:
                    build_run = self.pair(purpose)
                    if purpose == artifacts.TECHNICAL_PURPOSE:
                        record = self.read(build_run)
                        record.pop('candidate_copy')
                        artifacts.save_record(self.root, record)
                    review = self.validate(build_run)
                    self.approve(review)
                    self.assertTrue((build_run.preserved / 'previous-dist').exists())
                finally:
                    self.root = original

    def test_tampered_or_missing_archives_stop_without_deleting_copy(self):
        for name in ('previous-release.zip', 'previous-release.sha256'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                original = self.root
                self.root = Path(scratch)
                try:
                    build_run = self.pair()
                    review = self.validate(build_run)
                    (build_run.preserved / name).unlink()
                    with self.assertRaisesRegex(RuntimeError, 'archive_missing'):
                        self.approve(review)
                    self.assertTrue((build_run.preserved / 'previous-dist').exists())
                    self.assertEqual(self.read(build_run)['state'], 'cleanup_failed')
                finally:
                    self.root = original

    def test_archive_or_checksum_tampering_is_not_accepted(self):
        for name in ('previous-release.zip', 'previous-release.sha256'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                original = self.root
                self.root = Path(scratch)
                try:
                    build_run = self.pair()
                    review = self.validate(build_run)
                    self.write(build_run.preserved / name, b'tampered')
                    with self.assertRaisesRegex(RuntimeError, 'checksum_mismatch'):
                        self.approve(review)
                    self.assertTrue((build_run.preserved / 'previous-dist').exists())
                finally:
                    self.root = original

    def test_user_data_and_extra_files_never_read_or_deleted(self):
        for name in ('runtime/config/secret.dat', 'secret.dat', 'user.sqlite', 'paper.pdf', 'extra.txt'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as scratch:
                original = self.root
                self.root = Path(scratch)
                try:
                    build_run = self.pair()
                    review = self.validate(build_run)
                    extra = self.write(build_run.preserved / 'previous-dist' / name)
                    real_hash = artifacts.file_hash
                    def no_sensitive_read(path):
                        self.assertNotEqual(path, extra)
                        return real_hash(path)
                    with patch.object(artifacts, 'file_hash', side_effect=no_sensitive_read):
                        with self.assertRaisesRegex(RuntimeError, 'user_data|manifest_changed'):
                            self.approve(review)
                    self.assertTrue(extra.exists())
                finally:
                    self.root = original

    def test_active_reference_defers_cleanup_then_next_entry_reclaims_before_capacity(self):
        build_run = self.pair()
        review = self.validate(build_run)
        old = build_run.preserved / 'previous-dist'
        with artifacts.ArtifactRun('visual', root=self.root, review=True) as reference:
            reference.protect(old)
        self.approve(review)
        self.assertTrue(old.exists())
        self.assertEqual(self.read(build_run)['candidate_copy']['retention_reason'], 'active_reference_or_process')
        record = self.read(reference)
        record.update(state='released', review_pending=False)
        artifacts.save_record(self.root, record)
        bytes_before = sum(artifacts.inventory(self.root, self.root / artifacts.REGISTRY).values())
        with artifacts.ArtifactRun('tests', root=self.root, work_limit=bytes_before-1):
            pass
        self.assertFalse(old.exists())

    def test_manual_evidence_deletion_does_not_count_as_approval(self):
        build_run = self.pair()
        review = self.validate(build_run)
        plan = artifacts.cleanup_plan(self.root, review.record['id'], review_complete=True)
        with self.assertRaisesRegex(RuntimeError, 'use_close_review'):
            artifacts.apply_plan(self.root, review.record['id'], plan['plan_sha256'], review_complete=True)
        self.assertTrue((review.evidence / 'validation.json').exists())

    def test_delete_failure_and_interrupt_protect_residue_and_block_next_entry(self):
        build_run = self.pair()
        review = self.validate(build_run)
        with patch.object(artifacts, 'remove_exact', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.approve(review)
        self.assertEqual(self.read(build_run)['state'], 'cleanup_failed')
        self.assertTrue((build_run.preserved / 'previous-dist').exists())
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_interrupt_during_delete_leaves_rotating_and_requires_manual_recovery(self):
        build_run = self.pair()
        review = self.validate(build_run)
        with patch.object(artifacts, 'remove_exact', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.approve(review)
        self.assertEqual(self.read(build_run)['state'], 'rotating')
        with self.assertRaisesRegex(RuntimeError, 'unfinished'):
            with artifacts.ArtifactRun('tests', root=self.root):
                pass

    def test_live_process_and_unknown_status_prevent_approval(self):
        build_run = self.pair()
        review = self.validate(build_run)
        with patch.object(artifacts, 'candidate_process_active', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'resources_still_active'):
                self.approve(review)
        with patch.object(artifacts, 'candidate_process_active', side_effect=RuntimeError('status_unknown')):
            with self.assertRaisesRegex(RuntimeError, 'status_unknown'):
                self.approve(review)
        self.assertTrue(self.read(review)['review_pending'])

    def test_zip_file_set_and_hash_are_both_checked(self):
        build_run = self.pair()
        old = build_run.preserved / 'previous-dist'
        archive = build_run.preserved / 'previous-release.zip'
        checksum = build_run.preserved / 'previous-release.sha256'
        files = self.read(build_run)['candidate_copy']['files']
        for wrong_name in ('extra.txt', 'arXivKaleid.exe'):
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr(build.NAME + '/' + wrong_name, b'wrong')
                if wrong_name == 'arXivKaleid.exe':
                    stream.write(old / 'BUILD_INFO.json', build.NAME + '/BUILD_INFO.json')
            self.write(checksum, (artifacts.file_hash(archive) + '  ' + build.NAME + '.zip\n').encode())
            with self.assertRaisesRegex(RuntimeError, 'zip_manifest_changed|zip_hash_changed'):
                artifacts.verify_candidate_archive(self.root, old, archive, checksum, files)

    def test_real_junction_is_rejected_without_following_user_files(self):
        build_run = self.pair()
        review = self.validate(build_run)
        user = self.write(self.root / 'user/keep')
        link = build_run.preserved / 'previous-dist/link'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(user.parent)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(user.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '目录链接|link'):
            self.approve(review)
        self.assertEqual(user.read_bytes(), b'synthetic')

    def test_record_cannot_redirect_delete_scope(self):
        build_run = self.pair()
        review = self.validate(build_run)
        user = self.write(self.root / 'user/keep')
        record = self.read(build_run)
        record['candidate_copy']['directory'] = 'user'
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'delete_scope'):
            self.approve(review)
        self.assertTrue(user.exists())

    def test_output_record_cannot_redirect_read_to_user_directory(self):
        build_run = self.pair()
        review = self.validate(build_run)
        record = self.read(build_run)
        record['outputs']['dist'] = '.desktop-runtime'
        artifacts.save_record(self.root, record)
        with self.assertRaisesRegex(RuntimeError, 'output_scope_invalid'):
            self.approve(review)


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
        for index, path in enumerate((zip_path, checksum)):
            release['assets'].append(dict(id=index, name=path.name, size=path.stat().st_size,
                digest='sha256:' + artifacts.file_hash(path)))
        return repo, tag, commit, zip_path, checksum, release

    def test_public_assets_compare_both_local_hashes_without_network(self):
        repo, tag, commit, zip_path, checksum, release = self.release_fixture()
        with patch.object(release_check, 'github_metadata_get', side_effect=AssertionError('unexpected network')):
            result = release_check.verify_assets(release, tag, commit, zip_path, checksum)
        self.assertTrue(result['asset_digests_match_local'])
        self.assertEqual({a['name']: a['local_sha256'] for a in result['assets']},
                         {p.name: artifacts.file_hash(p) for p in (zip_path, checksum)})
        with self.assertRaisesRegex(RuntimeError, 'build_identity_mismatch'):
            release_check.verify_assets(release, tag, 'b' * 40, zip_path, checksum)

    def test_public_assets_reject_missing_invalid_or_mismatched_digests(self):
        for index in (0, 1):
            for digest in (None, '', 1, 'md5:' + 'a' * 32, 'sha256:' + 'a' * 63,
                           'sha256:' + 'A' * 64, 'sha256:' + '0' * 64):
                with self.subTest(asset=index, digest=digest):
                    repo, tag, commit, zip_path, checksum, release = self.release_fixture()
                    release['assets'][index]['digest'] = digest
                    error = 'digest_mismatch' if digest == 'sha256:' + '0' * 64 else 'digest_invalid'
                    with self.assertRaisesRegex(RuntimeError, error):
                        release_check.verify_assets(release, tag, commit, zip_path, checksum)
            release['assets'][index].pop('digest')
            with self.assertRaisesRegex(RuntimeError, 'digest_invalid'):
                release_check.verify_assets(release, tag, commit, zip_path, checksum)

    def test_public_assets_reject_missing_extra_duplicate_or_wrong_names(self):
        for scenario in ('missing', 'extra', 'duplicate', 'wrong'):
            with self.subTest(scenario=scenario):
                repo, tag, commit, zip_path, checksum, release = self.release_fixture()
                if scenario == 'missing':
                    release['assets'].pop()
                elif scenario == 'extra':
                    release['assets'].append(dict(release['assets'][0], name='extra.zip'))
                else:
                    release['assets'][1]['name'] = zip_path.name if scenario == 'duplicate' else 'wrong.sha256'
                with self.assertRaisesRegex(RuntimeError, 'assets_invalid'):
                    release_check.verify_assets(release, tag, commit, zip_path, checksum)

    def metadata_fixture(self):
        repo, tag, commit, zip_path, checksum, release = self.release_fixture()
        baseline = self.write(self.root / 'history.json', b'{"releases": [], "tags": {}}')
        api = f'https://api.github.com/repos/{repo}'
        responses = {api: {'private': False, 'visibility': 'public'},
                     api + '/releases/tags/' + tag: release,
                     api + '/git/ref/tags/' + tag: {'object': {'type': 'commit', 'sha': commit}},
                     api + '/releases?per_page=100&page=1': [],
                     api + '/git/matching-refs/tags/?per_page=100&page=1': []}
        args = ['--repo', repo, '--tag', tag, '--commit', commit, '--zip', zip_path.name,
                '--checksum', checksum.name, '--history-baseline', baseline.name]
        return api, args, responses

    def test_complete_public_check_only_requests_metadata_and_preserves_inputs(self):
        api, args, responses = self.metadata_fixture()
        # 正文要求在发布前检查，发布后不以正文或 immutable 作为额外门槛。
        release = responses[api + '/releases/tags/v1']
        release.update(body='', immutable=False)
        calls = []
        def get(url):
            calls.append(url)
            self.assertTrue(url.startswith(api))
            return json.dumps(responses[url]).encode()
        with patch.object(release_check, 'ROOT', self.root):
            release_check.main(args, get=get)
        path, record = artifacts.records(self.root)[0]
        self.assertEqual(record['state'], 'succeeded')
        self.assertFalse((path / 'work').exists())
        self.assertNotIn('downloads', record)
        self.assertFalse(list(self.root.rglob('*.download')))
        result = json.loads((path / 'evidence/release.json').read_text(encoding='utf-8'))
        self.assertEqual(result['verification'], 'github-metadata')
        self.assertTrue(result['release_public'])
        for obsolete in ('anonymous', 'source_files', 'source_sha256', 'assets_equal_local'):
            self.assertNotIn(obsolete, result)
        for name, digest in record['protected_hashes'].items():
            self.assertEqual(artifacts.file_hash(self.root / name), digest)
        self.assertEqual(calls.count(api + '/releases/tags/v1'), 2)

    def test_public_check_rejects_private_repository_draft_and_wrong_tag(self):
        for scenario in ('private', 'draft', 'tag-name', 'tag-target'):
            root = self.root / scenario
            root.mkdir()
            with self.subTest(scenario=scenario), patch.object(self, 'root', root):
                api, args, responses = self.metadata_fixture()
                if scenario == 'private':
                    responses[api].update(private=True, visibility='private')
                elif scenario == 'draft':
                    responses[api + '/releases/tags/v1']['draft'] = True
                elif scenario == 'tag-name':
                    responses[api + '/releases/tags/v1']['tag_name'] = 'v2'
                else:
                    responses[api + '/git/ref/tags/v1']['object']['sha'] = 'b' * 40
                calls = []
                def get(url):
                    calls.append(url)
                    return json.dumps(responses[url]).encode()
                error = ('repository_not_public' if scenario == 'private' else
                         'tag_mismatch' if scenario == 'tag-target' else 'state_invalid')
                with patch.object(release_check, 'ROOT', root), self.assertRaisesRegex(RuntimeError, error):
                    release_check.main(args, get=get)
                if scenario == 'private':
                    self.assertEqual(calls, [api])

    def test_tag_resolution_supports_annotations_and_rejects_cycles(self):
        api = 'https://api.github.com/repos/owner/project'
        responses = {api + '/git/ref/tags/v1': {'object': {'type': 'tag', 'sha': 'b' * 40}},
                     api + '/git/tags/' + 'b' * 40: {'object': {'type': 'commit', 'sha': 'a' * 40}}}
        get = lambda url: json.dumps(responses[url]).encode()
        self.assertEqual(release_check.tag_commit(api, 'v1', get), 'a' * 40)
        responses[api + '/git/tags/' + 'b' * 40]['object'] = {'type': 'tag', 'sha': 'b' * 40}
        with self.assertRaisesRegex(RuntimeError, 'tag_mismatch'):
            release_check.tag_commit(api, 'v1', get)

    def test_public_check_detects_metadata_changes_during_check(self):
        for scenario in ('release', 'tag', 'repository', 'history'):
            root = self.root / scenario
            root.mkdir()
            with self.subTest(scenario=scenario), patch.object(self, 'root', root):
                api, args, responses = self.metadata_fixture()
                counts = {}
                target = {'release': api + '/releases/tags/v1',
                          'tag': api + '/git/ref/tags/v1', 'repository': api,
                          'history': api + '/git/matching-refs/tags/?per_page=100&page=1'}[scenario]
                def get(url):
                    counts[url] = counts.get(url, 0) + 1
                    value = copy.deepcopy(responses[url])
                    if url == target and counts[url] == 2:
                        if scenario == 'release':
                            value['assets'][0]['digest'] = 'sha256:' + '0' * 64
                        elif scenario == 'tag':
                            value['object']['sha'] = 'b' * 40
                        elif scenario == 'repository':
                            value.update(private=True, visibility='private')
                        else:
                            value = [{'ref': 'refs/tags/v0', 'object': {'sha': 'b' * 40}}]
                    return json.dumps(value).encode()
                error = 'historical_release_changed' if scenario == 'history' else 'changed_during_check'
                with patch.object(release_check, 'ROOT', root), self.assertRaisesRegex(RuntimeError, error):
                    release_check.main(args, get=get)
                path, record = artifacts.records(root)[0]
                self.assertEqual(record['state'], 'failed')
                self.assertFalse((path / 'evidence/release.json').exists())

    def test_metadata_transport_requests_json_once_without_credentials(self):
        url = 'https://api.github.com/repos/owner/project'
        for status in (200, 503):
            with self.subTest(status=status), patch.object(release_check.urllib.request, 'build_opener') as factory:
                opener = factory.return_value
                response = opener.open.return_value.__enter__.return_value
                response.status = status
                response.read.return_value = b'{}'
                if status == 200:
                    self.assertEqual(release_check.github_metadata_get(url), b'{}')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'http_failed'):
                        release_check.github_metadata_get(url)
                opener.open.assert_called_once()
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')
                self.assertIsNone(request.get_header('Authorization'))
                self.assertIsNone(request.get_header('Cookie'))

    def test_metadata_transport_rejects_download_urls_and_redirects(self):
        for url in ('https://github.com/owner/project/releases/download/v1/file.zip',
                    'https://github.com/owner/project/archive/refs/tags/v1.zip',
                    'https://api.github.com/repos/owner/project/zipball/v1',
                    'https://api.github.com/repos/owner/project/tarball/v1',
                    'https://api.github.com/repos/owner/project/releases/assets/123',
                    'https://api.github.com.evil.invalid/repos/owner/project',
                    'http://api.github.com/repos/owner/project'):
            with self.subTest(url=url), patch.object(release_check.urllib.request, 'build_opener') as opener:
                with self.assertRaisesRegex(RuntimeError, 'metadata_url_invalid'):
                    release_check.github_metadata_get(url)
                opener.assert_not_called()
                with self.assertRaisesRegex(RuntimeError, 'metadata_url_invalid'):
                    release_check.MetadataRedirectHandler().redirect_request(None, None, 302, '', {}, url)

    def test_history_baseline_change_stops_before_target_release(self):
        repo, tag, commit, zip_path, checksum, release = self.release_fixture()
        baseline = self.write(self.root / 'history.json', b'{"releases": [], "tags": {"old": "different"}}')
        calls = []
        def get(url):
            calls.append(url)
            if url == f'https://api.github.com/repos/{repo}':
                return b'{"private": false, "visibility": "public"}'
            return b'[]'
        with patch.object(release_check, 'ROOT', self.root):
            with self.assertRaisesRegex(RuntimeError, 'historical_release_changed'):
                release_check.main(['--repo', repo, '--tag', tag, '--commit', commit, '--zip', zip_path.name,
                    '--checksum', checksum.name, '--history-baseline', baseline.name], get=get)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(url.startswith('https://api.github.com/') for url in calls))
        path, record = artifacts.records(self.root)[0]
        self.assertEqual(record['state'], 'failed')
        self.assertFalse((path / 'work').exists())


if __name__ == '__main__':
    unittest.main()
