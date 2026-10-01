# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from desktop.diagnostics import DesktopDiagnostics
from desktop.errors import (
    FailureBoundary,
    PAPER_SCOPE,
    SYSTEM_SCOPE,
    classify_failure_scope,
    make_issue,
)


ROOT = Path(__file__).resolve().parents[1]


class DesktopDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".codex-validation"
        scratch.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_jsonl_schema_sequence_correlations_and_flush(self):
        diagnostics = DesktopDiagnostics(self.root)
        self.addCleanup(diagnostics.close)
        operation_id = diagnostics.operation_id()
        diagnostics.event(
            operation_id=operation_id,
            operation_type="analysis",
            stage="run_bound",
            state="complete",
            snapshot_id="snapshot-safe-id",
            fetch_operation_id="fetch-safe-id",
            run_id=17,
            counts={"candidate_count": 3},
        )
        diagnostics.event(
            operation_id=operation_id,
            operation_type="analysis",
            stage="analysis",
            state="complete",
            run_id=17,
            elapsed_ms=9,
        )
        self.assertTrue(diagnostics.persistent)
        self.assertEqual(
            diagnostics.log_directory,
            self.root / ".desktop-runtime" / "logs",
        )
        path = next(diagnostics.log_directory.glob("desktop-*.jsonl"))
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["sequence"] for item in records], [1, 2])
        self.assertTrue(all(item["schema_version"] == 1 for item in records))
        self.assertTrue(all(item["session_id"] == diagnostics.session_id for item in records))
        self.assertTrue(all(item["operation_id"] == operation_id for item in records))
        self.assertEqual(records[0]["snapshot_id"], "snapshot-safe-id")
        self.assertEqual(records[0]["fetch_operation_id"], "fetch-safe-id")
        self.assertEqual(records[0]["run_id"], 17)

    def test_privacy_canaries_and_safe_traceback(self):
        diagnostics = DesktopDiagnostics(self.root)
        self.addCleanup(diagnostics.close)
        secret = "sk-private-api-key-canary"
        prompt = "private prompt title summary fulltext canary"
        ciphertext = "dpapi-ciphertext-canary"
        try:
            local_value = secret
            raise RuntimeError("private-exception-message " + secret + " " + str(self.root))
        except RuntimeError as exc:
            diagnostics.event(
                operation_id=diagnostics.operation_id(),
                operation_type="analysis",
                stage="round1",
                state="fail",
                code="AKD-R1-UNEXPECTED",
                scope="system",
                details={
                    "api_key": secret,
                    "authorization": "Bearer " + secret,
                    "cookie": secret,
                    "ciphertext": ciphertext,
                    "prompt": prompt,
                    "summary": prompt,
                    "fulltext": prompt,
                    "raw_response": prompt,
                    "curl_stderr": prompt,
                    "environment": prompt,
                    "argv": prompt,
                    "http_status": 500,
                },
                unexpected=exc,
            )
            self.assertEqual(local_value, secret)
        data = next(diagnostics.log_directory.glob("desktop-*.jsonl")).read_text(encoding="utf-8")
        for forbidden in (secret, prompt, ciphertext, str(self.root), "private-exception-message"):
            self.assertNotIn(forbidden, data)
        record = json.loads(data)
        self.assertEqual(record["details"], {"http_status": 500})
        self.assertTrue(record["traceback"])
        self.assertEqual(set(record["traceback"][0]), {"module", "function", "line"})

    def test_memory_ring_fallback_never_raises_or_claims_persistence(self):
        with patch.object(Path, "open", side_effect=OSError("private path")):
            diagnostics = DesktopDiagnostics(self.root)
        self.assertFalse(diagnostics.persistent)
        self.assertIsNone(diagnostics.log_directory)
        for index in range(600):
            diagnostics.event(
                operation_id="operation-safe-id",
                operation_type="fetch",
                stage="fetch",
                state="complete",
                counts={"candidate_count": index},
            )
        self.assertEqual(len(diagnostics.memory_events), 512)
        self.assertEqual(diagnostics.memory_events[-1]["sequence"], 600)

    def test_thread_safe_sequence(self):
        diagnostics = DesktopDiagnostics(self.root)
        self.addCleanup(diagnostics.close)

        def emit_many():
            for _ in range(25):
                diagnostics.event(
                    operation_id="operation-safe-id",
                    operation_type="analysis",
                    stage="pdf",
                    state="complete",
                )

        threads = [threading.Thread(target=emit_many) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        records = [
            json.loads(line)
            for line in next(diagnostics.log_directory.glob("desktop-*.jsonl"))
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([record["sequence"] for record in records], list(range(1, 201)))

    def test_category_codes_keep_http_numbers_in_details(self):
        first = make_issue("AKD-FETCH-HTTP_RESPONSE_ERROR", details={"http_status": 401})
        second = make_issue("AKD-FETCH-HTTP_RESPONSE_ERROR", details={"http_status": 503})
        self.assertEqual(first.code, second.code)
        self.assertEqual(first.details["http_status"], 401)
        self.assertEqual(second.details["http_status"], 503)

    def test_scope_uses_actual_boundary_and_fails_closed(self):
        paper = FailureBoundary(True, True, True, True)
        shared_component = FailureBoundary(True, False, True, True)
        sqlite_failure = FailureBoundary(True, True, True, False, sqlite_failure=True)
        unresolved = FailureBoundary(True, True, False, True)
        self.assertEqual(classify_failure_scope(paper), PAPER_SCOPE)
        self.assertEqual(classify_failure_scope(shared_component), SYSTEM_SCOPE)
        self.assertEqual(classify_failure_scope(sqlite_failure), SYSTEM_SCOPE)
        self.assertEqual(classify_failure_scope(unresolved), SYSTEM_SCOPE)


if __name__ == "__main__":
    unittest.main()
