"""可审计的 Windows x64 one-folder 构建；无安装、模型调用或发布操作。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import __version__
from desktop.paths import checked_path
from desktop.config import load_config

NAME = f'arXivKaleid-{__version__}-windows-x64'
RESOURCES = (
    'config.json',
    'prompts/relevance_round1_v20.txt',
    'prompts/relevance_round2_v15.txt',
)
PUBLIC_DOCUMENTS = ('README.md', 'EULA.txt', 'PRIVACY.md', 'SECURITY.md')
REQUIRED_RELEASE_FILES = (
    'arXivKaleid.exe', '_internal/python313.dll',
    '_internal/vendor/curl/bin/curl.exe', '_internal/zoneinfo/Asia/Shanghai',
    *PUBLIC_DOCUMENTS, 'THIRD_PARTY_NOTICES.txt',
    'licenses/QtBase/LICENSES/LGPL-3.0-only.txt',
    'licenses/QtBase/LICENSES/GPL-3.0-only.txt',
    'licenses/QtBase/LICENSES/MPL-2.0.txt', 'licenses/components.json',
    *('_internal/' + p for p in RESOURCES),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pinned_requirements(filename: str, *, root: Path = ROOT,
                        visiting: frozenset[Path] = frozenset()) -> dict[str, str]:
    """展开项目内 requirements 引用；拒绝越界、循环和未固定版本。"""
    path = checked_path(root, filename)
    if path in visiting:
        raise RuntimeError('requirements_include_cycle')
    pins = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('-r '):
            included = checked_path(root, str(path.parent.relative_to(root)), line[3:].strip())
            additions = pinned_requirements(str(included.relative_to(root)), root=root,
                                            visiting=visiting | {path})
        else:
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)', line)
            if not match:
                raise RuntimeError('requirements_pin_invalid')
            additions = {match[1]: match[2]}
        for name, version in additions.items():
            if name in pins and pins[name] != version:
                raise RuntimeError('requirements_pin_conflict')
            pins[name] = version
    return pins


def verify_checksum(data: bytes, expected: str) -> None:
    if sha(data) != expected:
        raise RuntimeError('download_checksum_mismatch')


def verify_x64(data: bytes) -> None:
    if data[:2] != b'MZ' or len(data) < 64:
        raise RuntimeError('invalid_pe')
    offset = struct.unpack_from('<I', data, 60)[0]
    if data[offset:offset + 4] != b'PE\0\0' or data[offset + 4:offset + 6] != b'\x64\x86':
        raise RuntimeError('pe_not_x64')


def download(url: str, digest: str, target: Path) -> bytes:
    if target.exists():
        data = target.read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    verify_checksum(data, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return data


def prepare_curl(stage: Path, vendor: Path) -> None:
    manifest = json.loads((ROOT / 'packaging/windows/curl-manifest.json').read_text(encoding='utf-8'))
    if manifest['architecture'] != 'x64' or not manifest['url'].startswith('https://curl.se/windows/'):
        raise RuntimeError('curl_manifest_invalid')
    data = download(manifest['url'], manifest['sha256'], checked_path(vendor, manifest['archive']))
    dest = stage / 'vendor/curl'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        prefix = manifest['archive'][:-4] + '/'
        for item in archive.infolist():
            if item.is_dir():
                continue
            if not item.filename.startswith(prefix):
                raise RuntimeError('curl_archive_root_invalid')
            relative = item.filename[len(prefix):]
            if (relative in ('bin/curl.exe', 'bin/libcurl-x64.dll', 'bin/curl-ca-bundle.crt',
                             'COPYING.txt', 'README.txt', 'BUILD-MANIFEST.txt', 'BUILD-HASHES.txt')
                    or relative.startswith('dep/') and any(t in Path(relative).name.lower() for t in ('license', 'copying', 'authors'))):
                target = checked_path(stage, 'vendor/curl', relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    exe = dest / 'bin/curl.exe'
    verify_x64(exe.read_bytes())
    result = subprocess.run([str(exe), '--disable', '--version'], check=True, capture_output=True, timeout=15)
    version = result.stdout.decode('utf-8')
    if f"curl {manifest['version'].split('_')[0]} " not in version or 'https' not in version.split('Protocols:')[-1].splitlines()[0]:
        raise RuntimeError('curl_version_or_https_invalid')
    # 固定运行文件哈希，用于 packaged resolver；许可证无需在每次请求前哈希。
    files = {p.relative_to(dest).as_posix(): sha(p.read_bytes()) for p in (dest / 'bin').iterdir()}
    (dest / 'files.json').write_text(json.dumps(files, indent=2), encoding='utf-8')
    print(version.strip())


def prepare_resources(stage: Path) -> None:
    for name in RESOURCES:
        source = checked_path(ROOT, name)
        target = checked_path(stage, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Desktop 配置同时校验资源身份，不再需要在线策略文件。
    load_config(stage / 'config.json')
    prefix = Path(sys.prefix)
    tz = json.loads(next((prefix / 'conda-meta').glob('tzdata-*.json')).read_text(encoding='utf-8'))
    if tz['version'] != '2026c':
        raise RuntimeError('tzdata_version_mismatch')
    # 仅收集当前业务所需 IANA 数据；保留历史转换，不硬编码 UTC+8。
    target = stage / 'zoneinfo/Asia/Shanghai'
    target.parent.mkdir(parents=True)
    shutil.copyfile(prefix / 'share/zoneinfo/Asia/Shanghai', target)


def copy_public_documents(folder: Path) -> None:
    """将公开文档放在发行根目录；发布检查清单只留在开发仓库。"""
    source_root = checked_path(ROOT, 'docs/public_release')
    for name in PUBLIC_DOCUMENTS:
        source = checked_path(source_root, name)
        if not source.is_file():
            raise RuntimeError('public_document_missing:' + name)
        shutil.copyfile(source, checked_path(folder, name))


def verify_tree(folder: Path) -> dict[str, str]:
    """不接触真实 Secret；仅扫描发行 allowlist 产生的文件和字节。"""
    forbidden = {'runtime', '.git', '.desktop-runtime', '.codex-validation', 'logs', 'reports', 'cache', '__pycache__'}
    inventory = {}
    private_paths = [str(ROOT), str(Path.home())]
    patterns = [value.replace('\\', sep).encode(enc) for value in private_paths
                for sep in ('\\', '/') for enc in ('utf-8', 'utf-16-le')]
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        checked_path(folder, str(relative))
        if any(part.lower() in forbidden for part in relative.parts):
            raise RuntimeError('forbidden_release_path')
        if not path.is_file():
            continue
        if (path.name.lower() in ('secret.dat', 'local_secret.json', 'automation_policy.json',
                                  'research_profile.json', 'research_profile.md',
                                  'daily_report_template.py', 'selection_nature.py')
                or '.sqlite' in path.name.lower()
                or path.suffix.lower() in {'.pdf', '.jsonl'}):
            raise RuntimeError('forbidden_release_file')
        data = path.read_bytes()
        if any(p in data for p in patterns):
            raise RuntimeError('private_build_path_in_release:' + relative.as_posix())
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for item in archive.infolist():
                    if any(p in archive.read(item) for p in patterns):
                        raise RuntimeError('private_path_in_nested_archive')
        inventory[relative.as_posix()] = sha(data)
    for required in REQUIRED_RELEASE_FILES:
        if required not in inventory:
            raise RuntimeError('release_file_missing:' + required)
    verify_x64((folder / 'arXivKaleid.exe').read_bytes())
    return inventory


def main() -> None:
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('windows_x64_required')
    if Path(sys.prefix).name != 'arxivkaleid-desktop' or sys.version_info[:2] != (3, 13):
        raise RuntimeError('dedicated_conda_python313_required')
    for filename in ('requirements-desktop.txt', 'requirements-build.txt'):
        for name, version in pinned_requirements(filename).items():
            if importlib.metadata.version(name) != version:
                raise RuntimeError('build_dependency_version_mismatch:' + name)
    build = checked_path(ROOT, '.desktop-build')
    build.mkdir(exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix='build-', dir=build))
    stage = job / 'stage'
    stage.mkdir()
    for name in ('temp', 'pyinstaller-cache'):
        (job / name).mkdir()
    env = os.environ.copy()
    env.update(TEMP=str(job / 'temp'), TMP=str(job / 'temp'),
               PYINSTALLER_CONFIG_DIR=str(job / 'pyinstaller-cache'),
               ARXIVKALEID_BUILD_STAGE=str(stage), PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONUTF8'] = '1'
    prepare_resources(stage)
    prepare_curl(stage, checked_path(build, 'vendor'))
    with (job / 'pyinstaller.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-m', 'PyInstaller', '--noconfirm',
                        '--workpath', str(job / 'work'), '--distpath', str(job / 'dist'),
                        str(ROOT / 'packaging/windows/arxivkaleid.spec')], env=env, cwd=ROOT, stdout=log, stderr=log)
    if result.returncode:
        raise RuntimeError('pyinstaller_failed; inspect ' + str(job / 'pyinstaller.log'))
    print('PyInstaller completed:', job.name, flush=True)
    folder = job / 'dist/arXivKaleid'
    copy_public_documents(folder)
    from portable_licenses import collect_licenses
    collect_licenses(ROOT, folder)
    identity = {'version': __version__, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'working_tree_clean': not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip(),
                'python': platform.python_version(), 'architecture': 'x64',
                'dependencies': {n: importlib.metadata.version(n) for n in ('PySide6', 'pypdf', 'PyInstaller')}, 'tzdata': '2026c'}
    (folder / 'BUILD_INFO.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    inventory = verify_tree(folder)
    (job / 'inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    dist = checked_path(ROOT, 'dist', NAME)
    release = checked_path(ROOT, 'release')
    dist.parent.mkdir(exist_ok=True)
    release.mkdir(exist_ok=True)
    if dist.exists():
        if (dist / 'runtime').exists():
            raise RuntimeError('existing_distribution_contains_runtime_preserved')
        # 旧发行物移入本项目本次构建目录留存，不递归删除历史数据。
        dist.rename(job / 'previous-dist')
    shutil.copytree(folder, dist)
    zip_path = release / (NAME + '.zip')
    if zip_path.exists():
        zip_path.rename(job / 'previous-release.zip')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(inventory):
            archive.write(dist / name, NAME + '/' + name)
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(inventory):
            raise RuntimeError('zip_integrity_failed')
        for item in archive.infolist():
            if sha(archive.read(item)) != inventory[item.filename[len(NAME) + 1:]]:
                raise RuntimeError('zip_inventory_mismatch')
    digest = sha(zip_path.read_bytes())
    checksum = release / (NAME + '.zip.sha256')
    if checksum.exists():
        checksum.rename(job / 'previous-release.sha256')
    checksum.write_text(f'{digest}  {zip_path.name}\n', encoding='ascii')
    print(json.dumps({'zip': str(zip_path), 'sha256': digest, 'bytes': zip_path.stat().st_size, 'files': len(inventory)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
