# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""匿名只读核验公开发行及历史基线；不发布、不上传、不自动重试。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.local_artifacts import ArtifactRun, checked_path, file_hash


def public_get(url):
    # 不加载 Cookie、CLI 登录或 Authorization；每个请求只有一次 attempt。
    request = urllib.request.Request(url, headers={'User-Agent': 'arXivKaleid-release-check', 'Accept': '*/*'})
    with urllib.request.build_opener(urllib.request.ProxyHandler()).open(request, timeout=180) as response:
        if response.status != 200:
            raise RuntimeError('public_release_http_failed')
        return response.read()


def release_identity(release):
    return {k: release[k] for k in ('id', 'tag_name', 'name', 'body', 'draft', 'prerelease', 'immutable')} | {
        'assets': sorted(({k: a[k] for k in ('id', 'name', 'size', 'digest')} for a in release['assets']), key=lambda a: a['id'])}


def paginated(repo, resource, get):
    items = []
    for page in range(1, 1001):
        value = json.loads(get(f'https://api.github.com/repos/{repo}/{resource}?per_page=100&page={page}'))
        if not isinstance(value, list):
            raise RuntimeError('public_release_inventory_invalid')
        items.extend(value)
        if len(value) < 100:
            return items
    raise RuntimeError('public_release_inventory_too_large')


def history_snapshot(repo, exclude_tag, get=public_get):
    releases = paginated(repo, 'releases', get)
    refs = paginated(repo, 'git/matching-refs/tags/', get)
    return {'releases': sorted([release_identity(r) for r in releases if r['tag_name'] != exclude_tag], key=lambda r: r['id']),
            'tags': {r['ref']: r['object']['sha'] for r in refs if r['ref'] != 'refs/tags/' + exclude_tag}}


def verify_assets(release, repo, tag, commit, zip_path, checksum, *, get=public_get):
    """资产名称、digest、实际字节、校验内容和 BUILD_INFO 必须同时匹配。"""
    names = {zip_path.name, checksum.name}
    assets = release['assets']
    if len(assets) != 2 or {a['name'] for a in assets} != names:
        raise RuntimeError('public_release_assets_invalid')
    for asset in assets:
        local = zip_path if asset['name'] == zip_path.name else checksum
        url = f'https://github.com/{repo}/releases/download/{tag}/{local.name}'
        if asset['browser_download_url'] != url or asset.get('state') != 'uploaded':
            raise RuntimeError('public_release_asset_url_invalid')
        data = get(url)
        if (len(data) != asset['size'] or 'sha256:' + hashlib.sha256(data).hexdigest() != asset['digest']
                or data != local.read_bytes()):
            raise RuntimeError('public_release_asset_bytes_mismatch')
    digest = file_hash(zip_path)
    if checksum.read_text(encoding='ascii').strip() != f'{digest}  {zip_path.name}':
        raise RuntimeError('public_release_checksum_mismatch')
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        prefix = zip_path.name[:-4] + '/'
        if len(names) != len(set(names)) or any('/runtime/' in n for n in names):
            raise RuntimeError('public_release_archive_invalid')
        info = json.loads(archive.read(prefix + 'BUILD_INFO.json'))
        if (info.get('commit') != commit or info.get('purpose') != 'public-release'
                or info.get('branch') != 'main' or not info.get('working_tree_clean')
                or tag != 'v' + info.get('version', '')):
            raise RuntimeError('public_release_build_identity_mismatch')
    return {'zip_sha256': digest, 'zip_bytes': zip_path.stat().st_size, 'assets_equal_local': True}


def verify_source(data, root, commit):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root)
    names = git('ls-tree', '-r', '--name-only', commit).decode('utf-8').splitlines()
    if any(Path(n).name.lower() in ('secret.dat', 'local_secret.json', '.env') for n in names):
        raise RuntimeError('sensitive_file_in_release_source')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [n for n in archive.namelist() if not n.endswith('/')]
        if not files or len(files) != len(set(files)):
            raise RuntimeError('public_source_inventory_invalid')
        prefix = files[0].split('/')[0] + '/'
        if set(files) != {prefix + n for n in names}:
            raise RuntimeError('public_source_inventory_mismatch')
        for name in names:
            if archive.read(prefix + name) != git('show', commit + ':' + name):
                raise RuntimeError('public_source_bytes_mismatch')
    return {'source_files': len(names), 'source_sha256': hashlib.sha256(data).hexdigest()}


def main(argv=None, *, get=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--zip', required=True)
    parser.add_argument('--checksum', required=True)
    parser.add_argument('--history-baseline', required=True)
    args = parser.parse_args(argv)
    get = get or public_get
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo) or not re.fullmatch(r'v[0-9A-Za-z.+-]+', args.tag) or not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('仓库、tag 或冻结提交格式无效')
    zip_path = checked_path(ROOT, args.zip)
    checksum = checked_path(ROOT, args.checksum)
    baseline_path = checked_path(ROOT, args.history_baseline)
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    with ArtifactRun('release-check', root=ROOT) as run:
        for path in (zip_path, checksum, baseline_path):
            run.protect(path)
        run.record['commit'] = args.commit
        def managed_get(url):
            run.checkpoint()
            data = get(url)
            if url.startswith('https://github.com/'):
                target = run.work / (hashlib.sha256(url.encode()).hexdigest() + '.download')
                target.write_bytes(data)
                run.record.setdefault('downloads', {})[target.name] = {'bytes': len(data), 'sha256': file_hash(target)}
                run.save()
            run.checkpoint()
            return data
        api = f'https://api.github.com/repos/{args.repo}'
        before = history_snapshot(args.repo, args.tag, managed_get)
        if before != baseline:
            raise RuntimeError('historical_release_changed')
        release = json.loads(managed_get(api + '/releases/tags/' + args.tag))
        source_url = f'https://github.com/{args.repo}/archive/refs/tags/{args.tag}.zip'
        if (release['tag_name'] != args.tag or release['draft'] or not release['immutable']
                or not re.search(r'^## 本次更新\s*\n\s*\S', release['body'], re.MULTILINE)
                or source_url not in release['body'] or 'GPL-3.0-only' not in release['body']):
            raise RuntimeError('public_release_body_or_state_invalid')
        ref = json.loads(managed_get(api + '/git/ref/tags/' + args.tag))['object']
        # 同时支持轻量 tag 和 annotated tag，不使用浮动 main 替代冻结提交。
        if ref['type'] == 'tag':
            ref = json.loads(managed_get(api + '/git/tags/' + ref['sha']))['object']
        if ref['type'] != 'commit' or ref['sha'] != args.commit:
            raise RuntimeError('public_release_tag_mismatch')
        result = verify_assets(release, args.repo, args.tag, args.commit, zip_path, checksum, get=managed_get)
        result.update(verify_source(managed_get(source_url), ROOT, args.commit))
        if history_snapshot(args.repo, args.tag, managed_get) != before:
            raise RuntimeError('historical_release_changed')
        final_release = json.loads(managed_get(api + '/releases/tags/' + args.tag))
        if release_identity(final_release) != release_identity(release):
            raise RuntimeError('public_release_changed_during_check')
        result.update(commit=args.commit, anonymous=True, history_unchanged=True)
        (run.evidence / 'release.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        (run.evidence / 'public-release.json').write_text(json.dumps(release_identity(release), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
