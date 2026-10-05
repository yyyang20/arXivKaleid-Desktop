# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import hashlib
from contextlib import chdir
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from desktop import paths, pipeline, analysis
from desktop.diagnostics import DesktopDiagnostics


class DesktopPortableTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".codex-validation"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.app = self.root / "portable"
        self.source.mkdir()
        self.app.mkdir()

    def frozen(self):
        for name, value in (("frozen", True), ("executable", str(self.app / "arXivKaleid.exe")),
                            ("_MEIPASS", str(self.app / "_internal"))):
            manager = patch.object(sys, name, value, create=True)
            manager.start()
            self.addCleanup(manager.stop)

    def test_source_roots_and_system_curl(self):
        self.assertEqual(paths.application_root(self.source), self.source)
        self.assertEqual(paths.resource_root(self.source), self.source)
        self.assertEqual(paths.runtime_path(self.source, "work"), self.source / ".desktop-runtime/work")
        self.assertEqual(paths.curl_executable(self.source), "curl")

    def test_frozen_paths_ignore_cwd_and_environment(self):
        self.frozen()
        with chdir(self.source), patch.dict(os.environ, {"APPDATA": str(self.source), "LOCALAPPDATA": str(self.source)}):
            paths.prepare_runtime(self.source)
            self.assertEqual(paths.resource_root(self.source), self.app / "_internal")
            for part in ("config/secret.dat", "history/history.sqlite", "work/arxiv_kaleid.sqlite", "work/round2_inputs.sqlite", "work/analysis.lock", "cache/arxiv", "pdfs/2026-09-25"):
                self.assertEqual(paths.runtime_path(self.source, part), self.app / "runtime" / part)
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertFalse((self.app / "_internal").exists())

    def test_escape_and_reparse_points_are_rejected(self):
        self.frozen()
        for part in ("../escape", str(self.root), "C:escape", "work/../../escape"):
            with self.assertRaises(ValueError):
                paths.runtime_path(self.source, part)
        real_lstat = Path.lstat
        from types import SimpleNamespace
        def lstat(p):
            if p.name == "runtime":
                return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(p)
        with patch.object(Path, "lstat", lstat):
            with self.assertRaises(ValueError):
                paths.runtime_path(self.source, "work")

    def test_unwritable_root_never_falls_back(self):
        self.frozen()
        with patch.object(Path, "mkdir", side_effect=PermissionError("private path")):
            with self.assertRaises(PermissionError):
                paths.prepare_runtime(self.source)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_bundled_curl_required_verified_and_never_uses_path(self):
        self.frozen()
        with patch.dict(os.environ, {"PATH": str(self.source)}):
            with self.assertRaises(FileNotFoundError):
                paths.curl_executable(self.source)
            vendor = self.app / "_internal/vendor/curl"
            (vendor / "bin").mkdir(parents=True)
            exe = vendor / "bin/curl.exe"
            exe.write_bytes(b"synthetic-binary-not-executed")
            manifest = {"bin/curl.exe": hashlib.sha256(exe.read_bytes()).hexdigest()}
            (vendor / "files.json").write_text(json.dumps(manifest))
            self.assertEqual(paths.curl_executable(self.source), str(exe))
            exe.write_bytes(b"corrupt")
            with self.assertRaises(ValueError):
                paths.curl_executable(self.source)
            (vendor / "files.json").write_text(json.dumps({"bin/curl.exe": "wrong", "../../escape": "wrong"}))
            with self.assertRaises(ValueError):
                paths.curl_executable(self.source)

    def test_packaged_work_lock_and_databases_remain_in_runtime(self):
        self.frozen()
        with patch.object(pipeline, "PROJECT_ROOT", self.source):
            with analysis.lock_work_directory():
                databases = analysis.initialize_work_paths()
                for p in databases:
                    self.assertEqual(p.parent, self.app / "runtime/work")
        self.assertEqual(list(self.source.iterdir()), [])

    def test_packaged_diagnostics_only_use_runtime_logs(self):
        self.frozen()
        diagnostics = DesktopDiagnostics(self.source)
        self.addCleanup(diagnostics.close)
        diagnostics.event(
            operation_id=diagnostics.operation_id(), operation_type="startup",
            stage="startup", state="complete",
        )
        self.assertTrue(diagnostics.persistent)
        self.assertEqual(diagnostics.log_directory, self.app / "runtime/logs")
        self.assertFalse((self.app / "_internal/logs").exists())
        self.assertEqual(list(self.source.iterdir()), [])
