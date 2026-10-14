# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from desktop.config import load_config

ROOT = Path(__file__).resolve().parents[1]


class DesktopConfigTests(unittest.TestCase):
    def setUp(self):
        from scripts.local_artifacts import test_scratch
        scratch = test_scratch(ROOT)
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
        for name in self.config['paths'].values():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())

    def load(self, config):
        path = self.root / 'config.json'
        path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        return load_config(path)

    def test_current_contract_without_policy_or_profile_files(self):
        config = self.load(self.config)
        self.assertEqual(config['config_version'], 'desktop_config_v7')
        self.assertEqual(config['limits']['model_context_tokens'], 1_000_000)
        self.assertEqual(config['limits']['model_max_output_tokens'], 384_000)
        self.assertEqual(config['limits']['context_safety_margin_tokens'], 72_000)
        self.assertFalse((self.root / 'config').exists())
        self.assertFalse((self.root / 'profiles').exists())

    def test_token_input_output_and_context_limits_remain_validated(self):
        for change in (
            lambda c: c['limits'].update(max_round1_request_tokens=0),
            lambda c: c['limits'].update(max_round2_request_tokens=False),
            lambda c: c['limits'].update(context_safety_margin_tokens=1_000_000),
            lambda c: c['deepseek'].update(round1_max_output_tokens=384001),
            lambda c: c['deepseek'].update(round2_max_output_tokens=0),
        ):
            config = copy.deepcopy(self.config)
            change(config)
            with self.assertRaises(RuntimeError):
                self.load(config)

    def test_online_fields_and_wrong_protocol_or_paths_are_rejected(self):
        mutations = [
            lambda c: c.update(automation={}),
            lambda c: c.update(config_version='online_config'),
            lambda c: c.update(fetch_policy='latest_version'),
            lambda c: c['deepseek'].update(api_key_file='config/local_secret.json'),
            lambda c: c['deepseek'].update(max_retries=1),
            lambda c: c['versions'].update(round2_prompt_version='round2_v14'),
            lambda c: c['paths'].update(round1_prompt='../outside.txt'),
            lambda c: c['limits'].update(max_round2_pdf_pages=61),
            lambda c: c['limits'].update(max_round1_request_tokens=True),
            lambda c: c.update(round1_max_selected_n=11),
            lambda c: c.update(final_max_recommendations=6),
        ]
        for mutate in mutations:
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.subTest(config=config), self.assertRaises(RuntimeError):
                self.load(config)

    def test_missing_or_changed_prompt_cannot_start_analysis(self):
        prompt = self.root / self.config['paths']['round1_prompt']
        prompt.write_text('synthetic altered prompt', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'hash_mismatch'):
            self.load(self.config)
        prompt.unlink()
        with self.assertRaisesRegex(RuntimeError, 'prompt_unavailable'):
            self.load(self.config)

    def test_loader_does_not_create_a_default_config(self):
        with self.assertRaisesRegex(RuntimeError, 'config_unavailable'):
            load_config(self.root / 'missing.json')
        self.assertFalse((self.root / 'missing.json').exists())
