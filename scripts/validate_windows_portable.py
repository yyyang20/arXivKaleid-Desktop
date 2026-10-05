# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""在项目内全新副本验证冻结程序，移除开发环境 PATH，不读取用户数据。"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from build_windows_portable import ROOT, NAME, checked_path, verify_tree, frozen_identity, inspect_python_archive


def verify_capture_display(visual, *, native_screen=None, target_dpr=None):
    """核验全部截图的屏幕与倍率，拒绝跨屏或部分截图倍率漂移。"""
    screen = visual['screen']
    captures = visual['captures']
    assert captures and all(c['screen'] == screen for c in captures), 'capture_screen_changed'
    if native_screen is not None:
        assert screen == native_screen, 'validation_screen_changed'
    actual_dpr = captures[0]['dpr']
    expected = actual_dpr if target_dpr is None else target_dpr
    assert all(abs(c['dpr'] - expected) < 0.03 for c in captures), 'simulated_dpr_mismatch'
    return screen, actual_dpr


def close_own_window(process):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    found = []
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            if title.value.startswith('arXivKaleid Desktop '):
                found.append(True)
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return bool(found)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arxiv', action='store_true')
    args = parser.parse_args()
    source = checked_path(ROOT, 'dist', NAME)
    verify_tree(source)
    identity = json.loads((source / 'BUILD_INFO.json').read_text(encoding='utf-8'))
    assert identity['commit'] == frozen_identity()['commit'] and identity['working_tree_clean']
    inspect_python_archive(source / 'arXivKaleid.exe')
    work = checked_path(ROOT, '.desktop-build', 'validation-' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    results = []
    native_dpr = None
    modes = [('native', None)] if args.arxiv else [('native', None), ('simulated-100', 1),
            ('simulated-125', 1.25), ('simulated-150', 1.5), ('simulated-200', 2)]
    for mode, target_dpr in modes:
        copy = checked_path(work, '隔离副本 ' + mode)
        shutil.copytree(source, copy)
        token = uuid.uuid4().hex
        env = {key: value for key, value in os.environ.items() if key.upper() in
               ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE')}
        env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'),
                   TEMP=str(copy / 'runtime/work'), TMP=str(copy / 'runtime/work'),
                   PYTHONNOUSERSITE='1', ARXIVKALEID_DIAGNOSTIC_TOKEN=token)
        if target_dpr is not None:
            # 只作用于本次子进程；用 native 实测值换算，不改变系统 DPI。
            env.update(QT_SCALE_FACTOR=str(target_dpr / native_dpr),
                       QT_SCALE_FACTOR_ROUNDING_POLICY='PassThrough')
        assert shutil.which('python', path=env['PATH']) is None
        command = [str(copy / 'arXivKaleid.exe'), '--portable-check', '--visual-qa']
        if args.arxiv:
            command.append('--arxiv')
        with (work / (mode + '-process.log')).open('wb') as log:
            result = subprocess.run(command, cwd=work, env=env, timeout=1600,
                                    stdout=log, stderr=log, creationflags=0x08000000)
        report_path = copy / 'runtime/work/portable-check.json'
        report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {'ok': False}
        if result.returncode or not report.get('ok') or report.get('failure_type'):
            raise RuntimeError('portable_validation_failed:' + mode + ':' + str(report.get('failure_type')))
        # 各模式及全部截图必须来自同一主屏，不能将跨屏变化误当作模拟倍率。
        actual_screen, actual_dpr = verify_capture_display(
            report['visual_qa'], native_screen=None if native_dpr is None else native_screen,
            target_dpr=target_dpr)
        if native_dpr is None:
            native_dpr = actual_dpr
            native_screen = actual_screen
        # 新进程先明确断言合成 DPAPI 恢复，随后验证普通入口重复启动/关闭。
        recovery = subprocess.run([str(copy / 'arXivKaleid.exe'), '--portable-recovery-check'],
                                  cwd=work, env=env, timeout=30, creationflags=0x08000000)
        assert recovery.returncode == 0
        restored = json.loads((copy / 'runtime/work/recovery-check.json').read_text())
        assert restored['synthetic_dpapi_recovered']
        assert restored['history_restored_exactly'] and restored['deleted_history_stays_deleted']
        assert restored['research_requirements_restored_exactly']
        for _ in range(3):
            process = subprocess.Popen([str(copy / 'arXivKaleid.exe')], cwd=work, env=env, creationflags=0x08000000)
            visible = False
            try:
                for _ in range(150):
                    if process.poll() is not None:
                        break
                    if close_own_window(process):
                        visible = True
                        break
                    time.sleep(0.1)
                if not visible or process.wait(timeout=15) != 0:
                    raise RuntimeError('portable_restart_failed')
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
        report.update(restart_cycles=3, synthetic_dpapi_restart=True, python_on_path=False,
                      dpi_mode='native' if target_dpr is None else 'simulated', requested_dpr=target_dpr,
                      actual_dpr=actual_dpr, validation_directory=copy.name, outside_source_cwd=True)
        results.append(report)
        (work / 'validation.json').write_text(json.dumps({'commit': identity['commit'], 'runs': results},
                                                       ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'mode': mode, 'dpr': actual_dpr, 'ok': True, 'screenshots': len(report['visual_qa']['captures'])}), flush=True)
    # 缺失资源必须失败，且诊断拒绝重复使用已有 runtime；仅操作本次新副本。
    negative = checked_path(work, 'missing-resource')
    shutil.copytree(source, negative)
    prompt = negative / '_internal/prompts/relevance_round2_v16.txt'
    prompt.rename(prompt.with_suffix('.disabled'))
    bad = subprocess.run([str(negative / 'arXivKaleid.exe'), '--portable-check'], cwd=work,
                         env=env, timeout=30, creationflags=0x08000000)
    assert bad.returncode != 0
    # 默认研究资源同样受发行完整性校验，篡改后不可启动分析。
    tampered = checked_path(work, 'tampered-requirements')
    shutil.copytree(source, tampered)
    requirement = checked_path(tampered, '_internal/prompts/research_requirements_round1_v1.txt')
    requirement.write_bytes(requirement.read_bytes() + b'\nsynthetic-tamper')
    bad = subprocess.run([str(tampered / 'arXivKaleid.exe'), '--portable-check'], cwd=work,
                         env=env, timeout=30, creationflags=0x08000000)
    assert bad.returncode != 0
    rejected = subprocess.run([str(copy / 'arXivKaleid.exe'), '--portable-check'], cwd=work,
                              env=env, timeout=30, creationflags=0x08000000)
    assert rejected.returncode == 2
    # 应用曾用过的验证目录不回流干净发行树。
    verify_tree(source)
    summary = {'commit': identity['commit'], 'runs': results, 'missing_resource_rejected': True,
               'tampered_requirements_rejected': True,
               'used_runtime_rejected': True, 'clean_distribution_preserved': True}
    (work / 'validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Validation audit:', work.name)


if __name__ == '__main__':
    main()
