# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""项目内产物生命周期；不扫描用户数据、不自动淘汰历史材料。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.paths import checked_path

SCHEMA = 'local_artifacts_v2'
LEGACY_SCHEMA = 'local_artifacts_v1'
WORK_LIMIT = 4 * 1024 ** 3
EVIDENCE_LIMIT = 512 * 1024 ** 2
KINDS = ('build', 'portable', 'visual', 'tests', 'release-check')
OPEN_STATES = ('running', 'finishing', 'cleanup_failed', 'rotating')
STATES = (*OPEN_STATES, 'failed', 'awaiting_review', 'succeeded', 'released')
REGISTRY = '.desktop-build/managed-artifacts'
TEST_ROOT = '.codex-validation/managed-artifacts'
ORDINARY_FAILURES = ('unittest_failure', 'pyinstaller_failure')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root, path, *, hashes=False):
    """删除前逐项 lstat/边界检查；不跟随任何 reparse point。"""
    path = checked_path(root, path.relative_to(root).as_posix())
    if not path.exists():
        return {}
    items = [path] if path.is_file() else list(path.rglob('*'))
    result = {}
    for item in items:
        item = checked_path(root, item.relative_to(root).as_posix())
        if item.is_file():
            result[item.relative_to(root).as_posix()] = file_hash(item) if hashes else item.stat().st_size
    return result


@contextmanager
def operation_lock(root):
    """操作系统进程锁自动随退出释放；遗留运行记录仍由入口拒绝复用。"""
    directory = checked_path(root, REGISTRY)
    directory.mkdir(parents=True, exist_ok=True)
    path = checked_path(root, REGISTRY, 'operation.lock')
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('artifact_operation_busy') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_record(root, run_id):
    if not re.fullmatch(r'(build|portable|visual|tests|release-check)-[0-9a-f]{32}', run_id):
        raise RuntimeError('artifact_run_id_invalid')
    path = checked_path(root, REGISTRY, run_id)
    record = json.loads(checked_path(root, REGISTRY, run_id, 'run.json').read_text(encoding='utf-8'))
    work = f'{TEST_ROOT}/{run_id}' if record.get('kind') == 'tests' else f'{REGISTRY}/{run_id}/work'
    if (record.get('schema') not in (SCHEMA, LEGACY_SCHEMA) or record.get('id') != run_id
            or run_id.rsplit('-', 1)[0] != record.get('kind')
            or record.get('work') != work or record.get('state') not in STATES
            or not isinstance(record.get('pid'), int)
            or not isinstance(record.get('children'), list)
            or not all(isinstance(pid, int) and pid > 0 for pid in record['children'])):
        raise RuntimeError('artifact_record_invalid')
    if record['schema'] == SCHEMA and (
            record.get('evidence_class') not in ('ordinary', 'protected')
            or not isinstance(record.get('protection_reason'), str)
            or 'completed_utc' not in record):
        raise RuntimeError('artifact_record_invalid')
    for item in path.iterdir():
        checked_path(root, item.relative_to(root).as_posix())
        if item.name not in ('run.json', 'run.json.tmp', 'work', 'evidence', 'preserved'):
            raise RuntimeError('unknown_artifact_entry')
    # 清理范围从固定布局导出，不信任记录中任意添加的路径。
    return path, record


def records(root):
    directory = checked_path(root, REGISTRY)
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.iterdir()):
        checked_path(root, path.relative_to(root).as_posix())
        if path.name == 'operation.lock':
            continue
        if not path.is_dir():
            raise RuntimeError('unknown_artifact_entry')
        result.append(read_record(root, path.name))
    return result


def check_capacity(root, *, work_limit=WORK_LIMIT, evidence_limit=EVIDENCE_LIMIT):
    workspace = sum(inventory(root, checked_path(root, REGISTRY)).values())
    workspace += sum(inventory(root, checked_path(root, TEST_ROOT)).values())
    evidence = sum(sum(inventory(root, path / 'evidence').values()) for path, _ in records(root))
    if workspace > work_limit or evidence > evidence_limit:
        raise RuntimeError('artifact_capacity_exceeded; run local_artifacts.py plan')
    if shutil.disk_usage(root).free < min(work_limit, 256 * 1024 ** 2):
        raise RuntimeError('artifact_disk_space_insufficient')


def remove_exact(root, path, *, expected=None):
    # 先完整核验再逐文件删除，禁止通配、rmtree 和链接穿越。
    relative = path.relative_to(root).as_posix()
    if not any(relative.startswith(prefix + '/') for prefix in (REGISTRY, TEST_ROOT)):
        raise RuntimeError('artifact_delete_scope_invalid')
    actual = inventory(root, path, hashes=expected is not None)
    if expected is not None and actual != expected:
        raise RuntimeError('artifact_evidence_manifest_changed')
    if not path.exists():
        return
    items = list(path.rglob('*')) if path.is_dir() else []
    if expected is not None:
        files = {p.relative_to(root).as_posix() for p in items if p.is_file()}
        if files != set(expected):
            raise RuntimeError('artifact_evidence_manifest_changed')
    for item in sorted(items, key=lambda p: len(p.parts), reverse=True):
        item = checked_path(root, item.relative_to(root).as_posix())
        if expected is not None and item.is_file() and file_hash(item) != expected[item.relative_to(root).as_posix()]:
            raise RuntimeError('artifact_evidence_manifest_changed')
        item.rmdir() if item.is_dir() else item.unlink()
    path = checked_path(root, path.relative_to(root).as_posix())
    path.rmdir() if path.is_dir() else path.unlink()


def save_record(root, record):
    target = checked_path(root, REGISTRY, record['id'], 'run.json')
    temporary = checked_path(root, REGISTRY, record['id'], 'run.json.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)


def ordinary_completion(root, path, record):
    """仅已正常收尾且完整性可核验的治理记录取得轮换资格；v1 失败不推断。"""
    legacy = record['schema'] == LEGACY_SCHEMA
    if record['state'] not in ('succeeded', 'failed'):
        return None
    if legacy and record['state'] != 'succeeded':
        return None
    if not legacy and record['evidence_class'] != 'ordinary':
        return None
    if (record.get('review_pending') is not False or record.get('cleanup_error') is not None
            or record.get('outcome') != record['state']
            or checked_path(root, record['work']).exists()):
        return None
    if not legacy and (record['protection_reason'] != '' or
            record['state'] == 'failed' and record.get('ordinary_failure_reason') not in ORDINARY_FAILURES):
        raise RuntimeError('artifact_evidence_class_invalid')
    stamp = record.get('created_utc') if legacy else record.get('completed_utc')
    try:
        completed = datetime.fromisoformat(stamp)
        if completed.tzinfo is None or completed > datetime.now(timezone.utc):
            raise ValueError('timezone_required')
    except (TypeError, ValueError) as error:
        raise RuntimeError('artifact_completion_invalid') from error
    evidence = checked_path(root, REGISTRY, record['id'], 'evidence')
    expected = record.get('evidence_hashes')
    if (not evidence.is_dir() or not isinstance(expected, dict)
            or inventory(root, evidence, hashes=True) != expected):
        raise RuntimeError('artifact_evidence_manifest_changed')
    # 文件清单之外的空目录也不能成为隐式删除目标。
    allowed_dirs = {evidence}
    for relative in expected:
        item = checked_path(root, relative)
        if not item.is_relative_to(evidence):
            raise RuntimeError('artifact_evidence_manifest_changed')
        allowed_dirs.update(p for p in item.parents if p.is_relative_to(evidence))
    if any(item.is_dir() and item not in allowed_dirs for item in evidence.rglob('*')):
        raise RuntimeError('unknown_evidence_directory')
    if (path / 'run.json.tmp').exists():
        raise RuntimeError('unfinished_artifact_record')
    return completed


def rotate_evidence(root, kind, *, finishing_id=None):
    """调用者持有进程锁；先记轮换状态，失败或中断都留下停止入口的记录。"""
    groups = {'succeeded': [], 'failed': []}
    for path, record in records(root):
        if record['kind'] == kind:
            # 当前运行直到轮换和容量核验完成都保持未收尾；只在本锁内参与保留计数。
            candidate = (dict(record, state=record['outcome'])
                         if record['id'] == finishing_id and record['state'] == 'finishing' else record)
            completed = ordinary_completion(root, path, candidate)
            if completed is not None:
                groups[candidate['state']].append((completed, record['id']))
    for state, keep in (('succeeded', 2), ('failed', 1)):
        for _, run_id in sorted(groups[state], reverse=True)[keep:]:
            if run_id == finishing_id:
                raise RuntimeError('artifact_completion_order_invalid')
            path, record = read_record(root, run_id)
            if ordinary_completion(root, path, record) is None:
                raise RuntimeError('artifact_rotation_eligibility_changed')
            record['state'] = 'rotating'
            record['rotation_outcome'] = state
            save_record(root, record)
            try:
                # 写入日志后再核验一次；不信任首次选择时的文件快照。
                snapshot = dict(record, state=state)
                ordinary_completion(root, path, snapshot)
                remove_exact(root, checked_path(root, REGISTRY, run_id, 'evidence'),
                             expected=record['evidence_hashes'])
                preserved = checked_path(root, REGISTRY, run_id, 'preserved')
                if record.get('preserved') or (preserved.exists() and any(preserved.iterdir())):
                    record['state'] = 'released'
                    record['evidence_hashes'] = {}
                    save_record(root, record)
                else:
                    # 没有保护材料才移除空布局和日志，避免普通记录无限增长。
                    if preserved.exists():
                        preserved.rmdir()
                    checked_path(root, REGISTRY, run_id, 'run.json').unlink()
                    path.rmdir()
            except Exception as error:
                record['state'] = 'cleanup_failed'
                record['cleanup_error'] = type(error).__name__
                save_record(root, record)
                raise


class ArtifactRun:
    def __init__(self, kind, *, root=ROOT, review=False, work_limit=WORK_LIMIT, evidence_limit=EVIDENCE_LIMIT):
        if kind not in KINDS:
            raise ValueError('artifact_kind_invalid')
        self.root = root.resolve()
        self.kind, self.review = kind, review
        self.work_limit, self.evidence_limit = work_limit, evidence_limit
        self.children = []
        self.cleanup_allowed = True
        self.ordinary_failure = None

    def __enter__(self):
        self.lock = operation_lock(self.root)
        self.lock.__enter__()
        try:
            existing = records(self.root)
            if any(r['state'] in OPEN_STATES for _, r in existing):
                print(json.dumps([cleanup_plan(self.root, r['id']) for _, r in existing
                                  if r['state'] in OPEN_STATES], ensure_ascii=False, indent=2), file=sys.stderr)
                raise RuntimeError('unfinished_artifact_run; run local_artifacts.py plan')
            same = [r for _, r in existing if r['kind'] == self.kind]
            if any(r['state'] == 'awaiting_review' or r.get('review_pending') and r['state'] != 'released'
                   or r['state'] == 'failed' and ordinary_completion(self.root, p, r) is None
                   for p, r in existing if r['kind'] == self.kind):
                print(json.dumps([cleanup_plan(self.root, r['id']) for r in same],
                                 ensure_ascii=False, indent=2), file=sys.stderr)
                raise RuntimeError('artifact_retention_limit; review local_artifacts.py plan')
            check_capacity(self.root, work_limit=self.work_limit, evidence_limit=self.evidence_limit)
            run_id = self.kind + '-' + uuid.uuid4().hex
            self.path = checked_path(self.root, REGISTRY, run_id)
            self.path.mkdir()
            self.evidence = self.path / 'evidence'
            self.evidence.mkdir()
            self.preserved = self.path / 'preserved'
            self.preserved.mkdir()
            relative = f'{TEST_ROOT}/{run_id}' if self.kind == 'tests' else f'{REGISTRY}/{run_id}/work'
            self.work = checked_path(self.root, relative)
            self.record = dict(schema=SCHEMA, id=run_id, kind=self.kind, pid=os.getpid(),
                               created_utc=datetime.now(timezone.utc).isoformat(),
                               state='running', work=relative, protected_inputs=[], preserved=[],
                               children=[], cleanup_error=None, failure_type=None, review_pending=self.review)
            self.record.update(evidence_class='protected', protection_reason='running', completed_utc=None)
            self.record['commit'] = (subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root,
                stderr=subprocess.DEVNULL).decode().strip() if (self.root / '.git').exists() else None)
            self.save()
            self.work.mkdir(parents=True)
            return self
        except BaseException:
            self.lock.__exit__(*sys.exc_info())
            raise

    def save(self):
        save_record(self.root, self.record)

    def mark_ordinary_failure(self, error, reason):
        """入口仅为明确识别的异常对象标记；后来的不同异常仍按未结案故障保护。"""
        if not isinstance(error, Exception) or reason not in ORDINARY_FAILURES:
            raise ValueError('ordinary_failure_marker_invalid')
        self.ordinary_failure = (error, reason)

    def checkpoint(self):
        check_capacity(self.root, work_limit=self.work_limit, evidence_limit=self.evidence_limit)

    def protect(self, path):
        path = checked_path(self.root, path.relative_to(self.root).as_posix())
        if path == self.work or path.is_relative_to(self.work) or path.is_relative_to(self.path):
            raise RuntimeError('protected_input_inside_disposable_work')
        self.record['protected_inputs'].append(path.relative_to(self.root).as_posix())
        self.record.setdefault('protected_hashes', {}).update(inventory(self.root, path, hashes=True))
        self.record.setdefault('protected_sizes', {}).update(inventory(self.root, path))
        self.record['protected_input_bytes'] = sum(self.record['protected_sizes'].values())
        self.save()

    def preserve_move(self, source, name):
        """移动旧成品仍按旧材料保护；核对前后逐文件哈希。"""
        source = checked_path(self.root, source.relative_to(self.root).as_posix())
        parts = source.relative_to(self.root).parts
        if (len(parts) < 2 or parts[0] not in ('dist', 'release')
                or source.is_dir() and (source / 'runtime').exists()):
            raise RuntimeError('old_distribution_source_invalid')
        target = checked_path(self.root, REGISTRY, self.record['id'], 'preserved', name)
        if target.exists():
            raise RuntimeError('preserved_destination_exists')
        before = inventory(self.root, source, hashes=True)
        self.record['preserved'].append(dict(source=source.relative_to(self.root).as_posix(),
                                             destination=target.relative_to(self.root).as_posix(), hashes=before,
                                             bytes=sum(inventory(self.root, source).values())))
        self.save()
        source.rename(target)
        after = inventory(self.root, target, hashes=True)
        old = {Path(k).relative_to(source.relative_to(self.root)).as_posix(): v for k, v in before.items()}
        new = {Path(k).relative_to(target.relative_to(self.root)).as_posix(): v for k, v in after.items()}
        if old != new:
            raise RuntimeError('preserved_hash_mismatch')

    def capture(self, source, destination):
        source = checked_path(self.root, source.relative_to(self.root).as_posix())
        target = checked_path(self.root, REGISTRY, self.record['id'], 'evidence', destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise RuntimeError('evidence_destination_exists')
        shutil.copyfile(source, target)
        if file_hash(source) != file_hash(target):
            raise RuntimeError('evidence_copy_mismatch')

    def process(self, command, *, timeout=None, **kwargs):
        """只管理自己启动的子进程；超时/取消后等待退出再清理。"""
        process = subprocess.Popen(command, **kwargs)
        self.children.append(process)
        self.record['children'].append(process.pid)
        try:
            self.save()
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except BaseException:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            except Exception as error:
                self.cleanup_allowed = False
                print('子进程收尾失败：' + type(error).__name__, file=sys.stderr)
            raise

    def __exit__(self, exc_type, exc, traceback):
        cleanup_error = None
        try:
            self.record['failure_type'] = exc_type.__name__ if exc_type else None
            # 原始错误文本可能带凭据或私人路径，只记类型，不记录 repr(exc)。
            outcome = 'failed' if exc_type else ('awaiting_review' if self.review else 'succeeded')
            self.record['outcome'] = outcome
            ordinary = not self.review and (exc is None or self.ordinary_failure is not None
                                           and self.ordinary_failure[0] is exc)
            self.record['evidence_class'] = 'ordinary' if ordinary else 'protected'
            self.record['protection_reason'] = '' if ordinary else ('manual_review' if self.review else 'unresolved_fault')
            if ordinary and exc is not None:
                self.record['ordinary_failure_reason'] = self.ordinary_failure[1]
            if not self.cleanup_allowed or any(p.poll() is None for p in self.children):
                raise RuntimeError('artifact_resources_still_active')
            self.record['evidence_hashes'] = inventory(self.root, self.evidence, hashes=True)
            for relative, digest in self.record.get('protected_hashes', {}).items():
                if file_hash(checked_path(self.root, relative)) != digest:
                    raise RuntimeError('protected_input_hash_changed')
            self.save()
            remove_exact(self.root, self.work)
            self.record['completed_utc'] = datetime.now(timezone.utc).isoformat()
            self.record['state'] = 'finishing'
            self.save()
            rotate_evidence(self.root, self.kind, finishing_id=self.record['id'])
            self.checkpoint()
            self.record['state'] = outcome
        except Exception as error:
            cleanup_error = error
            self.record['state'] = 'cleanup_failed'
            self.record['cleanup_error'] = type(error).__name__
            self.record.update(evidence_class='protected', protection_reason='cleanup_failed')
        finally:
            try:
                try:
                    self.save()
                except Exception as error:
                    cleanup_error = error
                    self.record.update(state='cleanup_failed', cleanup_error=type(error).__name__,
                                       evidence_class='protected', protection_reason='cleanup_failed')
                    try:
                        save_record(self.root, self.record)
                    except Exception:
                        pass  # 磁盘无法写入时保留已有记录和独立报错，不掩盖原始异常。
                    print('产物记录保存失败：' + type(error).__name__, file=sys.stderr)
                print('产物记录：' + self.path.relative_to(self.root).as_posix(), file=sys.stderr)
            finally:
                self.lock.__exit__(exc_type, exc, traceback)
        if cleanup_error:
            print('产物收尾失败：' + type(cleanup_error).__name__, file=sys.stderr)
            if exc_type is None:
                raise RuntimeError('artifact_cleanup_failed') from cleanup_error
        return False

    def finish_evidence(self, collect):
        """证据收集失败时保留 work；不掩盖正在传播的原始失败。"""
        original_error = sys.exc_info()[0]
        try:
            collect()
        except Exception as error:
            self.cleanup_allowed = False
            print('证据收集失败：' + type(error).__name__, file=sys.stderr)
            if original_error is None:
                raise


@contextmanager
def temporary_environment(run):
    """源码 QA 的当前进程临时设置，退出时恢复，不写持久环境。"""
    import tempfile
    temporary = run.work / 'temp'
    temporary.mkdir()
    previous = {key: os.environ.get(key) for key in ('TEMP', 'TMP')}
    previous_tempdir = tempfile.tempdir
    try:
        os.environ.update(TEMP=str(temporary), TMP=str(temporary))
        tempfile.tempdir = str(temporary)
        yield
    finally:
        tempfile.tempdir = previous_tempdir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise RuntimeError('artifact_process_status_unknown')
        try:
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def cleanup_plan(root, run_id, *, review_complete=False):
    path, record = read_record(root, run_id)
    if record['state'] == 'running':
        # 强制中断时只能列出工作区，人工确认所有所属进程已经退出后才能收口。
        targets = [checked_path(root, record['work'])]
    else:
        targets = [checked_path(root, record['work']), path / 'evidence']
    if (record.get('review_pending') or record['state'] == 'awaiting_review') and not review_complete:
        targets = [checked_path(root, record['work'])]
    values = {p.relative_to(root).as_posix(): inventory(root, p, hashes=True) for p in targets if p.exists()}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return dict(id=run_id, state=record['state'], targets=values, plan_sha256=digest,
                protected=record['protected_inputs'] + [f'{REGISTRY}/{run_id}/preserved'])


def apply_plan(root, run_id, digest, *, review_complete=False, stopped=False):
    """仅显式指定运行与当前计划摘要的执行入口；文档规定当次删除授权。"""
    with operation_lock(root):
        path, record = read_record(root, run_id)
        if record['state'] in OPEN_STATES and not stopped:
            raise RuntimeError('explicit_stopped_confirmation_required')
        if record['state'] in OPEN_STATES and any(process_alive(pid) for pid in [record['pid'], *record['children']]):
            raise RuntimeError('artifact_resources_still_active')
        plan = cleanup_plan(root, run_id, review_complete=review_complete)
        if plan['plan_sha256'] != digest:
            raise RuntimeError('artifact_cleanup_plan_changed')
        original_state = record['state']
        # 先落盘未收尾状态，人工删除中途被中断也会阻止后续生成。
        record['state'] = 'cleanup_failed'
        (path / 'run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            for relative in plan['targets']:
                remove_exact(root, checked_path(root, relative))
        except Exception as error:
            record['cleanup_error'] = type(error).__name__
            try:
                (path / 'run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass  # 已落盘 cleanup_failed；保留并传播原始删除错误。
            raise
        # 未完成人工审查时只清理残余 work，不释放证据留存槽。
        if not (record.get('review_pending') or original_state == 'awaiting_review') or review_complete:
            record['state'] = 'released'
            record['review_pending'] = False
        else:
            record['state'] = record.get('outcome', 'awaiting_review')
        record['cleanup_error'] = None
        (path / 'run.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        if (record['state'] == 'released' and not (path / 'evidence').exists()
                and not any((path / 'preserved').iterdir())):
            remove_exact(root, path)


def test_scratch(root):
    """测试根由受管入口提供；直接 unittest 仍仅使用原隔离目录。"""
    relative = os.environ.get('ARXIVKALEID_TEST_SCRATCH', '.codex-validation')
    if relative != '.codex-validation' and not relative.startswith(TEST_ROOT + '/tests-'):
        raise RuntimeError('test_scratch_invalid')
    return checked_path(root, relative)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='plan', choices=('plan', 'apply'))
    parser.add_argument('--run-id')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--review-complete', action='store_true')
    parser.add_argument('--processes-stopped', action='store_true')
    args = parser.parse_args()
    if args.action == 'plan':
        ids = [args.run_id] if args.run_id else [r['id'] for _, r in records(ROOT)]
        print(json.dumps([cleanup_plan(ROOT, i, review_complete=args.review_complete) for i in ids], ensure_ascii=False, indent=2))
    else:
        if not args.run_id or not args.plan_sha256:
            parser.error('apply 必须明确运行 ID 和当前计划 SHA-256；执行前须获得对应删除授权')
        apply_plan(ROOT, args.run_id, args.plan_sha256, review_complete=args.review_complete,
                   stopped=args.processes_stopped)


if __name__ == '__main__':
    main()
