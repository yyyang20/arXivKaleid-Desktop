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


def collect_licenses(root: Path, folder: Path) -> None:
    licenses = folder / 'licenses'
    licenses.mkdir()
    records = []
    # Conda 元数据只提供必要依赖的许可证位置，绝不复制整个环境或元数据路径。
    required = {'python', 'tzdata', 'openssl', 'sqlite', 'libffi', 'libexpat',
                'libmpdec', 'bzip2', 'xz', 'libzlib', 'vc14_runtime', 'ucrt'}
    for path in (Path(sys.prefix) / 'conda-meta').glob('*.json'):
        metadata = json.loads(path.read_text(encoding='utf-8'))
        name = metadata['name']
        if name not in required:
            continue
        source = Path(metadata['extracted_package_dir']) / 'info/licenses'
        if not source.is_dir() or not any(source.rglob('*')):
            raise RuntimeError('conda_license_missing:' + name)
        shutil.copytree(source, licenses / name)
        records.append({'name': name, 'version': metadata['version'], 'license': metadata.get('license')})
    if required != {r['name'] for r in records}:
        raise RuntimeError('conda_license_inventory_incomplete')
    for name in ('pypdf', 'PyInstaller'):
        dist = importlib.metadata.distribution(name)
        items = [f for f in dist.files if '/licenses/' in str(f)]
        if not items:
            raise RuntimeError('wheel_license_missing:' + name)
        for item in items:
            target = licenses / name / Path(str(item)).name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(dist.locate_file(item), target)
        records.append({'name': name, 'version': dist.version, 'license': dist.metadata.get('License-Expression') or dist.metadata.get('License')})
    records.extend(collect_source_archives(root, licenses))
    # QtBase 的 Mozilla/Unicode 等许可证文本已收集，MPL 全文来自其官方源码。
    for name in ('LGPL-3.0-only.txt', 'GPL-3.0-only.txt', 'MPL-2.0.txt'):
        if not (licenses / 'QtBase/LICENSES' / name).is_file():
            raise RuntimeError('required_license_missing:' + name)
    shutil.copyfile(root / 'packaging/windows/THIRD_PARTY_NOTICES.txt', folder / 'THIRD_PARTY_NOTICES.txt')
    (licenses / 'components.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
