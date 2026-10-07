# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""从实际安装包和固定上游源码收集许可证；不得猜测或静默跳过。"""
import importlib.metadata
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile

from build_windows_portable import checked_path, download, source_archives

LICENSE_INPUT_SCHEMA = 'portable_conda_license_inputs_v1'


def conda_metadata(prefix):
    """只读指定环境的包身份，不修改安装元数据或原缓存。"""
    return {data['name']: data for path in sorted((prefix / 'conda-meta').glob('*.json'))
            for data in [json.loads(path.read_text(encoding='utf-8'))]}


def package_identity(metadata):
    return {key: metadata[key] for key in ('name', 'version', 'build', 'sha256')}


def prepare_license_inputs(root, archive_path, digest, prefix, output):
    """从明确指定且哈希正确的干净归档复制许可；旧来源材料永不删除。"""
    archive_path = checked_path(root, archive_path.relative_to(root).as_posix())
    output = checked_path(root, output.relative_to(root).as_posix())
    stable = checked_path(root, '.desktop-build/inputs/conda-licenses')
    if not output.is_relative_to(stable) or output == stable or output.exists():
        raise RuntimeError('license_input_output_invalid_or_exists')
    data = archive_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError('license_input_archive_hash_mismatch')
    metadata = conda_metadata(prefix)
    records, copies = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if not n.endswith('/')]
        if len(names) != len(set(names)) or any('/runtime/' in n for n in names):
            raise RuntimeError('license_input_archive_invalid')
        roots = {n.split('/')[0] for n in names}
        if len(roots) != 1:
            raise RuntimeError('license_input_archive_root_invalid')
        base = next(iter(roots)) + '/licenses/'
        components = json.loads(archive.read(base + 'components.json'))
        for component in components:
            if 'distributed_files' not in component:
                continue
            name = component['name']
            package = metadata[name]
            if component['version'] != package['version']:
                raise RuntimeError('license_input_version_mismatch')
            files = []
            for member in names:
                if not member.startswith(base + name + '/'):
                    continue
                relative = member[len(base + name + '/'):]
                target = checked_path(root, output.parent.relative_to(root).as_posix(), name, relative)
                content = archive.read(member)
                files.append({'relative': relative, 'path': target.relative_to(root).as_posix(),
                              'member': member, 'sha256': hashlib.sha256(content).hexdigest()})
                copies.append((target, content))
            if not files:
                raise RuntimeError('license_input_files_missing')
            records.append({**package_identity(package), 'files': files})
    if len(records) != len({r['name'] for r in records}) or not {'python', 'tzdata'} <= {r['name'] for r in records}:
        raise RuntimeError('license_input_inventory_invalid')
    # 拒绝覆盖已存在目录；完整验证通过后才开始写入新的稳定输入目录。
    if output.parent.exists():
        raise RuntimeError('license_input_directory_exists')
    output.parent.mkdir(parents=True)
    for target, content in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = dict(schema=LICENSE_INPUT_SCHEMA, archive=archive_path.relative_to(root).as_posix(),
                    archive_sha256=digest, packages=records)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def license_input_files(root, manifest_path, metadata):
    manifest_path = checked_path(root, manifest_path.relative_to(root).as_posix())
    stable = checked_path(root, '.desktop-build/inputs/conda-licenses')
    if not manifest_path.is_relative_to(stable):
        raise RuntimeError('license_input_manifest_outside_stable_directory')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != LICENSE_INPUT_SCHEMA:
        raise RuntimeError('license_input_schema_invalid')
    archive_path = checked_path(root, manifest['archive'])
    data = archive_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['archive_sha256']:
        raise RuntimeError('license_input_archive_hash_mismatch')
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if not n.endswith('/')]
        roots = {n.split('/')[0] for n in names}
        if len(roots) != 1 or len(names) != len(set(names)):
            raise RuntimeError('license_input_archive_invalid')
        base = next(iter(roots)) + '/licenses/'
        components = json.loads(archive.read(base + 'components.json'))
        expected_packages = {c['name']: c['version'] for c in components if 'distributed_files' in c}
        for record in manifest['packages']:
            name = record['name']
            if (name not in metadata or name not in expected_packages or name in result
                    or expected_packages[name] != record['version']
                    or package_identity(metadata[name]) != {k: record[k] for k in package_identity(metadata[name])}):
                raise RuntimeError('license_input_package_identity_mismatch')
            files = []
            seen = set()
            for item in record['files']:
                source = checked_path(root, item['path'])
                expected = checked_path(root, manifest_path.parent.relative_to(root).as_posix(), name, item['relative'])
                if (source != expected or item['relative'] in seen
                        or item['member'] != base + name + '/' + item['relative']):
                    raise RuntimeError('license_input_file_path_invalid')
                seen.add(item['relative'])
                content = source.read_bytes()
                if hashlib.sha256(content).hexdigest() != item['sha256'] or content != archive.read(item['member']):
                    raise RuntimeError('license_input_file_hash_mismatch')
                files.append((item['relative'], source))
            if not files or {base + name + '/' + relative for relative in seen} != {n for n in names if n.startswith(base + name + '/')}:
                raise RuntimeError('license_input_files_missing')
            result[name] = files
        if set(result) != set(expected_packages):
            raise RuntimeError('license_input_inventory_incomplete')
    return result


def wheel_license_files(dist):
    """保留 wheel 原许可相对目录，防止 pywin32 多份 License.txt 相互覆盖。"""
    items = []
    for item in dist.files or ():
        parts = Path(str(item)).parts
        if not parts or not parts[0].endswith('.dist-info'):
            continue
        if 'licenses' in parts:
            relative = Path(*parts[parts.index('licenses') + 1:])
        elif parts[-1].lower().startswith(('license', 'copying', 'notice')):
            relative = Path(parts[-1])
        else:
            continue
        items.append((item, relative))
    return items


def collect_source_archives(root: Path, licenses: Path) -> list[dict[str, str]]:
    """保留固定源码归档，并从 gzip/xz 原文收集对应版权与许可。"""
    records = []
    for item in source_archives(root):
        data = download(item['url'], item['sha256'], checked_path(root, '.desktop-build/vendor', item['archive']))
        target = checked_path(licenses, 'sources', item['archive'])
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
            for member in archive:
                if not member.isfile():
                    continue
                # 许可提取同样拒绝绝对路径和父目录逃逸，归档本身原样保留。
                parts = Path(member.name).parts
                if Path(member.name).is_absolute() or '..' in parts or len(parts) < 2:
                    raise RuntimeError('source_member_path_invalid')
                relative = Path(*Path(member.name).parts[1:])
                name = relative.name.lower()
                if ('LICENSES' in relative.parts or 'license' in name or name.startswith(('copying', 'copyright', 'notice'))
                        or name == 'qt_attribution.json'):
                    target = checked_path(licenses, item['component'].split()[0], relative.as_posix())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
        records.append({'name': item['component'], 'version': item['version'], 'license': item['license'],
                        'source': item['url'], 'sha256': item['sha256']})
    return records


def collect_licenses(root: Path, folder: Path, module_inventory: dict | None = None, *, license_manifest=None) -> None:
    licenses = folder / 'licenses'
    licenses.mkdir()
    records = []
    # Conda 元数据只提供必要依赖的许可证位置，绝不复制整个环境或元数据路径。
    binaries = [p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()]
    predicates = {
        'python': lambda n: Path(n).name == 'python313.dll',
        'tzdata': lambda n: n.endswith('zoneinfo/asia/shanghai'),
        'openssl': lambda n: Path(n).name.startswith(('libssl', 'libcrypto')),
        'sqlite': lambda n: Path(n).name == 'sqlite3.dll',
        'libffi': lambda n: Path(n).name.startswith(('ffi', 'libffi')) and n.endswith('.dll'),
        'libexpat': lambda n: 'expat' in Path(n).name and n.endswith('.dll'),
        'libmpdec': lambda n: 'mpdec' in Path(n).name and n.endswith('.dll'),
        'bzip2': lambda n: 'bz2' in Path(n).name and n.endswith('.dll'),
        'xz': lambda n: 'lzma' in Path(n).name and n.endswith('.dll'),
        'libzlib': lambda n: 'zlib' in Path(n).name and n.endswith('.dll'),
        'vc14_runtime': lambda n: Path(n).name.startswith(('msvcp', 'vcruntime', 'concrt')),
        'ucrt': lambda n: Path(n).name == 'ucrtbase.dll',
    }
    shipped = {name: [n for n in binaries if predicate(n.lower())] for name, predicate in predicates.items()}
    required = {name for name, files in shipped.items() if files}
    if not {'python', 'tzdata'} <= required:
        raise RuntimeError('execution_platform_missing')
    installed = conda_metadata(Path(sys.prefix))
    explicit = license_input_files(root, license_manifest, installed) if license_manifest else None
    for metadata in installed.values():
        name = metadata['name']
        if name not in required:
            continue
        if explicit is not None:
            if name not in explicit:
                raise RuntimeError('conda_license_missing:' + name)
            for relative, source in explicit[name]:
                target = checked_path(licenses, name, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        else:
            source = Path(metadata['extracted_package_dir']) / 'info/licenses'
            if not source.is_dir() or not any(source.rglob('*')):
                raise RuntimeError('conda_license_missing:' + name)
            shutil.copytree(source, licenses / name)
        records.append({'name': name, 'version': metadata['version'], 'license': metadata.get('license'),
                        'distributed_files': shipped[name]})
    if required != {r['name'] for r in records}:
        raise RuntimeError('conda_license_inventory_incomplete')
    wheel_modules = {'pypdf': ['pypdf'], 'PyInstaller': ['bootloader / runtime scripts'],
                     'PySide6-Fluent-Widgets': ['qfluentwidgets'],
                     'PySideSix-Frameless-Window': ['qframelesswindow'], 'darkdetect': ['darkdetect'],
                     'pywin32': ['win32api', 'win32con', 'win32gui', 'win32print', 'pywintypes']}
    runtime_scripts = (module_inventory or {}).get('runtime_scripts', [])
    if 'pyi_rth_pywintypes' in runtime_scripts:
        wheel_modules['pyinstaller-hooks-contrib'] = ['pyi_rth_pywintypes']
    for name, modules in wheel_modules.items():
        dist = importlib.metadata.distribution(name)
        items = wheel_license_files(dist)
        if not items:
            raise RuntimeError('wheel_license_missing:' + name)
        for item, relative in items:
            target = checked_path(licenses, name, relative.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist.locate_file(item), target)
        records.append({'name': name, 'version': dist.version,
                        'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
                        'distributed_modules': modules})
    records.extend(collect_source_archives(root, licenses))
    # QtBase 的 Mozilla/Unicode 等许可证文本已收集，MPL 全文来自其官方源码。
    for name in ('LGPL-3.0-only.txt', 'GPL-3.0-only.txt', 'MPL-2.0.txt'):
        if not (licenses / 'QtBase/LICENSES' / name).is_file():
            raise RuntimeError('required_license_missing:' + name)
    shutil.copyfile(root / 'packaging/windows/THIRD_PARTY_NOTICES.txt', folder / 'THIRD_PARTY_NOTICES.txt')
    (licenses / 'components.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='准备项目内固定许可输入；不构建、不联网、不修改环境')
    parser.add_argument('--source-archive', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(sys.prefix).name != 'arxivkaleid-desktop':
        raise RuntimeError('dedicated_conda_environment_required')
    prepare_license_inputs(root, checked_path(root, args.source_archive), args.source_sha256,
                           Path(sys.prefix), checked_path(root, args.output))
    print('许可输入已准备：' + args.output)
