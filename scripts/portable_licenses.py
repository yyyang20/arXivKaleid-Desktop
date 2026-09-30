"""从实际安装包和固定上游源码收集许可证；不得猜测或静默跳过。"""
import importlib.metadata
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile

from build_windows_portable import checked_path, download


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
    sources = json.loads((root / 'packaging/windows/source-manifest.json').read_text(encoding='utf-8'))
    for item in sources:
        data = download(item['url'], item['sha256'], checked_path(root, '.desktop-build/vendor', item['archive']))
        target = licenses / 'sources' / item['archive']
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        # 保留版权、许可及第三方归属信息；源码包原样提供，无需在用户电脑解包。
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:xz') as archive:
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
        records.append({'name': item['component'], 'version': item['version'], 'license': 'LGPL-3.0-only; third-party notices included', 'source': item['url'], 'sha256': item['sha256']})
    # QtBase 的 Mozilla/Unicode 等许可证文本已收集，MPL 全文来自其官方源码。
    for name in ('LGPL-3.0-only.txt', 'GPL-3.0-only.txt', 'MPL-2.0.txt'):
        if not (licenses / 'QtBase/LICENSES' / name).is_file():
            raise RuntimeError('required_license_missing:' + name)
    shutil.copyfile(root / 'packaging/windows/THIRD_PARTY_NOTICES.txt', folder / 'THIRD_PARTY_NOTICES.txt')
    (licenses / 'components.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
