# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""通过 GitHub 元数据核验公开发行及历史基线；不下载发行物、不写入远端、不自动重试。"""
import argparse
import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.local_artifacts import ArtifactRun, checked_path, file_hash, approved_portable
from scripts.build_windows_portable import RELEASE_WORKFLOW, verify_merge_identity


def require_metadata_url(url):
    parsed = urllib.parse.urlsplit(url)
    # 只允许本入口实际使用的元数据资源；GitHub API 也存在归档下载端点。
    path = re.fullmatch(r'/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(.*)', parsed.path)
    resources = (r'', r'/releases', r'/releases/tags/v[0-9A-Za-z.+-]+',
                 r'/git/matching-refs/tags/', r'/git/ref/heads/main',
                 r'/git/ref/tags/v[0-9A-Za-z.+-]+', r'/git/tags/[0-9a-f]{40}')
    if (parsed.scheme != 'https' or parsed.netloc != 'api.github.com'
            or path is None or not any(re.fullmatch(resource, path[1]) for resource in resources)):
        raise RuntimeError('public_release_metadata_url_invalid')


class MetadataRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 即使服务返回重定向，也不能转去下载资产或源码。
        require_metadata_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def github_metadata_get(url):
    require_metadata_url(url)
    # 不读取本地凭据；仅请求 JSON 元数据，每个请求只有一次 attempt。
    request = urllib.request.Request(url, headers={'User-Agent': 'arXivKaleid-release-check',
                                                 'Accept': 'application/vnd.github+json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), MetadataRedirectHandler())
    with opener.open(request, timeout=180) as response:
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


def history_snapshot(repo, exclude_tag, get=github_metadata_get):
    releases = paginated(repo, 'releases', get)
    refs = paginated(repo, 'git/matching-refs/tags/', get)
    return {'releases': sorted([release_identity(r) for r in releases if r['tag_name'] != exclude_tag], key=lambda r: r['id']),
            'tags': {r['ref']: r['object']['sha'] for r in refs if r['ref'] != 'refs/tags/' + exclude_tag}}


def archive_identity(tag, commit, zip_path, checksum):
    """先检查本地包身份；不将构建分支名当作发布资格。"""
    digest = file_hash(zip_path)
    if checksum.read_text(encoding='ascii').strip() != f'{digest}  {zip_path.name}':
        raise RuntimeError('public_release_checksum_mismatch')
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        prefix = zip_path.name[:-4] + '/'
        if (archive.testzip() is not None or len(names) != len(set(names))
                or any('/runtime/' in n for n in names)):
            raise RuntimeError('public_release_archive_invalid')
        info = json.loads(archive.read(prefix + 'BUILD_INFO.json'))
        if (info.get('commit') != commit or info.get('purpose') != 'public-release'
                or info.get('working_tree_clean') is not True or tag != 'v' + info.get('version', '')
                or zip_path.name != f"arXivKaleid-{info['version']}-windows-x64.zip"
                or checksum.name != zip_path.name + '.sha256'):
            raise RuntimeError('public_release_build_identity_mismatch')
        if info.get('release_workflow') not in (None, RELEASE_WORKFLOW):
            raise RuntimeError('release_workflow_identity_invalid')
    return info


def verify_assets(release, tag, commit, zip_path, checksum, *,
                  review_run_id=None, root=None):
    """仅将两份资产的 GitHub digest 与通过本地身份检查的文件哈希对比。"""
    info = archive_identity(tag, commit, zip_path, checksum)
    # 新流程包必须带人工批准；无新标识的历史包仍可单独只读核验。
    if info.get('release_workflow') == RELEASE_WORKFLOW and not review_run_id:
        raise RuntimeError('release_review_required')
    if review_run_id:
        approved_portable(root or ROOT, review_run_id, commit, zip_path, checksum)
    names = {zip_path.name, checksum.name}
    assets = release['assets']
    if len(assets) != 2 or {a['name'] for a in assets} != names:
        raise RuntimeError('public_release_assets_invalid')
    verified = []
    for asset in assets:
        local = zip_path if asset['name'] == zip_path.name else checksum
        digest = asset.get('digest')
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise RuntimeError('public_release_asset_digest_invalid')
        local_digest = file_hash(local)
        if digest != 'sha256:' + local_digest:
            raise RuntimeError('public_release_asset_digest_mismatch')
        verified.append(dict(name=local.name, local_sha256=local_digest, github_digest=digest))
    digest = file_hash(zip_path)
    return {'zip_sha256': digest, 'zip_bytes': zip_path.stat().st_size,
            'asset_digests_match_local': True, 'assets': verified}


def tag_commit(api, tag, get):
    ref = json.loads(get(api + '/git/ref/tags/' + tag))['object']
    seen = set()
    # 轻量和 annotated tag 均解析到提交，拒绝循环或非提交对象。
    while ref['type'] == 'tag':
        if ref['sha'] in seen:
            raise RuntimeError('public_release_tag_mismatch')
        seen.add(ref['sha'])
        ref = json.loads(get(api + '/git/tags/' + ref['sha']))['object']
    if ref['type'] != 'commit':
        raise RuntimeError('public_release_tag_mismatch')
    return ref['sha']


def main(argv=None, *, get=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--zip', required=True)
    parser.add_argument('--checksum', required=True)
    parser.add_argument('--history-baseline', required=True)
    parser.add_argument('--preflight', action='store_true', help='发布前检查原包批准及 C 已进入远端 main；不创建 tag/Release')
    parser.add_argument('--review-run-id', help='人工验收通过的 portable 运行 ID；新流程包必须指定')
    args = parser.parse_args(argv)
    if args.preflight and not args.review_run_id:
        parser.error('--preflight requires --review-run-id')
    get = get or github_metadata_get
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
            require_metadata_url(url)
            run.checkpoint()
            data = get(url)
            run.checkpoint()
            return data
        api = f'https://api.github.com/repos/{args.repo}'
        repository = json.loads(managed_get(api))
        if repository.get('private') is not False or repository.get('visibility') != 'public':
            raise RuntimeError('public_release_repository_not_public')
        before = history_snapshot(args.repo, args.tag, managed_get)
        if before != baseline:
            raise RuntimeError('historical_release_changed')
        if args.preflight:
            info = archive_identity(args.tag, args.commit, zip_path, checksum)
            if info.get('release_workflow') != RELEASE_WORKFLOW:
                raise RuntimeError('release_workflow_identity_missing')
            approval = approved_portable(ROOT, args.review_run_id, args.commit, zip_path, checksum)
            main_url = api + '/git/ref/heads/main'
            ref = json.loads(managed_get(main_url))['object']
            if ref['type'] != 'commit':
                raise RuntimeError('public_main_identity_invalid')
            changed = verify_merge_identity(ROOT, args.commit, ref['sha'])
            if (history_snapshot(args.repo, args.tag, managed_get) != before
                    or json.loads(managed_get(main_url))['object'] != ref):
                raise RuntimeError('public_release_changed_during_check')
            # 结束前再次核对原包与批准，避免检查期间文件变化。
            if approved_portable(ROOT, args.review_run_id, args.commit, zip_path, checksum) != approval:
                raise RuntimeError('release_review_artifact_mismatch')
            result = dict(preflight=True, commit=args.commit, main_commit=ref['sha'],
                          document_changes=changed, approval=approval, history_unchanged=True)
            (run.evidence / 'preflight.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
            return
        release = json.loads(managed_get(api + '/releases/tags/' + args.tag))
        if release['tag_name'] != args.tag or release['draft'] is not False:
            raise RuntimeError('public_release_state_invalid')
        if tag_commit(api, args.tag, managed_get) != args.commit:
            raise RuntimeError('public_release_tag_mismatch')
        result = verify_assets(release, args.tag, args.commit, zip_path, checksum,
                               review_run_id=args.review_run_id, root=ROOT)
        if history_snapshot(args.repo, args.tag, managed_get) != before:
            raise RuntimeError('historical_release_changed')
        final_release = json.loads(managed_get(api + '/releases/tags/' + args.tag))
        final_repository = json.loads(managed_get(api))
        if (release_identity(final_release) != release_identity(release)
                or tag_commit(api, args.tag, managed_get) != args.commit
                or final_repository.get('visibility') != 'public'
                or final_repository.get('private') is not False):
            raise RuntimeError('public_release_changed_during_check')
        if args.review_run_id:
            result['approval'] = approved_portable(ROOT, args.review_run_id, args.commit, zip_path, checksum)
        result.update(commit=args.commit, release_public=True,
                      verification='github-metadata', history_unchanged=True)
        (run.evidence / 'release.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        (run.evidence / 'public-release.json').write_text(json.dumps(release_identity(release), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
