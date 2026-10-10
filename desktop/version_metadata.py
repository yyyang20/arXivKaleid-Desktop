# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""轻量版本元数据；只持久保存成功候选，独立于分析与日报数据库。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from desktop import paths

SCHEMA_VERSION = 1
FETCH_POLICY = "beijing_version_day_v1"
FIELDS = {"id", "arxiv_id", "version", "title", "authors", "summary", "categories",
          "primary_category", "published", "updated", "abs_url", "pdf_url"}


class MetadataError(RuntimeError):
    """固定错误身份，不暴露数据库路径或正文。"""


def encode_paper(paper):
    if set(paper) != FIELDS or type(paper["version"]) is not int or paper["version"] < 1:
        raise MetadataError("metadata_identity_invalid")
    if any(not isinstance(v, str) for k, v in paper.items() if k != "version"):
        raise MetadataError("metadata_identity_invalid")
    value = json.dumps(dict(paper), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return value, hashlib.sha256(value.encode("utf-8")).hexdigest()


class VersionStore:
    def __init__(self, source_root: Path):
        self.source_root = source_root

    @property
    def database(self):
        return paths.runtime_path(self.source_root, "metadata", "arxiv_versions.sqlite")

    @contextmanager
    def connection(self, *, write=False):
        conn = None
        try:
            path = self.database
            if not path.exists() and not write:
                yield None
                return
            if write:
                path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path.as_uri() + ("?mode=rwc" if write else "?mode=ro"),
                                   uri=True, timeout=0)
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")  # 冲突写入立即拒绝，不排队重试。
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and not tables and write:
                conn.execute("""CREATE TABLE versions (
                    arxiv_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
                    metadata_json TEXT NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(arxiv_id,version))""")
                conn.execute("""CREATE TABLE queries (
                    query_id TEXT PRIMARY KEY, day TEXT NOT NULL, categories TEXT NOT NULL,
                    policy TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success','failed')),
                    raw_count INTEGER NOT NULL, unique_count INTEGER NOT NULL,
                    error_code TEXT NOT NULL)""")
                conn.execute("""CREATE TABLE query_versions (
                    query_id TEXT NOT NULL REFERENCES queries(query_id),
                    arxiv_id TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(query_id,arxiv_id,version),
                    FOREIGN KEY(arxiv_id,version) REFERENCES versions(arxiv_id,version))""")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                tables = {"versions", "queries", "query_versions"}
                version = SCHEMA_VERSION
            expected = {
                "versions": {"arxiv_id", "version", "metadata_json", "sha256"},
                "queries": {"query_id", "day", "categories", "policy", "fetched_at", "status",
                            "raw_count", "unique_count", "error_code"},
                "query_versions": {"query_id", "arxiv_id", "version"},
            }
            if version != SCHEMA_VERSION or tables != set(expected):
                raise MetadataError("metadata_schema_invalid")
            if any({r[1] for r in conn.execute(f"PRAGMA table_info({table})")} != columns
                   for table, columns in expected.items()):
                raise MetadataError("metadata_schema_invalid")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise MetadataError("metadata_corrupt")
            yield conn
            if write:
                conn.commit()
        except MetadataError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise MetadataError("metadata_storage_failed") from None
        finally:
            if conn is not None:
                conn.close()  # 未提交的整批候选自动回滚。

    @staticmethod
    def decode(row):
        paper = json.loads(row[2])
        value, digest = encode_paper(paper)
        if digest != row[3] or value != row[2] or (paper["arxiv_id"], paper["version"]) != tuple(row[:2]):
            raise MetadataError("metadata_conflict")
        return paper

    def get(self, key):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM versions WHERE arxiv_id=? AND version=?", key).fetchone() if conn else None
            return self.decode(row) if row else None

    def previous(self, day: date, categories):
        # 按已查询分类保留历史成员；失败和其他策略记录不构成缓存命中。
        with self.connection() as conn:
            if conn is None:
                return []
            rows = conn.execute("""SELECT DISTINCT v.* FROM versions v JOIN query_versions m
                USING(arxiv_id,version) JOIN queries q USING(query_id)
                WHERE q.day=? AND q.policy=? AND q.status='success'""", (day.isoformat(), FETCH_POLICY)).fetchall()
            papers = [self.decode(row) for row in rows]
            return [p for p in papers if set(categories) & set(p["categories"].split(", "))]

    def save(self, query_id, day, categories, papers, raw_count, *, error_code=""):
        if type(day) is not date or raw_count < len(papers) or (error_code and papers):
            raise MetadataError("metadata_query_invalid")
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?,?,?)", (
                query_id, day.isoformat(), json.dumps(list(categories)), FETCH_POLICY,
                datetime.now(timezone.utc).isoformat(), "failed" if error_code else "success",
                raw_count, len(papers), error_code,
            ))
            for paper in papers:
                value, digest = encode_paper(paper)
                key = (paper["arxiv_id"], paper["version"])
                old = conn.execute("SELECT metadata_json,sha256 FROM versions WHERE arxiv_id=? AND version=?", key).fetchone()
                if old and old != (value, digest):
                    raise MetadataError("metadata_conflict")
                conn.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?)", (*key, value, digest))
                conn.execute("INSERT INTO query_versions VALUES (?,?,?)", (query_id, *key))
