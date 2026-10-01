# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
# See LICENSE in the project root for the full license text.

"""冻结 GUI 入口；诊断模式只能在全新 runtime 中使用合成数据。"""
import sys

if "--portable-check" in sys.argv:
    from portable_check import run
    raise SystemExit(run(network="--arxiv" in sys.argv, visual="--visual-qa" in sys.argv))

if "--portable-recovery-check" in sys.argv:
    from portable_check import run_recovery
    raise SystemExit(run_recovery())

from desktop.app import main

raise SystemExit(main())
