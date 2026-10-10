# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""分类级 OAI 历史发现 + 指定版本 Atom 校验；不维护全站账本。"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET

import main
from desktop.version_metadata import VersionStore

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
RAW = "{http://arxiv.org/OAI/arXivRaw/}"
SETS = {"gr-qc": "physics:gr-qc", "astro-ph.HE": "physics:astro-ph:HE",
        "astro-ph.GA": "physics:astro-ph:GA"}
BATCH_SIZE = 100
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


def oai_page(xml, verb):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError("arxiv_oai_xml_invalid") from None
    if root.tag != OAI + "OAI-PMH":
        raise ValueError("arxiv_oai_xml_invalid")
    errors = root.findall(OAI + "error")
    if errors:
        if verb == "ListRecords" and len(errors) == 1 and errors[0].get("code") == "noRecordsMatch":
            return {}, ""
        raise ValueError("arxiv_oai_protocol_error")
    body = root.find(OAI + verb)
    if body is None:
        raise ValueError("arxiv_oai_body_missing")
    histories = {}
    for record in body.findall(OAI + "record"):
        header = record.find(OAI + "header")
        if header is None:
            raise ValueError("arxiv_oai_header_missing")
        if header.get("status") == "deleted":
            continue
        raw = record.find(OAI + "metadata/" + RAW + "arXivRaw")
        if raw is None:
            raise ValueError("arxiv_oai_history_missing")
        identity = valid_id(raw.findtext(RAW + "id", ""))
        if header.findtext(OAI + "identifier") != "oai:arXiv.org:" + identity:
            raise ValueError("arxiv_oai_identity_conflict")
        versions = {}
        for item in raw.findall(RAW + "version"):
            label = item.get("version", "")
            if not re.fullmatch(r"v[1-9]\d*", label):
                raise ValueError("arxiv_oai_version_invalid")
            try:
                value = parsedate_to_datetime(item.findtext(RAW + "date", ""))
            except (ValueError, TypeError):
                raise ValueError("arxiv_oai_time_invalid") from None
            if value.tzinfo is None:
                raise ValueError("arxiv_oai_time_invalid")
            number = int(label[1:])
            if number in versions:
                raise ValueError("arxiv_oai_version_duplicate")
            versions[number] = value.astimezone(timezone.utc)
        if not versions or sorted(versions) != list(range(1, max(versions) + 1)) or any(
                versions[n] < versions[n-1] for n in range(2, max(versions)+1)):
            raise ValueError("arxiv_oai_history_incomplete")
        if identity in histories:
            raise ValueError("arxiv_oai_record_duplicate")
        histories[identity] = versions
    tokens = body.findall(OAI + "resumptionToken")
    if len(tokens) > 1:
        raise ValueError("arxiv_oai_token_invalid")
    token = (tokens[0].text or "").strip() if tokens else ""
    if token and not histories:
        raise ValueError("arxiv_oai_empty_page")
    return histories, token


def discover(category, day, request, on_page):
    if category not in SETS:
        raise ValueError("arxiv_oai_set_unknown")
    lower, _ = main.beijing_submission_bounds(day)
    params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw", "set": SETS[category],
              "from": lower.date().isoformat()}
    merged, tokens, pages = {}, set(), set()
    page = 0
    while True:
        xml = request(OAI_URL + "?" + urllib.parse.urlencode(params))
        digest = hashlib.sha256(xml).hexdigest()
        histories, token = oai_page(xml, "ListRecords")
        if digest in pages or any(identity in merged for identity in histories):
            raise ValueError("arxiv_oai_page_repeated")
        pages.add(digest)
        merged.update(histories)
        page += 1
        on_page(page, len(histories))
        if not token:
            return merged, page
        if token in tokens:
            raise ValueError("arxiv_oai_token_repeated")
        tokens.add(token)
        params = {"verb": "ListRecords", "resumptionToken": token}


def exact_metadata(xml, expected):
    page = main.ArxivFeedPage(xml, logging.getLogger("arxivkaleid.version"))
    if page.raw_count != len(expected) or len(page.papers) != len(expected):
        raise ValueError("arxiv_version_response_missing")
    papers = {}
    for paper in page.papers:
        key = version_url(paper["id"], "abs")
        if key != (valid_id(paper["arxiv_id"]), paper["version"]) or key not in expected or key in papers:
            raise ValueError("arxiv_version_response_mismatch")
        if version_url(paper["abs_url"], "abs") != key or version_url(paper["pdf_url"], "pdf") != key:
            raise ValueError("arxiv_version_response_mismatch")
        published, updated = timestamp(paper["published"]), timestamp(paper["updated"])
        if updated != expected[key] or published > updated or not paper["summary"] or not paper["categories"]:
            raise ValueError("arxiv_version_metadata_conflict")
        # 规范传输差异，URL 一律钉死版本；时区文本归一化后才计算持久哈希。
        for field, kind in (("id", "abs"), ("abs_url", "abs"), ("pdf_url", "pdf")):
            paper[field] = f"https://arxiv.org/{kind}/{key[0]}v{key[1]}"
        paper["published"] = published.isoformat()
        paper["updated"] = updated.isoformat()
        papers[key] = paper
    return papers


def collect(day, categories, request, store: VersionStore, on_page):
    lower, upper = main.beijing_submission_bounds(day)
    histories, pages = {}, 0
    for category in categories:
        found, count = discover(category, day, request, lambda p, n: on_page(category, p, n))
        pages += count
        for identity, history in found.items():
            if identity in histories and histories[identity] != history:
                raise ValueError("arxiv_oai_history_conflict")
            histories[identity] = history
    previous = store.previous(day, categories)
    # 曾经发现的历史论文不因移出分类消失；仍联网核对同日最后版本。
    for identity in sorted({p["arxiv_id"] for p in previous} - histories.keys()):
        url = OAI_URL + "?" + urllib.parse.urlencode({"verb": "GetRecord", "metadataPrefix": "arXivRaw", "identifier": "oai:arXiv.org:" + identity})
        found, token = oai_page(request(url), "GetRecord")
        if token or set(found) != {identity}:
            raise ValueError("arxiv_oai_known_history_missing")
        histories.update(found)
    expected = {}
    for identity, history in histories.items():
        eligible = [v for v, time in history.items() if lower <= time < upper]
        if eligible:
            v = max(eligible)
            expected[(identity, v)] = history[v]
    papers = {}
    for key, submitted in expected.items():
        cached = store.get(key)
        if cached:
            # 校验缓存身份与联网历史；缓存不能为另一版或另一日提供替代。
            verified = (version_url(cached["id"], "abs"), timestamp(cached["updated"]))
            if verified != (key, submitted) or version_url(cached["pdf_url"], "pdf") != key:
                raise ValueError("arxiv_version_cache_conflict")
            papers[key] = cached
    missing = sorted(expected.keys() - papers.keys())
    for start in range(0, len(missing), BATCH_SIZE):
        keys = missing[start:start+BATCH_SIZE]
        url = main.ARXIV_API_URL + "?" + urllib.parse.urlencode({"id_list": ",".join(f"{i}v{v}" for i, v in keys), "max_results": len(keys)})
        papers.update(exact_metadata(request(url), {k: expected[k] for k in keys}))
    groups = {c: {} for c in categories}
    for key, paper in papers.items():
        if timestamp(paper["published"]) != histories[key[0]][1]:
            raise ValueError("arxiv_version_published_conflict")
        for category in set(paper["categories"].split(", ")) & set(categories):
            groups[category][key] = paper
    result = {key: p for group in groups.values() for key, p in group.items()}
    ordered = sorted(result.values(), key=lambda p: (timestamp(p["updated"]), p["arxiv_id"], p["version"]), reverse=True)
    return ordered, sum(len(g) for g in groups.values()), pages, len(result.keys() & set(missing)), len(result.keys() - set(missing))
