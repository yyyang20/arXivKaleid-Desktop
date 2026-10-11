# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""按首次／最新提交日发现候选，历史关联补查后固定当前版本。"""
from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser
import logging
import re
import urllib.parse
from zoneinfo import ZoneInfo

import main
from desktop.version_metadata import VersionStore

SEARCH_URL = "https://arxiv.org/search/advanced"
BATCH_SIZE = 100
PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 200
ID_PATTERN = r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})"


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        raise ValueError("arxiv_version_identity_invalid")
    return value


def version_url(url, kind):
    parsed = urllib.parse.urlsplit(url)
    match = re.fullmatch("/" + kind + "/(" + ID_PATTERN + r")v([1-9]\d*)(?:\.pdf)?", parsed.path)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} or parsed.query or parsed.fragment or not match:
        raise ValueError("arxiv_version_url_invalid")
    return match[1], int(match[2])


def timestamp(value):
    result = main.parse_submission_timestamp(value)
    if result is None:
        raise ValueError("arxiv_version_time_invalid")
    return result


def normalized_papers(xml):
    page = main.ArxivFeedPage(xml, logging.getLogger("arxivkaleid.version"))
    if page.raw_count != len(page.papers):
        raise ValueError("arxiv_version_response_missing")
    seen = set()
    for paper in page.papers:
        key = version_url(paper["id"], "abs")
        if key != (valid_id(paper["arxiv_id"]), paper["version"]) or key[0] in seen:
            raise ValueError("arxiv_version_response_mismatch")
        seen.add(key[0])
        if version_url(paper["abs_url"], "abs") != key or version_url(paper["pdf_url"], "pdf") != key:
            raise ValueError("arxiv_version_response_mismatch")
        published, updated = timestamp(paper["published"]), timestamp(paper["updated"])
        if (published > updated or (paper["version"] == 1 and published != updated)
                or not paper["summary"] or not paper["categories"]):
            raise ValueError("arxiv_version_metadata_conflict")
        # 发现时读取最新版；交给数据库、PDF 与模型前将 URL 和时间规范化并固定版本。
        for field, kind in (("id", "abs"), ("abs_url", "abs"), ("pdf_url", "pdf")):
            paper[field] = f"https://arxiv.org/{kind}/{key[0]}v{key[1]}"
        paper["published"], paper["updated"] = published.isoformat(), updated.isoformat()
    return page


def latest_metadata(xml, identities):
    page = normalized_papers(xml)
    papers = {p["arxiv_id"]: p for p in page.papers}
    if set(papers) != set(identities):
        raise ValueError("arxiv_version_response_missing")
    return papers


class SearchPage(HTMLParser):
    """只解析官方搜索结果身份与分页；网页结构不符合契约时整批失败。"""

    def __init__(self, data, start):
        super().__init__(convert_charrefs=True)
        self.identities, self.text, self.links = [], [], []
        self.result_depth = 0
        try:
            self.feed(data.decode("utf-8"))
            self.close()
        except (UnicodeError, ValueError):
            raise ValueError("arxiv_search_page_invalid") from None
        if self.result_depth:
            raise ValueError("arxiv_search_page_incomplete")
        text = " ".join(" ".join(self.text).split())
        matches = re.findall(r"Showing\s+([\d,]+)\s*[–-]\s*([\d,]+)\s+of\s+([\d,]+)\s+results", text)
        if not matches:
            if start == 0 and not self.identities and "Sorry, your query returned no results" in text:
                self.total = 0
                return
            raise ValueError("arxiv_search_paging_missing")
        if len(matches) != 1:
            raise ValueError("arxiv_search_paging_invalid")
        first, last, self.total = (int(n.replace(",", "")) for n in matches[0])
        if (first != start + 1 or last != min(start + SEARCH_PAGE_SIZE, self.total)
                or last < first or len(self.identities) != last - first + 1
                or len(set(self.identities)) != len(self.identities)):
            raise ValueError("arxiv_search_paging_invalid")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "li":
            if "arxiv-result" in attrs.get("class", "").split():
                if self.result_depth:
                    raise ValueError("arxiv_search_result_nested")
                self.result_depth, self.links = 1, []
            elif self.result_depth:
                self.result_depth += 1
        if tag == "a" and self.result_depth:
            parsed = urllib.parse.urlsplit(attrs.get("href", ""))
            match = re.fullmatch("/abs/(" + ID_PATTERN + r")(?:v[1-9]\d*)?", parsed.path)
            if match and parsed.scheme in {"http", "https"} and parsed.hostname == "arxiv.org" and not parsed.query and not parsed.fragment:
                self.links.append(valid_id(match[1]))

    def handle_endtag(self, tag):
        if tag == "li" and self.result_depth:
            self.result_depth -= 1
            if not self.result_depth:
                if len(self.links) != 1:
                    raise ValueError("arxiv_search_result_identity_invalid")
                self.identities.append(self.links[0])

    def handle_data(self, data):
        self.text.append(data)


def search_update_ids(category, day, request, on_page):
    # 日期网页只有自然日精度；扩大一天后通过当前 Atom 的 UTC 时间精确过滤。
    params = {"advanced": "", "terms-0-operator": "AND", "terms-0-term": category,
              "terms-0-field": "all", "classification-include_cross_list": "include",
              "date-filter_by": "date_range", "date-from_date": (day-timedelta(days=1)).isoformat(),
              "date-to_date": (day+timedelta(days=1)).isoformat(), "date-date_type": "submitted_date",
              "abstracts": "hide", "size": SEARCH_PAGE_SIZE, "order": "-submitted_date"}
    identities, total, page_number = set(), None, 0
    while True:
        params["start"] = len(identities)
        page = SearchPage(request(SEARCH_URL + "?" + urllib.parse.urlencode(params)), len(identities))
        if (total is not None and page.total != total) or identities & set(page.identities):
            raise ValueError("arxiv_search_paging_conflict")
        total = page.total
        identities.update(page.identities)
        page_number += 1
        on_page(page_number, len(page.identities), "AdvancedSearch")
        if len(identities) == total:
            return identities, page_number


def today_update_papers(category, day, request, on_page):
    lower, upper = main.beijing_submission_bounds(day)
    result, seen, total, previous_time, pages = {}, set(), None, None, 0
    start = 0
    while True:
        params = {"search_query": f"cat:{category}", "sortBy": "lastUpdatedDate",
                  "sortOrder": "descending", "start": start, "max_results": PAGE_SIZE}
        page = normalized_papers(request(main.ARXIV_API_URL + "?" + urllib.parse.urlencode(params)))
        if page.start != start or page.total is None or page.page_size is None or page.raw_count > PAGE_SIZE:
            raise ValueError("arxiv_atom_paging_invalid")
        if (total is not None and total != page.total) or start + page.raw_count > page.total:
            raise ValueError("arxiv_atom_paging_conflict")
        total = page.total
        if not page.raw_count and start < total:
            raise ValueError("arxiv_atom_paging_empty_before_end")
        before_day = False
        for paper in page.papers:
            identity, submitted = paper["arxiv_id"], timestamp(paper["updated"])
            if identity in seen or (previous_time is not None and submitted > previous_time):
                raise ValueError("arxiv_atom_paging_order_invalid")
            seen.add(identity)
            previous_time = submitted
            if submitted < lower:
                before_day = True
            elif submitted < upper:
                result[identity] = paper
        pages += 1
        on_page(pages, page.raw_count, "LatestUpdates")
        start += page.raw_count
        if before_day or start == total:
            return result, pages
        # 官方 API 有结果窗口限制；不能把窗口耗尽误报成完整成功。
        if start >= 30000:
            raise ValueError("arxiv_atom_result_window_exceeded")


def collect(day, categories, request, store: VersionStore, on_page, *, today=None):
    lower, upper = main.beijing_submission_bounds(day)
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    evidence, network_ids, pages = {}, set(), 0

    def remember(paper):
        old = evidence.get(paper["arxiv_id"])
        if old and old != paper:
            raise ValueError("arxiv_version_discovery_conflict")
        evidence[paper["arxiv_id"]] = paper

    for category in categories:
        category_pages = 0

        def page_event(page, count, source):
            nonlocal category_pages
            category_pages += 1
            on_page(category, category_pages, count, source)

        if day == today:
            found, _ = today_update_papers(category, day, request, page_event)
            for paper in found.values():
                remember(paper)
            network_ids.update(found)
        else:
            # 复用首次提交日的分页规则；历史更新只查限定日期网页，不向前扫描数月。
            def first_page(cat, selected_day, start, size):
                page = normalized_papers(request(main.build_arxiv_query_url(cat, selected_day, size, start=start)))
                page_event(0, page.raw_count, "FirstSubmissions")
                return page

            first = main.fetch_category_submission_pages(category=category, submission_date=day,
                page_size=PAGE_SIZE, fetch_page=first_page, logger=logging.getLogger("arxivkaleid.version"))
            for paper in first:
                remember(paper)
                network_ids.add(paper["arxiv_id"])
            found, _ = search_update_ids(category, day, request, page_event)
            network_ids.update(found)
        pages += category_pages

    previous = store.previous(day, categories)
    historical_ids = {p["arxiv_id"] for p in previous}
    # 历史关联只提供 ID 和日期证据；所有候选重新联网，不能信任本地最高版本。
    ids = sorted(network_ids | historical_ids)
    current = {}
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start+BATCH_SIZE]
        url = main.ARXIV_API_URL + "?" + urllib.parse.urlencode({"id_list": ",".join(batch), "max_results": len(batch)})
        current.update(latest_metadata(request(url), batch))
    result = {}
    for identity, paper in current.items():
        observed = evidence.get(identity)
        if observed and (paper["version"] < observed["version"] or paper["published"] != observed["published"]
                         or (paper["version"] == observed["version"] and paper != observed)):
            raise ValueError("arxiv_version_latest_conflict")
        qualifies = (identity in historical_ids or identity in evidence
                     or lower <= timestamp(paper["published"]) < upper
                     or lower <= timestamp(paper["updated"]) < upper)
        if qualifies and set(paper["categories"].split(", ")) & set(categories):
            result[identity] = paper
    ordered = sorted(result.values(), key=lambda p: (timestamp(p["updated"]), p["arxiv_id"], p["version"]), reverse=True)
    raw_count = sum(len(set(p["categories"].split(", ")) & set(categories)) for p in ordered)
    return ordered, raw_count, pages, len(result.keys() & network_ids), len(result.keys() - network_ids)
