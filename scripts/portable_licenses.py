# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""从实际安装包和固定上游源码收集许可证；不得猜测或静默跳过。"""
import importlib.metadata
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile

from build_windows_portable import checked_path, download, source_archives


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


def collect_licenses(root: Path, folder: Path, module_inventory: dict | None = None) -> None:
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
    for path in (Path(sys.prefix) / 'conda-meta').glob('*.json'):
        metadata = json.loads(path.read_text(encoding='utf-8'))
        name = metadata['name']
        if name not in required:
            continue
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
