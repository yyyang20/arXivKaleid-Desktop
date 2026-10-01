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

from build_windows_portable import ROOT, NAME, checked_path, verify_tree


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
    work = checked_path(ROOT, '.desktop-build', 'validation-' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    copy = work / NAME
    shutil.copytree(source, copy)
    env = {key: value for key, value in os.environ.items() if key.upper() in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE')}
    env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'),
               TEMP=str(copy / 'runtime/work'), TMP=str(copy / 'runtime/work'), PYTHONNOUSERSITE='1')
    command = [str(copy / 'arXivKaleid.exe'), '--portable-check'] + (['--arxiv'] if args.arxiv else [])
    result = subprocess.run(command, cwd=work, env=env, timeout=1600, creationflags=0x08000000)
    report_path = copy / 'runtime/work/portable-check.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {'ok': False, 'diagnostic_missing': True}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if result.returncode or not report.get('ok'):
        raise RuntimeError('portable_validation_failed')
    # 独立新进程正常启动，再仅关闭该进程拥有的 GUI，验证 DPAPI 恢复及重启。
    process = subprocess.Popen([str(copy / 'arXivKaleid.exe')], cwd=work, env=env, creationflags=0x08000000)
    visible = False
    try:
        for _ in range(100):
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
    report.update(restart=True, python_on_path=shutil.which('python', path=env['PATH']) is not None,
                  validation_directory=str(copy), outside_source_cwd=True)
    (work / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
