from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCUMENTS = (
    'AGENTS.md', 'desktop/AGENTS.md', 'README.md',
    'docs/README.md', 'docs/PROJECT_SPEC.md', 'docs/OPERATIONS.md',
    'docs/PROJECT_STRUCTURE.md', 'docs/CHANGELOG.md',
    'docs/desktop/README.md', 'docs/desktop/DESKTOP_SPEC.md',
    'docs/desktop/DESKTOP_OPERATIONS.md', 'docs/desktop/DESKTOP_STRUCTURE.md',
    'docs/public_release/README.md', 'docs/public_release/EULA.txt',
    'docs/public_release/PRIVACY.md', 'docs/public_release/SECURITY.md',
    'docs/public_release/RELEASE_CHECKLIST.md',
    'EULA.txt', 'PRIVACY.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.txt',
    'packaging/windows/THIRD_PARTY_NOTICES.txt',
)


def read_bytes(relative: str) -> bytes:
    # 只读指定维护文件，拒绝通过目录链接越出本项目。
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise AssertionError(f'document_outside_project: {relative}')
    return path.read_bytes()


def read_utf8(relative: str) -> str:
    return read_bytes(relative).decode('utf-8')


def literal_constant(relative: str, name: str):
    # AST 读取常量，不导入应用、启动 GUI 或创建 runtime。
    tree = ast.parse(read_utf8(relative), filename=relative)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f'missing_constant: {relative}:{name}')


def prose(text: str) -> str:
    # 示例代码中的标题和链接不作为文档治理结构。
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return '\n'.join(lines)


def section(text: str, title: str) -> str:
    match = re.search(
        rf'^## {re.escape(title)}[ \t]*(?:\n|\Z)(.*?)(?=^#{{1,2}} |\Z)',
        prose(text), re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f'missing_section: {title}')
    return match[1]


def table_rows(text: str) -> dict[str, str]:
    rows = {}
    for line in text.splitlines():
        if line.startswith('|'):
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if len(cells) == 2:
                if cells[0] in rows:
                    raise AssertionError(f'duplicate_table_row: {cells[0]}')
                rows[cells[0]] = cells[1]
    return rows


def identity_errors(text: str, expected: dict[str, str]) -> list[str]:
    rows = table_rows(section(text, '当前身份'))
    return [label for label, value in expected.items() if rows.get(label) != value]


def heading_anchors(text: str) -> set[str]:
    anchors = set()
    counts = {}
    for title in re.findall(r'^#{1,6} (.+?)\s*#*$', prose(text), re.MULTILINE):
        slug = re.sub(r'[^\w -]', '', title.lower()).replace(' ', '-')
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        anchors.add(slug if number == 0 else f'{slug}-{number}')
    return anchors


def link_errors(documents: dict[Path, str]) -> list[str]:
    failures = []
    pattern = re.compile(r'(?<!!)\[[^\]]+\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)')
    for source, text in documents.items():
        for raw in pattern.findall(prose(text)):
            target = raw.strip('<>')
            url = urlsplit(target)
            if url.scheme in ('http', 'https', 'mailto'):
                continue
            name = unquote(url.path)
            resolved = (source.parent / name).resolve() if name else source
            context = f'{source.relative_to(PROJECT_ROOT)}: {target}'
            # 先核验边界，再检查存在性；不读取链接指向的运行数据。
            if url.scheme or url.netloc or not resolved.is_relative_to(PROJECT_ROOT):
                failures.append(f'outside_project: {context}')
            elif not resolved.is_file():
                failures.append(f'missing_target: {context}')
            elif url.fragment and resolved.suffix == '.md':
                linked = documents.get(resolved)
                if linked is None or unquote(url.fragment) not in heading_anchors(linked):
                    failures.append(f'missing_anchor: {context}')
    return failures


def mirror_errors(sources: dict[str, bytes], copies: dict[str, bytes]) -> list[str]:
    return [name for name, value in sources.items() if copies.get(name) != value]


class DesktopGovernanceTests(unittest.TestCase):
    def assert_fragments(self, text, fragments):
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_required_documents_and_local_rule_inheritance(self):
        for relative in REQUIRED_DOCUMENTS:
            with self.subTest(document=relative):
                self.assertTrue((PROJECT_ROOT / relative).is_file())
                read_utf8(relative)
        self.assert_fragments(read_utf8('desktop/AGENTS.md'), (
            '[AGENTS.md](../AGENTS.md)', '[文档索引](../docs/README.md)',
            '[Desktop 文档入口](../docs/desktop/README.md)',
        ))

    def test_safety_and_necessary_authorization_contract(self):
        agents = read_utf8('AGENTS.md')
        self.assert_fragments(section(agents, '电脑与项目安全'), (
            '项目外原则上只允许与当前任务直接相关、必要、非敏感的只读核验',
            '用户明确授权、环境名称和用途已经明确', '指定的项目专用 Conda 环境',
            '新增未获授权的第三方依赖、修改其他 Conda 环境或扩大操作范围',
            '`base` 环境、其他 Conda 环境、系统 Python',
            '不得读取、复制、打印或输出 Secret', '上述电脑安全边界是绝对限制',
            '只允许作用于当前进程或子进程', '持久环境变量、永久 PATH',
            '临时设置使用后应恢复',
        ))
        self.assert_fragments(section(agents, '需求确认与必要授权'), (
            '普通、可逆且位于项目范围内', '可直接执行，无需额外确认',
            '会实质影响实现结果的歧义', '需要用户明确授权的受控操作',
            '实际费用', 'Git 提交、推送', '第三方依赖', '可能造成数据丢失',
            '不要求固定关键词', '该指令本身可以视为授权',
            '入口、次数、费用上限', '用户授权不能突破',
            '未经用户明确授权，不得在 Codex 中',
            '创建、修改、启用、禁用或删除 Automation',
            '定时监控、周期性检查或其他持续后台任务',
            'QThread 不属于 Codex 持续后台任务',
        ))
        self.assert_fragments(section(agents, '项目内修改'), (
            '不得覆盖、回退或提交用户的无关改动', '禁止使用 `git reset --hard`',
            '不得删除既有未跟踪文件', '项目内明确的忽略目录',
        ))

    def test_task_reading_and_desktop_operation_contract(self):
        agents = read_utf8('AGENTS.md')
        self.assert_fragments(section(agents, '每个任务开始前'), (
            '完整阅读本文件和 `docs/README.md`', '预期影响范围',
            '直接相关的配置、提示词、源码、测试', '动态扩展',
            '实时外部状态', '只读 GitHub API', '明确无关的改动必须保留',
        ))
        self.assert_fragments(section(agents, '外部操作与费用'), (
            '入口、次数和费用上限', '失败后不得自动重试或重跑',
            '候选抓取、一次性快照分析、本机构建和 portable 验证',
            '`docs/PROJECT_SPEC.md`', '`docs/OPERATIONS.md`',
            '身份、授权、费用和停止条件', '授权分别处理',
        ))

    def test_completion_is_owned_by_root_agents(self):
        agents = section(read_utf8('AGENTS.md'), '文档与完成要求')
        self.assert_fragments(agents, (
            '每个功能完成时', '新增或更新与变更相匹配的测试',
            '更新 `docs/CHANGELOG.md`', '按 `docs/README.md` 的职责路由',
            '不得覆盖历史版本', '同步版本、兼容读取、发行校验和测试',
            'Prompt 哈希', 'allowlist', '`BUILD_INFO.json`', 'ZIP 与 SHA-256',
            '完整离线测试', '`git diff --check`', '测试跳过必须报告',
            '不能仅凭相关测试被跳过而宣称完成', 'Windows 环境实际验证',
            '纯文档与治理测试变更不因此要求重新构建 ZIP',
            'Release', '非空的“本次更新”', '冻结提交', '重新核验 `main`',
        ))
        self.assert_fragments(read_utf8('docs/README.md'), (
            '功能完成后的统一同步义务由根目录 `AGENTS.md` 规定',
            '本表不建立第二套完成规则', '只修改真正受影响的文件',
            '不得静默让实现偏差覆盖规范',
        ))

    def test_document_authority_and_reading_routes(self):
        index = read_utf8('docs/README.md')
        self.assert_fragments(section(index, '权威职责与不一致处理'), (
            '核心筛选', '开发、验证和公开发布流程', '整体目录与核心模块职责',
            'GUI、凭据与候选快照行为', '源码启动、依赖、本机构建与 portable 验证',
            '包内文件与 runtime 数据职责', '核心筛选规则以 `PROJECT_SPEC.md` 为唯一来源',
            '不建立相互竞争的规则', 'CHANGELOG.md', '只记录已完成变化的历史',
        ))
        self.assert_fragments(section(index, '新任务阅读顺序'), (
            '完整阅读 `AGENTS.md` 和本文档', '根据当前任务读取相关文档',
            'desktop/DESKTOP_SPEC.md', 'desktop/DESKTOP_OPERATIONS.md',
            'desktop/DESKTOP_STRUCTURE.md', '维护源与发布检查清单', '动态扩展阅读',
        ))

    def test_update_routes_include_versions_and_public_sources(self):
        routes = table_rows(section(read_utf8('docs/README.md'), '文档更新规则'))
        expected = {
            'Codex 协作、安全、授权或完成规则': ('AGENTS.md', 'desktop/AGENTS.md'),
            '文档职责、阅读路线或更新路由': ('本文档', 'desktop/README.md'),
            '候选、筛选、PDF、标签、日报、数据库或费用规则': ('PROJECT_SPEC.md',),
            '开发、验证与公开发布流程': ('OPERATIONS.md', 'public_release/RELEASE_CHECKLIST.md'),
            '文件新增、删除、移动或职责改变': ('PROJECT_STRUCTURE.md',),
            'Desktop GUI、凭据或候选快照行为': ('desktop/DESKTOP_SPEC.md',),
            'Desktop 源码启动、依赖、本机构建或 portable 验证': ('desktop/DESKTOP_OPERATIONS.md',),
            'Desktop 包内文件或 runtime 数据职责': ('desktop/DESKTOP_STRUCTURE.md',),
            'Desktop 版本': ('PROJECT_SPEC.md', 'desktop/README.md',
                            'desktop/DESKTOP_SPEC.md', 'desktop/DESKTOP_OPERATIONS.md'),
            'Windows portable 打包说明、EULA、隐私或安全政策': (
                'public_release/', 'EULA.txt', 'PRIVACY.md', 'SECURITY.md', 'README 分别维护'),
            '第三方声明': ('packaging/windows/THIRD_PARTY_NOTICES.txt', '同名副本'),
            '实际 GitHub Release 发布': ('CHANGELOG.md',),
            '提示词、模板、策略或 schema 身份变化': ('兼容校验', '发行校验', '测试'),
        }
        for label, fragments in expected.items():
            with self.subTest(route=label):
                self.assertIn(label, routes)
                self.assert_fragments(routes[label], fragments)

    def test_current_identity_table_matches_config_and_source(self):
        config = json.loads(read_utf8('config.json'))
        expected = {
            'Desktop 版本': f"`{literal_constant('desktop/__init__.py', '__version__')}`",
            'Desktop 配置': f"`{literal_constant('desktop/config.py', 'CONFIG_VERSION')}`",
            'Round 1 模型': f"`{config['deepseek']['round1_model']}`",
            'Round 2 模型': f"`{config['deepseek']['round2_model']}`",
            'Round 1 提示词': f"`{config['versions']['round1_prompt_version']}`",
            'Round 2 提示词': f"`{config['versions']['round2_prompt_version']}`",
            '研究画像兼容标识': f"`{config['versions']['research_profile_version']}`",
            'Round 1 策略': f"`{config['round1_selection_policy_version']}`",
            'Round 2 策略': f"`{literal_constant('main.py', 'ROUND2_SELECTION_POLICY')}`",
            'Round 2 输出传输': f"`{literal_constant('main.py', 'ROUND2_OUTPUT_TRANSPORT')}`",
            'Desktop 主工作 SQLite': f"schema v{literal_constant('main.py', 'DESKTOP_SCHEMA_VERSION')}",
        }
        self.assertEqual(identity_errors(read_utf8('docs/PROJECT_SPEC.md'), expected), [])

    def test_current_desktop_version_references_match_source(self):
        version = literal_constant('desktop/__init__.py', '__version__')
        for relative in ('README.md', 'docs/desktop/README.md', 'docs/desktop/DESKTOP_SPEC.md',
                         'docs/desktop/DESKTOP_OPERATIONS.md'):
            text = read_utf8(relative)
            values = re.findall(r'当前版本(?:为)?\s*`([^`]+)`', text)
            values += re.findall(r'arXivKaleid-([\d.]+(?:-[\w.]+)?)-windows-x64', text)
            with self.subTest(document=relative):
                if relative != 'README.md':
                    self.assertTrue(values)
                if not values:
                    continue
                self.assertEqual(set(values), {version})

    def test_internal_markdown_links_resolve_within_project(self):
        paths = sorted(PROJECT_ROOT.glob('*.md')) + sorted((PROJECT_ROOT / 'docs').rglob('*.md'))
        paths += [PROJECT_ROOT / 'desktop/AGENTS.md']
        documents = {p.resolve(): read_utf8(p.relative_to(PROJECT_ROOT).as_posix()) for p in paths}
        self.assertEqual(link_errors(documents), [])

    def test_current_documents_do_not_restore_online_obligations(self):
        # 只检查现行文档；历史记录及“当前没有 Online 模块”等说明继续允许。
        forbidden_resources = (
            'automation_daily.py', 'daily_report_template.py',
            'config/automation_policy.json', 'profiles/research_profile.md',
            '.github/workflows/',
        )
        for relative in REQUIRED_DOCUMENTS:
            if not relative.endswith('.md') or relative == 'docs/CHANGELOG.md':
                continue
            text = read_utf8(relative)
            for resource in forbidden_resources:
                with self.subTest(document=relative, resource=resource):
                    self.assertNotIn(resource, text)
            for line in prose(text).splitlines():
                if any(word in line for word in ('没有', '不依赖', '不包含', '不得', '禁止')):
                    continue
                obligation = any(word in line for word in ('必须', '应当', '需要'))
                online_mode = any(word in line for word in ('正式自动', '手动回查', '受控重做', 'Issue 日报发布'))
                self.assertFalse(obligation and online_mode, relative)

    def test_public_document_mirrors_match_their_sources(self):
        sources = {name: read_bytes(f'docs/public_release/{name}')
                   for name in ('EULA.txt', 'PRIVACY.md', 'SECURITY.md')}
        sources['THIRD_PARTY_NOTICES.txt'] = read_bytes('packaging/windows/THIRD_PARTY_NOTICES.txt')
        # 使用原始字节比较，换行或编码漂移也必须被发现。
        copies = {name: read_bytes(name) for name in sources}
        self.assertEqual(mirror_errors(sources, copies), [])

    def test_release_checklist_preserves_identity_and_public_readback(self):
        checklist = read_utf8('docs/public_release/RELEASE_CHECKLIST.md')
        self.assert_fragments(section(checklist, '冻结开发版本'), (
            '`main`', '`origin/main`', '工作树干净', '完整离线测试',
            'GUI、Windows DPAPI 测试', '实际执行',
        ))
        self.assert_fragments(section(checklist, '构建与验证'), (
            '`BUILD_INFO.json`', '冻结提交', '`working_tree_clean`', '`true`',
            'portable 零模型验证', '另行授权', 'SHA-256',
        ))
        self.assert_fragments(section(checklist, '发行文档核对'), (
            '`docs/public_release/`', 'allowlist', 'THIRD_PARTY_NOTICES.txt',
            '字节一致', 'README 分别核对',
        ))
        self.assert_fragments(section(checklist, '发布 GitHub Release'), (
            'GitHub 写操作授权', 'Release notes 包含非空的 `## 本次更新`',
            '发布 Draft 前重新读取', '存在且非空', '发布后重新读取公开 Release',
            '未登录视角', 'Tag', '资产摘要',
        ))
        self.assert_fragments(section(checklist, '发布后文档收口'), (
            '同一公开发布任务', '`docs/CHANGELOG.md`', '不重建或替换已发布资产',
            'Git commit/push 必须获得相应授权',
        ))

    def test_changelog_remains_history_and_records_code_decoupling(self):
        changelog = read_utf8('docs/CHANGELOG.md')
        self.assert_fragments(changelog, (
            '仅表示历史事实', '不代表当前仓库仍有这些入口或测试',
            'Desktop 文档解耦第一阶段', 'Desktop 代码解耦',
        ))
        self.assert_fragments(section(changelog, '2026-10-01：Desktop 代码解耦'), (
            '独立配置与预算校验', 'schema v1', '独立源码运行测试', '未发布成品',
        ))

    def test_missing_or_wrong_identity_is_detected_in_memory(self):
        text = '## 当前身份\n\n| Desktop 版本 | `1.0` |\n'
        self.assertEqual(identity_errors(text, {'Desktop 版本': '`1.0`'}), [])
        for changed in ('## 当前身份\n', text.replace('`1.0`', '`2.0`')):
            self.assertEqual(identity_errors(changed, {'Desktop 版本': '`1.0`'}), ['Desktop 版本'])

    def test_broken_link_anchor_and_escape_are_detected_in_memory(self):
        source = PROJECT_ROOT / 'README.md'
        for target, expected in (
            ('docs/__governance_missing__.md', 'missing_target'),
            ('README.md#governance-missing-anchor', 'missing_anchor'),
            ('../outside.md', 'outside_project'),
        ):
            with self.subTest(target=target):
                errors = link_errors({source: f'# Title\n[link]({target})'})
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith(expected))

    def test_public_copy_drift_is_detected_in_memory(self):
        sources = {'PRIVACY.md': b'canonical\r\n'}
        self.assertEqual(mirror_errors(sources, dict(sources)), [])
        for copies in ({}, {'PRIVACY.md': b'changed'}, {'PRIVACY.md': b'canonical\n'}):
            self.assertEqual(mirror_errors(sources, copies), ['PRIVACY.md'])


if __name__ == '__main__':
    unittest.main()
