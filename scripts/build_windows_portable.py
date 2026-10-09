# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""可审计的 Windows x64 one-folder 构建；无安装、模型调用或发布操作。"""
from __future__ import annotations

import hashlib
import argparse
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
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import __version__
from desktop.paths import checked_path
from desktop.config import load_config
from scripts.local_artifacts import ArtifactRun

NAME = f'arXivKaleid-{__version__}-windows-x64'
SOURCE_URL = f'https://github.com/yyyang20/arXivKaleid-Desktop/archive/refs/tags/v{__version__}.zip'
RESOURCES = (
    'config.json',
    'assets/app-icon.ico',
    'prompts/relevance_round1_v23.txt',
    'prompts/relevance_round2_v17.txt',
    'prompts/research_prompt_round1_v2.txt',
    'prompts/research_prompt_round2_v1.txt',
)
PUBLIC_DOCUMENTS = ('README.md', 'EULA.txt', 'PRIVACY.md', 'SECURITY.md')
REQUIRED_RELEASE_FILES = (
    'arXivKaleid.exe', '_internal/python313.dll',
    '_internal/vendor/curl/bin/curl.exe', '_internal/zoneinfo/Asia/Shanghai',
    *PUBLIC_DOCUMENTS, 'LICENSE', 'BUILD_INFO.json', 'THIRD_PARTY_NOTICES.txt',
    'licenses/QtBase/LICENSES/LGPL-3.0-only.txt',
    'licenses/QtBase/LICENSES/GPL-3.0-only.txt',
    'licenses/QtBase/LICENSES/MPL-2.0.txt', 'licenses/components.json',
    *('_internal/PySide6/Qt6' + module + '.dll' for module in
      ('Core', 'Gui', 'Widgets', 'Svg', 'SvgWidgets', 'Xml')),
    '_internal/PySide6/plugins/platforms/qwindows.dll',
    '_internal/PySide6/plugins/iconengines/qsvgicon.dll',
    '_internal/PySide6/plugins/imageformats/qico.dll',
    '_internal/PySide6/plugins/styles/qmodernwindowsstyle.dll',
    '_internal/pywin32_system32/pywintypes313.dll',
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


def copy_public_documents(folder: Path, frozen: dict | None = None) -> None:
    """将公开文档放在发行根目录；发布检查清单只留在开发仓库。"""
    source_root = checked_path(ROOT, 'docs/public_release')
    for name in PUBLIC_DOCUMENTS:
        source = checked_path(source_root, name)
        if not source.is_file():
            raise RuntimeError('public_document_missing:' + name)
        shutil.copyfile(source, checked_path(folder, name))
    readme = checked_path(folder, 'README.md')
    text = readme.read_text(encoding='utf-8')
    if '{{APPLICATION_SOURCE_NOTICE}}' in text:
        if frozen and frozen['purpose'] == 'local-portable-technical-validation':
            notice = (f"本包为本地 portable 技术候选，尚未公开发布。对应本地冻结提交 `{frozen['commit']}`；"
                      "源码保存在构建此包的本地仓库中，不存在本版本公开 tag 或 Release。"
                      "构建身份以 `BUILD_INFO.json` 为准。")
        else:
            notice = (f"本版[对应源码下载]({SOURCE_URL})固定到 `v{__version__}`，"
                      "包含应用源码、配置、Prompt、测试、构建脚本及说明，对应 `BUILD_INFO.json` 中的提交。")
        readme.write_text(text.replace('{{APPLICATION_SOURCE_NOTICE}}', notice), encoding='utf-8', newline='\n')
    # 应用许可证只有根目录这一份维护源，不从第三方目录或副本推断。
    license_source = checked_path(ROOT, 'LICENSE')
    if not license_source.is_file():
        raise RuntimeError('application_license_missing')
    shutil.copyfile(license_source, checked_path(folder, 'LICENSE'))


def source_archives(root: Path = ROOT) -> list[dict[str, str]]:
    """将对应源码绑定到固定运行依赖；许可和版本不符时停止发行。"""
    pins = pinned_requirements('requirements-desktop.txt', root=root)
    expected = {
        'QtBase': (pins['PySide6'], 'LGPL-3.0-only'),
        'PySide6 and Shiboken6': (pins['PySide6'], 'LGPL-3.0-only'),
        'pypdf': (pins['pypdf'], 'BSD-3-Clause'),
        'QtSvg': (pins['PySide6'], 'LGPL-3.0-only'),
        'PySide6-Fluent-Widgets': (pins['PySide6-Fluent-Widgets'], 'GPL-3.0-only'),
        'PySideSix-Frameless-Window': (pins['PySideSix-Frameless-Window'], 'LGPL-3.0-only'),
        'darkdetect': (pins['darkdetect'], 'BSD-3-Clause'),
        'pywin32': (pins['pywin32'], 'PSF-2.0'),
    }
    items = json.loads(checked_path(root, 'packaging/windows/source-manifest.json').read_text(encoding='utf-8'))
    if not isinstance(items, list) or len(items) != len(expected):
        raise RuntimeError('source_inventory_invalid')
    seen = set()
    for item in items:
        component = item.get('component') if isinstance(item, dict) else None
        if component not in expected or component in seen:
            raise RuntimeError('source_component_invalid')
        if (item.get('version'), item.get('license')) != expected[component]:
            raise RuntimeError('source_version_or_license_mismatch')
        if not isinstance(item.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise RuntimeError('source_checksum_invalid')
        checked_path(root, '.desktop-build/vendor', item['archive'])
        if not item['url'].startswith('https://') or not item['archive'].endswith(('.tar.gz', '.tar.xz')):
            raise RuntimeError('source_location_invalid')
        seen.add(component)
    return items


def frozen_identity() -> dict:
    """区分正式 main 构建与开发验证；两者均要求干净冻结身份。"""
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    if git('status', '--porcelain'):
        raise RuntimeError('clean_frozen_commit_required')
    baseline = git('rev-parse', 'origin/main')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', baseline, 'HEAD'], cwd=ROOT).returncode:
        raise RuntimeError('build_baseline_not_ancestor')
    commit = git('rev-parse', 'HEAD')
    branch = git('branch', '--show-current')
    # main 不能沿用开发分支的宽松后代条件，正式成品必须等于已核验基线。
    if branch == 'main' and commit != baseline:
        raise RuntimeError('release_main_not_equal_origin')
    return {'commit': commit, 'branch': branch,
            'baseline_commit': baseline, 'working_tree_clean': True,
            'purpose': 'public-release' if branch == 'main' else 'local-portable-technical-validation'}


def inspect_python_archive(executable: Path) -> dict:
    """检查冻结 Python 载荷；测试脚本不会因压缩藏过普通文件扫描。"""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    modules = sorted(pyz.toc)
    if any(n in ('tests', 'content_labels') or n.startswith(('tests.', 'unittest', 'pytest', 'scripts.visual_qa',
                                       'scipy', 'PIL', 'colorthief')) for n in modules):
        raise RuntimeError('development_or_full_module_in_release')
    for required in ('qfluentwidgets._rc.resource', 'qframelesswindow', 'darkdetect', 'portable_visual'):
        if required not in modules:
            raise RuntimeError('frozen_module_missing:' + required)
    return {'modules': modules, 'runtime_scripts': sorted(n for n in archive.toc if n.startswith('pyi_rth_'))}


def verify_legal_resources(folder: Path) -> None:
    """核验实际发行物的应用许可证、源码入口和第三方源码字节。"""
    if checked_path(folder, 'LICENSE').read_bytes() != checked_path(ROOT, 'LICENSE').read_bytes():
        raise RuntimeError('application_license_mismatch')
    readme = checked_path(folder, 'README.md').read_text(encoding='utf-8')
    info = checked_path(folder, 'BUILD_INFO.json')
    identity = json.loads(info.read_text(encoding='utf-8')) if info.is_file() else {}
    if identity.get('purpose') == 'local-portable-technical-validation':
        valid_source = (bool(re.fullmatch(r'[0-9a-f]{40}', str(identity.get('commit', ''))))
                        and f"对应本地冻结提交 `{identity['commit']}`" in readme
                        and '尚未公开发布' in readme and SOURCE_URL not in readme)
    else:
        valid_source = SOURCE_URL in readme
    if not valid_source:
        raise RuntimeError('application_source_link_mismatch')
    for item in source_archives():
        archive = checked_path(folder, 'licenses/sources', item['archive'])
        if not archive.is_file():
            raise RuntimeError('corresponding_source_missing:' + item['component'])
        verify_checksum(archive.read_bytes(), item['sha256'])


def verify_resource_identity(folder: Path, identity: dict) -> None:
    """冻结身份记录全部允许资源，拒绝遗漏或产物中的资源变化。"""
    expected = identity.get('resource_hashes')
    actual = {name: sha(checked_path(folder, '_internal', name).read_bytes()) for name in RESOURCES}
    if expected != actual:
        raise RuntimeError('build_resource_identity_mismatch')
    load_config(checked_path(folder, '_internal', 'config.json'))


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
        # 上游许可可能位于源码 tests/；保留这些原文，不将其误判为运行测试库。
        if relative.parts[0] != 'licenses' and any(part.lower() in {'tests', 'screenshots'} for part in relative.parts):
            raise RuntimeError('development_artifact_in_release')
        if not path.is_file():
            continue
        if (path.name.lower() in ('secret.dat', 'local_secret.json', 'automation_policy.json',
                                  'research_profile.json', 'research_profile.md',
                                  'round1_research_requirements.json', 'round2_research_requirements.json',
                                  'round1_research_prompt.json', 'round2_research_prompt.json',
                                  'daily_report_template.py', 'selection_nature.py')
                or path.name.lower() in {'visual_qa_desktop.py', 'qa.json'}
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
    verify_resource_identity(folder, json.loads((folder / 'BUILD_INFO.json').read_text(encoding='utf-8')))
    verify_legal_resources(folder)
    return inventory


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--license-input-manifest')
    args = parser.parse_args(argv)
    frozen = frozen_identity()
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('windows_x64_required')
    if Path(sys.prefix).name != 'arxivkaleid-desktop' or sys.version_info[:2] != (3, 13):
        raise RuntimeError('dedicated_conda_python313_required')
    for filename in ('requirements-desktop.txt', 'requirements-build.txt'):
        for name, version in pinned_requirements(filename).items():
            if importlib.metadata.version(name) != version:
                raise RuntimeError('build_dependency_version_mismatch:' + name)
    manifest = checked_path(ROOT, args.license_input_manifest) if args.license_input_manifest else None
    with ArtifactRun('build', root=ROOT) as run:
        run.record['commit'] = frozen['commit']
        run.protect(checked_path(ROOT, '.desktop-build/vendor'))
        if manifest:
            run.protect(manifest.parent)
            from portable_licenses import conda_metadata, license_input_files
            license_input_files(ROOT, manifest, conda_metadata(Path(sys.prefix)))
            run.protect(checked_path(ROOT, json.loads(manifest.read_text(encoding='utf-8'))['archive']))
        try:
            build_portable(frozen, run, manifest)
        finally:
            log = run.work / 'pyinstaller.log'
            if log.is_file():
                run.finish_evidence(lambda: run.capture(log, 'pyinstaller.log'))


def build_portable(frozen, run, license_manifest=None):
    """工作文件只放本次 work；输出与旧成品不属于自动删除范围。"""
    build = checked_path(ROOT, '.desktop-build')
    job = run.work
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
    run.checkpoint()
    with (job / 'pyinstaller.log').open('w', encoding='utf-8') as log:
        result = run.process([sys.executable, '-X', 'utf8', '-B', '-m', 'PyInstaller', '--noconfirm',
                        '--workpath', str(job / 'work'), '--distpath', str(job / 'dist'),
                        str(ROOT / 'packaging/windows/arxivkaleid.spec')], env=env, cwd=ROOT, stdout=log, stderr=log)
    if result.returncode:
        error = RuntimeError('pyinstaller_failed; inspect ' + str(job / 'pyinstaller.log'))
        if result.returncode == 1:
            run.mark_ordinary_failure(error, 'pyinstaller_failure')
        raise error
    print('PyInstaller completed:', job.name, flush=True)
    run.checkpoint()
    folder = job / 'dist/arXivKaleid'
    module_inventory = inspect_python_archive(folder / 'arXivKaleid.exe')
    (run.evidence / 'module-inventory.json').write_text(json.dumps(module_inventory, indent=2), encoding='utf-8')
    copy_public_documents(folder, frozen)
    from portable_licenses import collect_licenses
    collect_licenses(ROOT, folder, module_inventory, license_manifest=license_manifest)
    run.checkpoint()
    if frozen_identity() != frozen:
        raise RuntimeError('frozen_identity_changed_during_build')
    identity = {'version': __version__, **frozen,
                'python': platform.python_version(), 'architecture': 'x64',
                'dependencies': {n: importlib.metadata.version(n) for n in
                                 (*pinned_requirements('requirements-desktop.txt'), 'PyInstaller',
                                  'pyinstaller-hooks-contrib', 'shiboken6', 'PySide6_Essentials', 'PySide6_Addons')},
                'qt_modules': ['Core', 'Gui', 'Widgets', 'Svg', 'SvgWidgets', 'Xml'], 'tzdata': '2026c',
                'resource_hashes': {name: sha((folder / '_internal' / name).read_bytes()) for name in RESOURCES}}
    (folder / 'BUILD_INFO.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    inventory = verify_tree(folder)
    (run.evidence / 'inventory.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    shutil.copyfile(folder / 'BUILD_INFO.json', run.evidence / 'BUILD_INFO.json')
    groups = {}
    for name in inventory:
        group = ('source_archives' if name.startswith('licenses/sources/') else
                 'licenses' if name.startswith('licenses/') else
                 'qt' if name.startswith(('_internal/PySide6/', '_internal/shiboken6/')) else
                 'curl' if name.startswith('_internal/vendor/curl/') else
                 'pywin32' if 'pywin' in name.lower() or Path(name).name.startswith('win32') else
                 'python_runtime' if name.startswith('_internal/') else 'application_and_documents')
        groups[group] = groups.get(group, 0) + (folder / name).stat().st_size
    (run.evidence / 'size-report.json').write_text(json.dumps(groups, indent=2), encoding='utf-8')
    dist = checked_path(ROOT, 'dist', NAME)
    release = checked_path(ROOT, 'release')
    dist.parent.mkdir(exist_ok=True)
    release.mkdir(exist_ok=True)
    if dist.exists() and (dist / 'runtime').exists():
        raise RuntimeError('existing_distribution_contains_runtime_preserved')
    # 先在工作区生成并验证完整新 ZIP，避免失败时覆盖旧成品/校验文件。
    zip_path = job / (NAME + '.zip')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(inventory):
            archive.write(folder / name, NAME + '/' + name)
    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(inventory):
            raise RuntimeError('zip_integrity_failed')
        for item in archive.infolist():
            if sha(archive.read(item)) != inventory[item.filename[len(NAME) + 1:]]:
                raise RuntimeError('zip_inventory_mismatch')
    digest = sha(zip_path.read_bytes())
    checksum = release / (NAME + '.zip.sha256')
    run.checkpoint()
    if dist.exists():
        run.preserve_move(dist, 'previous-dist')
    old_zip = release / zip_path.name
    if old_zip.exists():
        run.preserve_move(old_zip, 'previous-release.zip')
    if checksum.exists():
        run.preserve_move(checksum, 'previous-release.sha256')
    shutil.copytree(folder, dist)
    zip_path.rename(old_zip)
    zip_path = old_zip
    checksum.write_text(f'{digest}  {zip_path.name}\n', encoding='ascii')
    run.record['outputs'] = {'zip': zip_path.relative_to(ROOT).as_posix(), 'sha256': digest,
                             'bytes': zip_path.stat().st_size, 'files': len(inventory)}
    run.save()
    print(json.dumps({'zip': str(zip_path), 'sha256': digest, 'bytes': zip_path.stat().st_size, 'files': len(inventory)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
