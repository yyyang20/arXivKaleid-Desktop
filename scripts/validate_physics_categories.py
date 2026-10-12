# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""获准后的 Physics 全目录与真实 GUI 抓取验证；隔离 runtime，硬禁止模型。"""
from datetime import datetime, timedelta
import argparse
from html.parser import HTMLParser
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
import deepseek_client
from desktop import app, pipeline, arxiv_versions
from desktop.categories import PHYSICS_CATEGORIES, PHYSICS_IDS, TAXONOMY_SOURCE, DEFAULT_CATEGORIES
from desktop.diagnostics import DesktopDiagnostics
from desktop.history import HistoryStore
from desktop.research_requirements import RequirementsStore
from desktop.version_metadata import VersionStore, encode_paper
from scripts.local_artifacts import ArtifactRun, temporary_environment, inventory, checked_path

REPRESENTATIVES = ('gr-qc', 'astro-ph.HE', 'cond-mat.stat-mech', 'hep-th', 'nlin.CD', 'physics.optics', 'quant-ph')
FETCH_LIMIT_SECONDS = 120


class TaxonomyHeaders(HTMLParser):
    """按原始 HTML 的大类层级读取，避免正文提取器把 Physics 大类与子组混淆。"""
    def __init__(self):
        super().__init__()
        self.tag = None
        self.parts = []
        self.physics = False
        self.archive = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag in {'h2', 'h3', 'h4'}:
            self.tag, self.parts = tag, []

    def handle_data(self, data):
        if self.tag:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != self.tag:
            return
        text = ''.join(self.parts).strip()
        if tag == 'h2':
            self.physics = text == 'Physics'
        elif self.physics and tag == 'h3':
            self.archive = text.rsplit('(', 1)[-1].rstrip(')')
        elif self.physics and tag == 'h4':
            match = re.fullmatch(r'([^ ]+) \((.+)\)', text)
            if not match:
                raise ValueError('taxonomy_heading_invalid')
            self.rows.append((match[1], match[2], self.archive))
        self.tag = None


class EmptySecretStore:
    def load(self):
        return ''

    def save(self, value):
        raise RuntimeError('credentials_forbidden_in_category_validation')


def prior_validation(path):
    """只接续已结案中断的完整前半程证据，不自动重跑任何已通过网络用例。"""
    source = checked_path(ROOT, path.as_posix())
    record = json.loads((source.parent.parent/'run.json').read_text(encoding='utf-8'))
    assert record['state'] == 'released' and source.name == 'physics-validation.json'
    content = source.read_bytes()
    prior = json.loads(content)
    expected = [[c.id, c.name, c.archive] for c in PHYSICS_CATEGORIES]
    assert prior['taxonomy']['rows'] == expected and prior['taxonomy']['count'] == 51
    assert not prior['ok'] and 'failure_type' not in prior and prior['model_http_attempts'] == 0
    assert [row['category'] for row in prior['probes']] == [c.id for c in PHYSICS_CATEGORIES]
    assert all(row['classification_match'] and row['count'] > 0 for row in prior['probes'])
    expected_cases = [(list((category,)), label) for category in REPRESENTATIVES
                      for label in ('today', 'historical_updated')]
    expected_cases += [(list(DEFAULT_CATEGORIES[:count]), f'combination_{count}') for count in (1, 2, 3)]
    assert [(row['categories'], row['label']) for row in prior['cases']] == expected_cases
    assert all(row['classification_match'] and row['failure_code'] is None
               and row['elapsed_seconds'] <= FETCH_LIMIT_SECONDS for row in prior['cases'])
    assert all(row['candidate_count'] > 0 and row['fixture_version'] > 1
               for row in prior['cases'] if row['label'] == 'historical_updated')
    prior['continued_from'] = dict(path=source.relative_to(ROOT).as_posix(),
                                  sha256=hashlib.sha256(content).hexdigest())
    return prior


def validate(run, prior=None):
    from PySide6.QtWidgets import QApplication
    for relative in ('config.json', 'docs/desktop/USER_GUIDE.md', 'assets/user-guide.svg',
                     *json.loads((ROOT/'config.json').read_text(encoding='utf-8'))['paths'].values()):
        target = run.work/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
    proof = prior or dict(ok=False, model_http_attempts=0, probes=[], cases=[], fetch_limit_seconds=FETCH_LIMIT_SECONDS)
    output = run.evidence/'physics-validation.json'
    def save():
        output.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    def forbidden(*args, **kwargs):
        raise RuntimeError('model_forbidden_in_category_validation')
    run.cleanup_allowed = False
    application = QApplication.instance() or QApplication([])
    diagnostics = DesktopDiagnostics(run.work)
    window = None
    requests = []
    original = main.fetch_arxiv_metadata
    def observed(*args, **kwargs):
        requests.append(dict(index=len(requests)+1))
        return original(*args, **kwargs)
    with patch.object(pipeline, 'PROJECT_ROOT', run.work), \
            patch.object(main, 'fetch_arxiv_metadata', side_effect=observed), \
            patch.object(deepseek_client.DeepSeekClient, 'request_json', forbidden), \
            patch.object(deepseek_client.DeepSeekClient, 'request_responses_json', forbidden), \
            patch.object(app.AnalysisAttempt, 'run', forbidden):
        try:
            # 官方页面仅在内存提取；证据保存目录身份和摘要，不保存 HTTP 正文。
            if prior is None:
                with urlopen(TAXONOMY_SOURCE, timeout=90) as response:
                    content = response.read()
                taxonomy = TaxonomyHeaders()
                taxonomy.feed(content.decode('utf-8'))
                expected = [(c.id,c.name,c.archive) for c in PHYSICS_CATEGORIES]
                assert taxonomy.rows == expected and len(PHYSICS_IDS) == 51, 'official_taxonomy_mismatch'
                proof['taxonomy'] = dict(source=TAXONOMY_SOURCE, sha256=hashlib.sha256(content).hexdigest(), count=51, rows=expected)
            save()
            cache = pipeline.runtime_path('cache','arxiv')
            cache.mkdir(parents=True, exist_ok=True)
            def lookup(params):
                xml = main.fetch_arxiv_metadata(url=main.ARXIV_API_URL+'?'+urlencode(params),
                    timeout_seconds=90, cache_dir=cache, interval_seconds=5,
                    logger=logging.getLogger('physics-validation'), subprocess_timeout_margin_seconds=15)
                return arxiv_versions.normalized_papers(xml).papers
            today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
            fixtures = {}
            for category in (() if prior else PHYSICS_CATEGORIES):
                print('Physics 探针：'+category.id, flush=True)
                # 只选验证夹具；这不是候选管线，不把这 30 条冒充完整抓取。
                papers = lookup(dict(search_query='cat:'+category.id, sortBy='lastUpdatedDate',
                                     sortOrder='descending', start=0, max_results=30))
                assert papers and all(category.id in p['categories'].split(', ') for p in papers), 'probe_category_mismatch'
                proof['probes'].append(dict(category=category.id, count=len(papers), classification_match=True,
                    identities=[(p['arxiv_id'],p['version']) for p in papers]))
                if category.id in REPRESENTATIVES:
                    eligible = [p for p in papers if p['version']>1 and today-timedelta(days=365) <=
                        arxiv_versions.timestamp(p['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date() < today]
                    assert eligible, 'historical_updated_fixture_missing'
                    fixtures[category.id] = eligible[0]
                save()
                run.checkpoint()
            window = app.DesktopWindow(EmptySecretStore(), diagnostics,
                history_store=HistoryStore(run.work), requirements_store=RequirementsStore(run.work))
            window.winId()
            window.windowHandle().setScreen(application.primaryScreen())
            window.show()
            retained = []
            def fetch(categories, day, label, expected=None):
                assert window.save_categories(categories)
                window.select_date(day)
                before = len(requests)
                started = time.monotonic()
                window.start_fetch()
                worker = window.worker
                assert worker is not None and worker.categories == tuple(categories)
                while window.worker is not None:
                    application.processEvents()
                    time.sleep(.02)
                elapsed = time.monotonic()-started
                result = window.snapshot
                row = dict(categories=list(categories), date=day.isoformat(), label=label,
                           elapsed_seconds=round(elapsed,3), request_count=len(requests)-before,
                           failure_code=worker.issue.code if worker.issue else None)
                proof['cases'].append(row)
                save()
                assert worker.issue is None, 'complete_fetch_failed'
                assert elapsed<=FETCH_LIMIT_SECONDS, 'fetch_performance_limit_exceeded'
                papers = result.papers if result else ()
                assert all(set(p['categories'].split(', ')) & set(categories) for p in papers), 'fetch_category_mismatch'
                assert len({p['arxiv_id'] for p in papers})==len(papers)
                if result:
                    assert result.categories == tuple(categories)
                    assert result.raw_count == sum(len(set(p['categories'].split(', ')) & set(categories)) for p in papers)
                    assert result.network_count+result.reused_count==result.unique_count
                    assert list(papers) == sorted(papers,key=lambda p:(arxiv_versions.timestamp(p['updated']),p['arxiv_id'],p['version']),reverse=True)
                    retained.append((result,tuple(encode_paper(p)[1] for p in papers)))
                if expected:
                    chosen = [p for p in papers if p['arxiv_id']==expected['arxiv_id']]
                    assert len(chosen)==1 and chosen[0]['version']>=expected['version'], 'latest_fixture_missing'
                    row['fixture_id'], row['fixture_version'] = chosen[0]['arxiv_id'], chosen[0]['version']
                for snapshot, digests in retained:
                    assert tuple(encode_paper(p)[1] for p in snapshot.papers)==digests
                row.update(candidate_count=len(papers), classification_match=True,
                    raw_count=result.raw_count if result else 0,
                    page_count=result.page_count if result else None,
                    network_count=result.network_count if result else 0,
                    reused_count=result.reused_count if result else 0)
                assert window.grab().save(str(run.evidence/f'case-{len(proof["cases"]):02d}.png'))
                save()
                print(json.dumps(row,ensure_ascii=False),flush=True)
                run.checkpoint()
                return result
            for category in (() if prior else REPRESENTATIVES):
                fetch((category,),today,'today')
                fixture = fixtures[category]
                day = arxiv_versions.timestamp(fixture['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date()
                fetch((category,),day,'historical_updated',fixture)
            if prior is None:
                day = arxiv_versions.timestamp(fixtures['gr-qc']['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date()
                for count in (1,2,3):
                    fetch(DEFAULT_CATEGORIES[:count],day,f'combination_{count}',fixtures['gr-qc'])
                previous = window.snapshot
                assert window.save_categories(('physics.optics',)) and window.snapshot is previous
                assert previous.categories==DEFAULT_CATEGORIES
            # 指定旧版提供明确标注的合成历史关联，随后必须联网刷新最新版。
            current = lookup(dict(id_list='2506.15886',max_results=1))[0]
            older = lookup(dict(id_list='2506.15886v5',max_results=1))[0]
            old_day = arxiv_versions.timestamp(older['updated']).astimezone(ZoneInfo('Asia/Shanghai')).date()
            assert current['version']>older['version'] and today-timedelta(days=365)<=old_day<=today
            VersionStore(run.work).save('explicit-old-version-category-fixture',old_day,('gr-qc',),[older],1)
            proof['historical_fixture'] = dict(synthetic_association=True,source='explicit_version_atom',
                arxiv_id=older['arxiv_id'],version=older['version'],date=old_day.isoformat(),sha256=encode_paper(older)[1])
            recovered = fetch(('gr-qc',),old_day,'saved_history_latest_refresh',current)
            assert recovered.reused_count>=1
            assert encode_paper(VersionStore(run.work).get((older['arxiv_id'],older['version'])))[1]==encode_paper(older)[1]
            frozen = tuple(encode_paper(p)[1] for p in recovered.papers)
            assert window.save_categories(('physics.optics',)) and window.snapshot is recovered
            assert recovered.categories == ('gr-qc',) and tuple(encode_paper(p)[1] for p in recovered.papers) == frozen
            proof.update(ok=True, immutable_snapshots=True, request_count=len(requests),
                         request_count_scope='continuation_only' if prior else 'complete_run')
            save()
        except Exception as exc:
            proof['failure_type']=type(exc).__name__
            save()
            raise
        finally:
            if window is not None:
                if window.worker is not None:
                    window.worker.wait()
                    application.processEvents()
                window.close()
                window.deleteLater()
                application.processEvents()
            diagnostics.close()
            inventory(ROOT,run.work)
            for path in diagnostics.log_directory.glob('*.jsonl'):
                run.capture(path,path.name)
            run.cleanup_allowed=True


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--continue-evidence', type=Path,
                        help='已获准结案的中断证据；仅接续尚未执行的历史补查，不重跑探针与17组抓取')
    args = parser.parse_args()
    prior = prior_validation(args.continue_evidence) if args.continue_evidence else None
    with ArtifactRun('release-check',root=ROOT) as run:
        run.record['purpose']='authorized_alpha13_physics_51_and_real_gui_categories'
        run.save()
        with temporary_environment(run):
            validate(run, prior)
