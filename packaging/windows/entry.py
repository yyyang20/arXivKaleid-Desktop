"""冻结 GUI 入口；诊断模式只能在全新 runtime 中使用合成数据。"""
import sys

if "--portable-check" in sys.argv:
    from portable_check import run
    raise SystemExit(run(network="--arxiv" in sys.argv))

from desktop.app import main

raise SystemExit(main())
