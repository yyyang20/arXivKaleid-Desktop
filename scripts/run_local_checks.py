# Copyright (c) 2026 yyyang20
# SPDX-License-Identifier: GPL-3.0-only
"""已有 Python 环境中的离线测试薄入口；子进程临时目录受项目约束。"""
import argparse
import json
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.local_artifacts import ArtifactRun


def ordinary_test_failure(returncode, text):
    # 只有正常退出的 unittest 完整失败汇总才是普通诊断；崩溃、超时不推断。
    summary = re.search(
        r'^Ran [1-9][0-9]* tests? in [0-9]+(?:\.[0-9]+)?s\r?\n\s*\nFAILED \(([^()\r\n]+)\)\r?$',
        text, re.MULTILINE)
    if returncode != 1 or summary is None:
        return False
    counters = [re.fullmatch(r'(failures|errors|skipped|expected failures|unexpected successes)=([0-9]+)', part)
                for part in summary[1].split(', ')]
    return all(counters) and any(item[1] in ('failures', 'errors', 'unexpected successes')
                                 and int(item[2]) > 0 for item in counters)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    args = parser.parse_args()
    with ArtifactRun('tests', root=ROOT) as run:
        temporary = run.work / 'temp'
        temporary.mkdir()
        scratch = run.work / 'fixtures'
        scratch.mkdir()
        env = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE='1',
                   ARXIVKALEID_TEST_SCRATCH=scratch.relative_to(ROOT).as_posix())
        log = run.evidence / 'offline-tests.log'
        with log.open('wb') as stream:
            result = run.process([sys.executable, '-X', 'utf8', '-B', '-m', 'unittest',
                                  'discover', '-s', 'tests', '-p', args.pattern, '-v'],
                                 env=env, cwd=ROOT, stdout=stream, stderr=stream)
        (run.evidence / 'tests.json').write_text(json.dumps({'returncode': result.returncode,
            'pattern': args.pattern, 'model_http_attempts': 0}), encoding='utf-8')
        print(log.relative_to(ROOT).as_posix())
        if result.returncode:
            error = RuntimeError('offline_tests_failed; inspect recorded log')
            if ordinary_test_failure(result.returncode, log.read_text(encoding='utf-8', errors='replace')):
                run.mark_ordinary_failure(error, 'unittest_failure')
            raise error


if __name__ == '__main__':
    main()
