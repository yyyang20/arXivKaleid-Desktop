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
import zipfile

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
CANDIDATE_POLICY = 'verified_candidate_copy_v1'
TECHNICAL_PURPOSE = 'local-portable-technical-validation'


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


def manifest_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode('utf-8')).hexdigest()


def candidate_files(root, directory, expected=None):
    """先检查路径和敏感文件存在性，再读取干净发行文件；不读取运行数据。"""
    directory = checked_path(root, directory.relative_to(root).as_posix())
    paths = list(directory.rglob('*'))
    for item in paths:
        checked_path(root, item.relative_to(root).as_posix())
        parts = [p.lower() for p in item.relative_to(directory).parts]
        if ('runtime' in parts or item.name.lower().startswith(('secret.', '.env', 'api-key', 'credentials'))
                or item.name.lower() in ('local_secret.json', 'round1_research_prompt.json', 'round2_research_prompt.json')
                or re.search(r'\.(sqlite\d*|db|pdf|jsonl)(?:-wal|-shm|-journal)?$', item.name, re.I)):
            raise RuntimeError('candidate_contains_user_data')
    names = {p.relative_to(directory).as_posix() for p in paths if p.is_file()}
    if expected is not None and names != set(expected):
        raise RuntimeError('candidate_manifest_changed')
    files = {p.relative_to(directory).as_posix(): file_hash(p) for p in paths if p.is_file()}
    allowed = {directory}
    for p in paths:
        if p.is_file():
            allowed.update(parent for parent in p.parents if parent.is_relative_to(directory))
    if any(p.is_dir() and p not in allowed for p in paths):
        raise RuntimeError('candidate_unknown_directory')
    return files


def verify_candidate_archive(root, directory, archive, checksum, expected):
    """只读校验整个 ZIP 与明确发行目录的文件集；不解压，不接受额外成员。"""
    for p in (directory, archive, checksum):
        checked_path(root, p.relative_to(root).as_posix())
    if not directory.is_dir() or not archive.is_file() or not checksum.is_file():
        raise RuntimeError('candidate_archive_missing')
    files = candidate_files(root, directory, expected)
    if files != expected:
        raise RuntimeError('candidate_manifest_changed')
    digest = file_hash(archive)
    declared = checksum.read_text(encoding='ascii').split()
    if len(declared) != 2 or declared[0] != digest:
        raise RuntimeError('candidate_checksum_mismatch')
    # 归档移动后文件名为 previous-release.zip；校验文件仍保存移动前原名。
    prefix = declared[1].removesuffix('.zip') + '/'
    if not re.fullmatch(r'arXivKaleid-[\w.-]+-windows-x64/', prefix):
        raise RuntimeError('candidate_archive_name_invalid')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        wanted = {prefix + name for name in files}
        if len(entries) != len(wanted) or {e.filename for e in entries} != wanted:
            raise RuntimeError('candidate_zip_manifest_changed')
        for entry in entries:
            if entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('candidate_zip_link')
            with source.open(entry) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != files[entry.filename[len(prefix):]]:
                    raise RuntimeError('candidate_zip_hash_changed')
    return digest


def register_candidate_copy(run):
    """仅新构建显式登记技术副本；历史记录和正式发行不推断清理资格。"""
    directory = run.preserved / 'previous-dist'
    info = checked_path(run.root, REGISTRY, run.record['id'], 'preserved', 'previous-dist', 'BUILD_INFO.json')
    if not info.is_file():
        return
    identity = json.loads(info.read_text(encoding='utf-8'))
    if identity.get('purpose') != TECHNICAL_PURPOSE:
        return
    if not re.fullmatch('[0-9a-f]{40}', identity.get('commit', '')) or identity.get('working_tree_clean') is not True:
        raise RuntimeError('candidate_identity_invalid')
    archive, checksum = run.preserved / 'previous-release.zip', run.preserved / 'previous-release.sha256'
    if not archive.is_file() or not checksum.is_file():
        run.record['candidate_retention_reason'] = 'matching_archive_missing'
        run.save()
        return
    files = candidate_files(run.root, directory)
    digest = verify_candidate_archive(run.root, directory, archive, checksum, files)
    run.record['candidate_copy'] = dict(policy=CANDIDATE_POLICY, status='pending',
        directory=directory.relative_to(run.root).as_posix(), files=files,
        bytes=sum(inventory(run.root, directory).values()), archive_sha256=digest,
        old_identity=identity)
    run.save()


def output_paths(root, output):
    """输出位置由发行布局确定，不信任运行记录提供的任意路径。"""
    relative = output.get('dist', '')
    if not re.fullmatch(r'dist/arXivKaleid-[\w.-]+-windows-x64', relative):
        raise RuntimeError('candidate_output_scope_invalid')
    name = relative.split('/')[1]
    if output.get('zip') != f'release/{name}.zip' or output.get('checksum') != f'release/{name}.zip.sha256':
        raise RuntimeError('candidate_output_scope_invalid')
    return tuple(checked_path(root, output[k]) for k in ('dist', 'zip', 'checksum'))


def bind_portable_build(root, source, files):
    """用 ZIP 和完整文件清单匹配本次构建，不能只凭 commit 或文件名匹配。"""
    source = checked_path(root, source.relative_to(root).as_posix())
    info = checked_path(root, source.relative_to(root).as_posix(), 'BUILD_INFO.json')
    identity = json.loads(info.read_text(encoding='utf-8'))
    matches = []
    for _, record in records(root):
        output = record.get('outputs', {})
        if (record['kind'] == 'build' and record['state'] in ('succeeded', 'released')
                and identity.get('build_run_id', record['id']) == record['id']
                and record.get('outcome') == 'succeeded' and output.get('dist') == source.relative_to(root).as_posix()
                and output.get('inventory') == files):
            directory, archive, checksum = output_paths(root, output)
            digest = verify_candidate_archive(root, directory, archive, checksum, files)
            if digest == output['sha256']:
                matches.append(dict(build_id=record['id'], commit=record['commit'],
                                    zip_sha256=digest, manifest_sha256=manifest_digest(files)))
    if len(matches) > 1:
        raise RuntimeError('portable_build_binding_ambiguous')
    return matches[0] if matches else None  # 旧构建仍可验证，但不能解锁新清理策略。


def validated_review(root, record):
    """验证资格来自完整入口及未变动证据，不依赖人工发布确认。"""
    binding = record.get('portable_build')
    report = checked_path(root, REGISTRY, record['id'], 'evidence', 'validation.json')
    if (record['kind'] != 'portable' or not binding or not record.get('portable_complete')
            or record.get('failure_type') or record.get('cleanup_error')
            or checked_path(root, record['work']).exists()
            or record.get('outcome') != 'awaiting_review'
            or not report.is_file() or file_hash(report) != record.get('portable_report_sha256')
            or not review_evidence_matches(root, record)):
        raise RuntimeError('portable_review_not_eligible')
    summary = json.loads(report.read_text(encoding='utf-8'))
    checks = ('missing_resource_rejected', 'tampered_requirements_rejected', 'missing_user_guide_rejected',
              'tampered_user_guide_rejected', 'used_runtime_rejected', 'clean_distribution_preserved')
    runs = summary.get('runs', [])
    modes = {(r.get('dpi_mode'), r.get('requested_dpr')) for r in runs}
    if (summary.get('portable_build') != binding or summary.get('complete') is not True
            or not all(summary.get(k) is True for k in checks)
            or not {('native', None), ('simulated', 1), ('simulated', 1.25),
                    ('simulated', 1.5), ('simulated', 2)}.issubset(modes)
            or not all(r.get('ok') is True and r.get('restart_cycles') == 3
                       and r.get('synthetic_dpapi_restart') is True and r.get('python_on_path') is False for r in runs)):
        raise RuntimeError('portable_review_binding_changed')
    _, build = read_record(root, binding['build_id'])
    output = build.get('outputs', {})
    expected = dict(build_id=build['id'], commit=build['commit'],
                    zip_sha256=output.get('sha256'), manifest_sha256=manifest_digest(output.get('inventory', {})))
    if (binding != expected or summary.get('commit') != build['commit']
            or build['state'] not in ('succeeded', 'released') or build.get('outcome') != 'succeeded'):
        raise RuntimeError('portable_review_binding_changed')
    return build


def review_evidence_matches(root, record):
    """先比较证据文件集，避免误读人工放入的额外文件或凭据。"""
    evidence = checked_path(root, REGISTRY, record['id'], 'evidence')
    expected = record.get('evidence_hashes')
    if not isinstance(expected, dict) or set(inventory(root, evidence)) != set(expected):
        return False
    for relative, digest in expected.items():
        item = checked_path(root, relative)
        if (not item.is_relative_to(evidence) or item.name.lower().startswith(('secret.', '.env', 'api-key', 'credentials'))
                or item.suffix.lower() not in ('.json', '.png', '.log', '.jsonl', '.txt')):
            return False
        if file_hash(item) != digest:
            return False
    return True


def candidate_in_use(root, directory, entries):
    """有效受管引用阻止清理；只查询程序路径，不读取进程命令行或凭据。"""
    for _, record in entries:
        if (record['state'] in OPEN_STATES or record.get('review_pending')
                or record['state'] == 'failed' and record.get('evidence_class') == 'protected'):
            for relative in record.get('protected_inputs', []):
                reference = checked_path(root, relative)
                if reference == directory or reference.is_relative_to(directory) or directory.is_relative_to(reference):
                    return True
    return candidate_process_active(directory)


def candidate_process_active(directory):
    if os.name == 'nt':
        # 只取 arXivKaleid 进程的可执行路径；状态不可确认时停止，不能强制结束用户程序。
        command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); @(Get-CimInstance Win32_Process -ErrorAction Stop -Filter \"Name = 'arXivKaleid.exe'\" | Select-Object ExecutablePath) | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
        paths = json.loads(result.stdout.decode('utf-8-sig') or '[]')
        paths = [paths] if isinstance(paths, dict) else paths
        for item in paths or []:
            value = item.get('ExecutablePath')
            if not value:
                raise RuntimeError('candidate_process_status_unknown')
            if Path(value).is_relative_to(directory):
                return True
    return False


def accept_portable_validation(root, record):
    """调用者持有锁；验证收尾后自动登记替代资格，人工验收只管发布。"""
    build = validated_review(root, record)
    output = build['outputs']
    directory, archive, checksum = output_paths(root, output)
    digest = verify_candidate_archive(root, directory, archive, checksum, output['inventory'])
    if digest != output['sha256']:
        raise RuntimeError('portable_review_current_zip_changed')
    if any(process_alive(pid) for pid in record['children']) or candidate_process_active(directory):
        raise RuntimeError('artifact_resources_still_active')
    build['candidate_validation'] = dict(run_id=record['id'], binding=record['portable_build'],
        report_sha256=record['portable_report_sha256'], completed_utc=record['completed_utc'])
    save_record(root, build)
    record['automatic_validation'] = build['candidate_validation']
    save_record(root, record)
    sweep_candidate_copies(root, finishing_id=record['id'])


def automatic_validation_accepted(root, record):
    """已完整验证的待发布材料保留证据，但不要求人工结案才能再验证。"""
    if record['kind'] != 'portable' or not record.get('portable_complete') or not record.get('portable_build'):
        return False
    build = validated_review(root, record)
    proof = record.get('automatic_validation', {})
    return (proof.get('run_id') == record['id'] and proof.get('binding') == record['portable_build']
            and proof.get('report_sha256') == record['portable_report_sha256']
            and proof.get('completed_utc') == record['completed_utc'])


def sweep_candidate_copies(root, *, finishing_id=None):
    """调用者持有锁；完整自动验证通过即清理合格旧副本，保留证明和归档。"""
    entries = records(root)
    for path, build in entries:
        copy = build.get('candidate_copy', {})
        if (build['schema'] != SCHEMA or build['kind'] != 'build'
                or copy.get('policy') != CANDIDATE_POLICY or copy.get('status') != 'pending'):
            continue
        approval = build.get('candidate_validation')
        if not approval or build['state'] not in ('succeeded', 'released'):
            continue
        try:
            _, review = read_record(root, approval['run_id'])
            allowed_states = ('awaiting_review', 'released')
            if review['state'] not in allowed_states and not (review['state'] == 'finishing' and review['id'] == finishing_id):
                continue
            verified = validated_review(root, review)
            if (verified['id'] != build['id'] or approval['binding'] != review['portable_build']
                    or approval['report_sha256'] != review['portable_report_sha256']
                    or approval['completed_utc'] != review['completed_utc']):
                raise RuntimeError('candidate_review_binding_changed')
            if any(process_alive(pid) for pid in review['children']):
                raise RuntimeError('artifact_resources_still_active')
            directory = checked_path(root, REGISTRY, build['id'], 'preserved', 'previous-dist')
            if copy.get('directory') != directory.relative_to(root).as_posix():
                raise RuntimeError('candidate_delete_scope_invalid')
            if candidate_in_use(root, directory, entries):
                copy['retention_reason'] = 'active_reference_or_process'
                save_record(root, build)
                continue
            archive, checksum = path / 'preserved/previous-release.zip', path / 'preserved/previous-release.sha256'
            digest = verify_candidate_archive(root, directory, archive, checksum, copy['files'])
            identity = json.loads((directory / 'BUILD_INFO.json').read_text(encoding='utf-8'))
            if digest != copy['archive_sha256'] or identity != copy['old_identity'] or identity.get('purpose') != TECHNICAL_PURPOSE:
                raise RuntimeError('candidate_identity_changed')
            if sum(inventory(root, directory).values()) != copy['bytes']:
                raise RuntimeError('candidate_size_changed')
            previous_state = build['state']
            build['state'] = 'rotating'
            copy['status'] = 'deleting'
            save_record(root, build)
            expected = {directory.relative_to(root).as_posix() + '/' + k: v for k, v in copy['files'].items()}
            remove_exact(root, directory, expected=expected)
            if file_hash(archive) != digest:
                raise RuntimeError('candidate_retained_archive_changed')
            copy.update(status='deleted', deleted_utc=datetime.now(timezone.utc).isoformat(),
                        deleted_files=len(expected), released_bytes=copy['bytes'], validation_run=review['id'])
            copy.pop('retention_reason', None)
            build['state'] = previous_state
            save_record(root, build)
            print('旧技术候选副本已清理：' + copy['directory'], file=sys.stderr)
        except Exception as error:
            build.update(state='cleanup_failed', cleanup_error=type(error).__name__,
                         evidence_class='protected', protection_reason='candidate_cleanup_failed')
            save_record(root, build)
            raise


def close_review(root, run_id):
    """仅记录原包发布的人工确认，保留证据，不作为副本清理门槛。"""
    with operation_lock(root):
        entries = records(root)
        if any(r['state'] in OPEN_STATES for _, r in entries):
            raise RuntimeError('unfinished_artifact_run')
        _, record = read_record(root, run_id)
        if record['state'] != 'awaiting_review' or record.get('review_pending') is not True:
            raise RuntimeError('portable_review_not_pending')
        build = validated_review(root, record)
        output = build['outputs']
        directory, archive, checksum = output_paths(root, output)
        digest = verify_candidate_archive(root, directory, archive, checksum, output['inventory'])
        if digest != output['sha256']:
            raise RuntimeError('portable_review_current_zip_changed')
        if output.get('checksum_sha256') and file_hash(checksum) != output['checksum_sha256']:
            raise RuntimeError('portable_review_current_checksum_changed')
        if any(process_alive(pid) for pid in record['children']) or candidate_process_active(checked_path(root, output['dist'])):
            raise RuntimeError('artifact_resources_still_active')
        now = datetime.now(timezone.utc).isoformat()
        record.update(state='released', review_pending=False, review_closed_utc=now,
                      release_review_approved=True, evidence_class='protected',
                      protection_reason='reviewed_portable_validation',
                      review_checksum_sha256=file_hash(checksum))
        save_record(root, record)


def approved_portable(root, run_id, commit, archive, checksum):
    """发布资格只来自本包完整验证及人工通过，不推定旧记录已获批准。"""
    _, review = read_record(root, run_id)
    if (review['state'] != 'released' or review.get('review_pending') is not False
            or review.get('release_review_approved') is not True or not review.get('review_closed_utc')
            or not review.get('review_checksum_sha256')):
        raise RuntimeError('release_review_not_approved')
    build = validated_review(root, review)
    directory, expected_archive, expected_checksum = output_paths(root, build['outputs'])
    if (build['commit'] != commit or archive != expected_archive or checksum != expected_checksum
            or file_hash(checksum) != review['review_checksum_sha256']):
        raise RuntimeError('release_review_artifact_mismatch')
    digest = verify_candidate_archive(root, directory, archive, checksum, build['outputs']['inventory'])
    if digest != review['portable_build']['zip_sha256']:
        raise RuntimeError('release_review_artifact_mismatch')
    if any(process_alive(pid) for pid in review['children']) or candidate_process_active(directory):
        raise RuntimeError('artifact_resources_still_active')
    return dict(review_run_id=run_id, **review['portable_build'], checksum_sha256=file_hash(checksum))


def ordinary_completion(root, path, record):
    """仅已正常收尾且完整性可核验的治理记录取得轮换资格；v1 失败不推断。"""
    legacy = record['schema'] == LEGACY_SCHEMA
    if record['state'] not in ('succeeded', 'failed'):
        return None
    if legacy and record['state'] != 'succeeded':
        return None
    if not legacy and record['evidence_class'] != 'ordinary':
        return None
    # 当前输出的构建记录是复用和人工验收的身份来源，不能被空复用审计轮换掉。
    output = record.get('outputs', {})
    if record['kind'] == 'build' and output.get('zip'):
        _, archive, _ = output_paths(root, output)
        if archive.exists() and file_hash(archive) == output.get('sha256'):
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
            sweep_candidate_copies(self.root)
            existing = records(self.root)
            same = [r for _, r in existing if r['kind'] == self.kind]
            blocked = False
            for p, r in existing:
                if r['kind'] != self.kind:
                    continue
                pending = r['state'] == 'awaiting_review' or r.get('review_pending') and r['state'] != 'released'
                if pending and r['state'] == 'awaiting_review' and automatic_validation_accepted(self.root, r):
                    continue
                if pending or r['state'] == 'failed' and ordinary_completion(self.root, p, r) is None:
                    blocked = True
            if blocked:
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
        if source.is_dir():
            candidate_files(self.root, source)  # 移动前先排除运行数据，禁止对凭据做哈希。
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
            if exc_type is None and self.kind == 'portable' and self.record.get('portable_build') and self.record.get('portable_complete'):
                accept_portable_validation(self.root, self.record)
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
    if record.get('portable_complete') and record.get('portable_build'):
        targets = [checked_path(root, record['work'])]  # 自动清理审计依赖此证明，不受人工结案影响。
    values = {p.relative_to(root).as_posix(): inventory(root, p, hashes=True) for p in targets if p.exists()}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return dict(id=run_id, state=record['state'], targets=values, plan_sha256=digest,
                candidate_copy=record.get('candidate_copy'),
                protected=record['protected_inputs'] + [f'{REGISTRY}/{run_id}/preserved']
                    + ([f'{REGISTRY}/{run_id}/evidence'] if record.get('portable_complete') and record.get('portable_build') else []))


def apply_plan(root, run_id, digest, *, review_complete=False, stopped=False):
    """仅显式指定运行与当前计划摘要的执行入口；文档规定当次删除授权。"""
    with operation_lock(root):
        path, record = read_record(root, run_id)
        if record.get('portable_build') and record.get('review_pending') and review_complete:
            raise RuntimeError('portable_use_close_review_to_preserve_evidence')
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
    parser.add_argument('action', nargs='?', default='plan', choices=('plan', 'apply', 'close-review'))
    parser.add_argument('--run-id')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--review-complete', action='store_true')
    parser.add_argument('--processes-stopped', action='store_true')
    args = parser.parse_args()
    if args.action == 'plan':
        ids = [args.run_id] if args.run_id else [r['id'] for _, r in records(ROOT)]
        print(json.dumps([cleanup_plan(ROOT, i, review_complete=args.review_complete) for i in ids], ensure_ascii=False, indent=2))
    elif args.action == 'close-review':
        if not args.run_id or args.plan_sha256 or args.review_complete or args.processes_stopped:
            parser.error('close-review 仅需 --run-id；须先由用户明确确认该候选人工验收合格')
        close_review(ROOT, args.run_id)
    else:
        if not args.run_id or not args.plan_sha256:
            parser.error('apply 必须明确运行 ID 和当前计划 SHA-256；执行前须获得对应删除授权')
        apply_plan(ROOT, args.run_id, args.plan_sha256, review_complete=args.review_complete,
                   stopped=args.processes_stopped)


if __name__ == '__main__':
    main()
