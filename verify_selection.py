#!/usr/bin/env python3
"""阶段选测与影子对照的策略层（issue #55，纯标准库，语法兼容 Python 3.9）。

verify.py 的选测策略：把「当前有效差异 + 开发阶段」翻译成建议的验证范围，
给不出可信建议时保守回退完整检查并解释原因。影子模式下建议只记录不裁剪，
实际执行仍由调用方决定（缺省完整门禁）；纯文档 PR 可由入口记录产品测试不适用。

设计边界（与 issue #49「选择策略」一致）：
- 判定只基于受版本控制的显式映射与 git 差异，不用 CodeGraph / AST 索引做依据；
  索引缺失或过期不构成「无需测试」。
- 基线解析不了、未知路径、规则损坏、清单不完整、策略或执行配置自身变化，
  一律回退完整检查并写明原因；空差异、解析失败绝不解释成「不用测」。
- 文档免产品测试走狭窄允许列表；技能指引、协议 / 安装资源、词典与 fixture
  不能按文件后缀放行。
- 映射表与允许列表都是版本化声明：`STRATEGY_VERSION` 随策略变化递增，
  同一版本、同一输入、同一阶段得到相同计划。映射条目修改走受审查的 PR。

`AE_SELECTION_MAP` 环境变量可指向与内置结构相同的 JSON 覆盖映射（测试与
实验用）；覆盖映射损坏同样保守回退，不静默采用部分规则。
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCHEMA = "ae-verify-selection/1"
STRATEGY_VERSION = 16

# 覆盖映射的环境变量（只记「用了覆盖」这个事实，路径不进记录）
OVERRIDE_ENV = "AE_SELECTION_MAP"

# 浏览器场景注册表（issue #53）的位置；只在规则引用场景时按需加载
SCENARIO_REGISTRY = "tests/e2e_scenarios.py"

# 「*」表示全部注册场景：注册表新增场景时规则自动跟上，不会漂移
ALL_SCENARIOS = "*"

# 回退原因类别。多个原因同时出现时按此顺序取第一个作为 kind，原因文本合并。
FALLBACK_KINDS = (
    "input_unavailable",      # git 快照或有效输入读不出来
    "bad_baseline",           # 基线缺失或解析不了
    "broken_rules",           # 映射表缺失字段、引用不存在的场景等
    "incomplete_manifest",    # 测试清单不完整 / 新增、删除、重命名的测试未纳入映射
    "unknown_path",           # 未分类路径（含改名伪装、未提交的新文件）
    "strategy_change",        # 验证入口、策略或 CI 配置自身变化，映射边界不可信
    "infrastructure_change",  # 测试基础设施（浏览器 / 服务入口、辅助文件）变化
    "shared_resource",        # fixture、技能资源等影响面不可静态判定的共享资源
    "guarded_doc",            # 不在允许列表内的说明文档，保守回退
    "no_changes",             # 相对基线没有差异：不解释成不用测，保守按完整检查
)

# 测试文件与测试基础设施的判定（tests/ 的处理内建，不进映射表）
TEST_FILE_RE = re.compile(r"^tests/test_[^/]+\.py$")
INFRA_TEST_FILES = ("tests/e2e_browser.py", "tests/e2e_scenarios.py", "tests/e2e_servers.py")


@dataclasses.dataclass(frozen=True)
class AllowEntry:
    prefix: str      # 精确路径，或以 / 结尾的目录前缀
    category: str    # 判定理由里展示的类别


@dataclasses.dataclass(frozen=True)
class SelectionRule:
    rid: str
    prefixes: tuple
    why: str
    selectors: tuple = ()
    scenarios: tuple = ()
    serve_check: bool = False
    fallback: Optional[str] = None   # 命中即回退的类别（FALLBACK_KINDS 之一）


# ── 内置映射（真实仓库；修改走受审查的 PR，并同步 STRATEGY_VERSION） ────────

DOC_ALLOWLIST = (
    AllowEntry("README.md", "仓库说明（英文）"),
    AllowEntry("README.zh-CN.md", "仓库说明（中文）"),
    AllowEntry("docs/assets/", "头图与截图"),
)

DOC_PR_REASON = "差异仅在文档允许列表内，由 CI 兜底；本地产品测试不适用。"

SELECTION_RULES = (
    SelectionRule(
        rid="cli-ae",
        prefixes=("ae.py",),
        selectors=("tests/test_ae_cli.py", "tests/test_ae_cli_i18n.py", "tests/test_ae_checks.py",
                   "tests/test_ae_checks_runtime.py", "tests/test_ae_checks_tools.py",
                   "tests/test_ae_repair.py", "tests/test_ae_target.py", "tests/test_ae_uninstall.py",
                   "tests/test_aux_cli_i18n.py", "tests/test_core.py", "tests/test_skill_updates.py"),
        why="ae.py 是安装 / 体检 / 修复 CLI，行为由这些模块覆盖",
    ),
    SelectionRule(
        rid="skill-updates",
        prefixes=("skill/annotation-everything/scripts/update.py",
                  "skill/annotation-everything/scripts/content_manifest.py",
                  "skill/annotation-everything/scripts/fsutil.py"),
        selectors=("tests/test_skill_updates.py", "tests/test_ae_cli.py", "tests/test_ae_target.py"),
        why="更新器与安装记录、副本和运行中的服务共同构成更新边界",
    ),
    SelectionRule(
        rid="protocol-notes",
        prefixes=("skill/annotation-everything/scripts/notes.py",
                  "skill/annotation-everything/scripts/notes_store.py",
                  "skill/annotation-everything/scripts/ledger.py",
                  "skill/annotation-everything/scripts/report_check.py",
                  "skill/annotation-everything/scripts/fsutil.py"),
        selectors=("tests/test_notes_exchange_i18n.py", "tests/test_core.py", "tests/test_handoff_i18n.py",
                   "tests/test_locale_loop.py", "tests/test_export_format.py",
                   "tests/test_skill_discovery.py", "tests/test_review_page_skills_i18n.py",
                   "tests/test_report_check.py", "tests/test_report_generation_resources.py",
                   "tests/test_report_reading.py", "tests/test_report_structure_visual.py",
                   "tests/test_report_revision.py", "tests/test_report_external_method.py",
                   "tests/test_docx_review.py", "tests/test_docx_anchor.py"),
        scenarios=("full-check", "locale-loop", "generated-draft-loop", "report-verdict-loop",
                   "revision-update-loop", "legacy-report-compat", "external-method-loop"),
        serve_check=True,
        why="审阅协议、共享存储与报告产物核验是服务/产物边界，变更必须带上服务安全自检",
    ),
    SelectionRule(
        rid="serve-page",
        prefixes=("skill/annotation-everything/scripts/serve.py",
                  "skill/annotation-everything/scripts/viewer_template.html",
                  "skill/annotation-everything/scripts/build_viewer.py",
                  "skill/annotation-everything/scripts/fsutil.py"),
        selectors=("tests/test_serve_locale.py", "tests/test_core.py", "tests/test_review_page_i18n.py",
                   "tests/test_review_page_editing_i18n.py", "tests/test_review_page_skills_i18n.py",
                   "tests/test_aux_cli_i18n.py", "tests/test_notes_exchange_i18n.py",
                   "tests/test_docx_review.py", "tests/test_docx_anchor.py"),
        scenarios=(ALL_SCENARIOS,),
        serve_check=True,
        why="服务与页面模板影响全部浏览器场景与服务安全约束",
    ),
    SelectionRule(
        rid="analysis",
        prefixes=("skill/annotation-everything/scripts/analysis_store.py",
                  "skill/annotation-everything/scripts/notes.py",
                  "skill/annotation-everything/scripts/notes_store.py",
                  "skill/annotation-everything/scripts/ledger.py",
                  "skill/annotation-everything/scripts/serve.py",
                  "skill/annotation-everything/scripts/viewer_template.html",
                  "skill/annotation-everything/scripts/build_viewer.py",
                  "skill/annotation-everything/scripts/wakeup.py",
                  "skill/annotation-everything/scripts/i18n.py",
                  "skill/annotation-everything/locales/"),
        selectors=("tests/test_analysis_store.py", "tests/test_analysis_agent.py",
                   "tests/test_analysis_page.py", "tests/test_analysis_lifecycle.py",
                   "tests/test_analysis_protection.py", "tests/test_analysis_protection_agent.py",
                   "tests/test_analysis_protection_page.py", "tests/test_analysis_history.py",
                   "tests/test_analysis_history_agent.py", "tests/test_analysis_history_page.py",
                   "tests/test_analysis_resolution.py", "tests/test_analysis_resolution_agent.py",
                   "tests/test_analysis_resolution_page.py", "tests/test_cli_error_i18n.py",
                   "tests/test_agent_delivery.py", "tests/test_receiver_binding.py"),
        scenarios=("analysis-smoke", "analysis-ui", "receiver-binding", "full-check"),
        serve_check=True,
        why="分析存储、改稿联动、投递与页面共享协议边界，覆盖全部分析契约及接收方兼容性",
    ),
    SelectionRule(
        rid="docx-review",
        prefixes=("skill/annotation-everything/scripts/docx_review.py", "skill/annotation-everything/scripts/docx_edit.py", "fixtures/docx/"),
        selectors=("tests/test_docx_review.py", "tests/test_docx_anchor.py", "tests/test_docx_edit.py", "tests/test_core.py", "tests/test_review_page_i18n.py"),
        scenarios=("docx-review",),
        why="DOCX 渲染器、回写与合成样例：分页、诊断、字节保留、写前闸门及失败恢复由 DOCX 用例覆盖；"
            "浏览器场景钉住原分页显示、限制清单、点/框选标注与只读提示（服务安全自检由 pr 门禁覆盖）",
    ),
    SelectionRule(
        rid="pptx-review",
        prefixes=("skill/annotation-everything/scripts/pptx_reader.py",
                  "skill/annotation-everything/scripts/pptx_report.py",
                  "skill/annotation-everything/scripts/pptx_edit.py",
                  "fixtures/pptx/"),
        selectors=("tests/test_pptx_reader.py", "tests/test_pptx_report.py",
                   "tests/test_pptx_annotations.py", "tests/test_pptx_remap.py",
                   "tests/test_pptx_edit.py", "tests/test_pptx_ops.py",
                   "tests/test_core.py", "tests/test_review_page_i18n.py"),
        scenarios=("pptx-review",),
        why="PPTX 读取、构建、回写与合成样例：包读取、能力矩阵、渲染诊断、幻灯片定位、重映射与白名单回写由 PPT 用例覆盖；"
            "浏览器场景钉住直接审阅、元素/文本/区域批注、只读元素、旧服务阻断与改后重建（服务安全自检由 pr 门禁覆盖）",
    ),
    SelectionRule(
        rid="review-page",
        prefixes=("skill/annotation-everything/scripts/match.py",
                  "skill/annotation-everything/scripts/skill_sources.py"),
        selectors=("tests/test_review_page_i18n.py", "tests/test_review_page_skills_i18n.py",
                   "tests/test_skill_discovery.py"),
        scenarios=("skill-search", "skill-recommendations", "skill-i18n"),
        why="页面匹配与技能发现逻辑",
    ),
    SelectionRule(
        rid="dict",
        prefixes=("skill/annotation-everything/scripts/i18n.py", "locales/",
                  "skill/annotation-everything/locales/"),
        selectors=("tests/test_i18n_catalog_contract.py", "tests/test_i18n.py", "tests/test_serve_locale.py",
                   "tests/test_locale_loop.py", "tests/test_ae_cli_i18n.py", "tests/test_aux_cli_i18n.py",
                   "tests/test_handoff_i18n.py", "tests/test_notes_exchange_i18n.py",
                   "tests/test_review_page_i18n.py", "tests/test_review_page_editing_i18n.py",
                   "tests/test_review_page_skills_i18n.py", "tests/test_export_format.py"),
        scenarios=(ALL_SCENARIOS,),
        why="词典与界面文案；全局契约模块显式在列，局部运行不丢全局保护",
    ),
    SelectionRule(
        rid="export-fonts",
        prefixes=("skill/annotation-everything/scripts/export_format.py",
                  "skill/annotation-everything/scripts/check_pptx_fonts.py"),
        selectors=("tests/test_export_format.py", "tests/test_pptx_fonts.py",
                   "tests/test_aux_cli_i18n.py", "tests/test_script_formats.py"),
        why="导出格式与字体检查 CLI",
    ),
    # 以下规则命中即回退：影响面不可静态判定或属于策略 / 配置自身
    SelectionRule(
        rid="skill-assets",
        prefixes=("skill/annotation-everything/SKILL.md", "skill/annotation-everything/references/",
                  "skill/annotation-everything/evals/"),
        fallback="shared_resource",
        why="技能指引、协议说明与评估资源影响行为面广，不能按扩展名放行",
    ),
    SelectionRule(
        rid="fixtures",
        prefixes=("fixtures/",),
        fallback="shared_resource",
        why="合成 fixture 被浏览器场景与多个测试引用，不能只按扩展名跳过",
    ),
    SelectionRule(
        rid="exec-config",
        prefixes=("verify.py", "verify_selection.py", "verify_evidence.py", "verify_report.py", "verify_parallel.py", "host_isolation.py", ".github/", "tools/"),
        fallback="strategy_change",
        why="验证入口、策略与 CI 配置自身变化，映射边界不可信",
    ),
    SelectionRule(
        rid="guarded-docs",
        prefixes=("docs/", "AGENTS.md", "CLAUDE.md", "LICENSE", ".gitignore", ".gitattributes"),
        fallback="guarded_doc",
        why="不在狭窄允许列表内的说明文档，保守回退完整检查",
    ),
)


# ── 覆盖映射与环境变量 ─────────────────────────────────────────────────────

def _parse_override(text: str) -> tuple:
    """覆盖映射 JSON -> (payload 或 None, problems)。"""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return None, ["AE_SELECTION_MAP 不是合法 JSON：%s" % exc]
    if not isinstance(payload, dict):
        return None, ["AE_SELECTION_MAP 顶层必须是对象"]
    return payload, []


def _payload_to_strategy(payload: dict, problems: list) -> tuple:
    """JSON payload -> (rules, allowlist, version)；结构问题记进 problems。"""
    rules, allowlist = [], []
    version = payload.get("version")
    if not isinstance(version, int):
        problems.append("覆盖映射缺少整数 version 字段")
        version = None
    raw_allow = payload.get("doc_allowlist")
    if isinstance(raw_allow, list):
        for entry in raw_allow:
            if (isinstance(entry, dict) and isinstance(entry.get("prefix"), str) and entry["prefix"]
                    and isinstance(entry.get("category"), str)):
                allowlist.append(AllowEntry(entry["prefix"], entry["category"]))
            else:
                problems.append("doc_allowlist 条目必须是 {prefix, category}：%r" % (entry,))
    else:
        problems.append("覆盖映射缺少 doc_allowlist 数组")
    raw_rules = payload.get("rules")
    if isinstance(raw_rules, list):
        seen = set()
        for index, raw in enumerate(raw_rules):
            rule = _rule_from_payload(raw, problems, index)
            if rule is not None:
                if rule.rid in seen:
                    problems.append("规则 ID 重复：%s" % rule.rid)
                seen.add(rule.rid)
                rules.append(rule)
    else:
        problems.append("覆盖映射缺少 rules 数组")
    return rules, allowlist, version


def _rule_from_payload(raw, problems: list, index: int) -> Optional[SelectionRule]:
    if not isinstance(raw, dict):
        problems.append("rules[%d] 必须是对象" % index)
        return None
    rid = raw.get("id")
    prefixes = raw.get("prefixes")
    why = raw.get("why")
    if not isinstance(rid, str) or not rid:
        problems.append("rules[%d] 缺少 id" % index)
        return None
    if not isinstance(prefixes, list) or not prefixes or not all(
            isinstance(item, str) and item for item in prefixes):
        problems.append("规则 %s 的 prefixes 必须是非空字符串数组" % rid)
        return None
    if not isinstance(why, str) or not why:
        problems.append("规则 %s 缺少可解释的 why" % rid)
        return None
    selectors = raw.get("selectors") or []
    if not isinstance(selectors, list) or not all(isinstance(item, str) and item for item in selectors):
        problems.append("规则 %s 的 selectors 必须是字符串数组" % rid)
        return None
    scenarios = raw.get("scenarios") or []
    if not isinstance(scenarios, list) or not all(isinstance(item, str) and item for item in scenarios):
        problems.append("规则 %s 的 scenarios 必须是字符串数组" % rid)
        return None
    fallback = raw.get("fallback")
    if fallback is not None and fallback not in FALLBACK_KINDS:
        problems.append("规则 %s 的 fallback 类别不认识：%r" % (rid, fallback))
        return None
    if not selectors and fallback is None:
        problems.append("规则 %s 既没有 selectors 也没有 fallback，命中后没有可执行的建议" % rid)
        return None
    return SelectionRule(rid=rid, prefixes=tuple(prefixes), why=why, selectors=tuple(selectors),
                         scenarios=tuple(scenarios), serve_check=bool(raw.get("serve_check")),
                         fallback=fallback)


def load_strategy(repo: Path, env: Optional[dict] = None) -> dict:
    """加载策略：环境变量覆盖优先，损坏的覆盖映射回退内置并解释。

    返回 {"rules", "allowlist", "version", "source", "problems", "override_used"}。
    problems 非空时调用方回退完整检查（broken_rules），problems 里写明原因。
    """
    env = os.environ if env is None else env
    override = env.get(OVERRIDE_ENV)
    if override:
        try:
            text = Path(override).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"rules": SELECTION_RULES, "allowlist": DOC_ALLOWLIST,
                    "version": STRATEGY_VERSION, "source": "builtin",
                    "problems": ["覆盖映射读不出来（%s），按内置映射继续校验" % exc],
                    "override_used": True}
        payload, problems = _parse_override(text)
        if payload is None:
            return {"rules": SELECTION_RULES, "allowlist": DOC_ALLOWLIST,
                    "version": STRATEGY_VERSION, "source": "builtin",
                    "problems": problems, "override_used": True}
        rules, allowlist, version = _payload_to_strategy(payload, problems)
        if problems:
            return {"rules": SELECTION_RULES, "allowlist": DOC_ALLOWLIST,
                    "version": STRATEGY_VERSION, "source": "builtin",
                    "problems": problems, "override_used": True}
        return {"rules": tuple(rules), "allowlist": tuple(allowlist), "version": version,
                "source": "override", "problems": [], "override_used": True}
    return {"rules": SELECTION_RULES, "allowlist": DOC_ALLOWLIST, "version": STRATEGY_VERSION,
            "source": "builtin", "problems": [], "override_used": False}


# ── 浏览器场景注册表（按需加载） ───────────────────────────────────────────

def load_scenario_ids(repo: Path) -> tuple:
    """返回 (场景 ID 元组或 None, 问题或 None)。只在规则引用场景时调用。"""
    path = repo / SCENARIO_REGISTRY
    if not path.is_file():
        return None, "浏览器场景注册表不存在（%s）" % SCENARIO_REGISTRY
    try:
        spec = importlib.util.spec_from_file_location("ae_verify_selection_scenarios", path)
        module = importlib.util.module_from_spec(spec)
        # dataclass 等运行时反射会经 sys.modules 找模块对象；不注册会报
        # 'NoneType' object has no attribute '__dict__'，被误判成注册表损坏
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    except BaseException as exc:  # 注册表坏了不能拖垮入口，只能保守回退
        return None, "浏览器场景注册表加载失败：%s: %s" % (type(exc).__name__, exc)
    scenarios = getattr(module, "SCENARIOS", None) or []
    ids = []
    for scenario in scenarios:
        identifier = getattr(scenario, "id", None)
        if identifier is None and isinstance(scenario, dict):
            identifier = scenario.get("id")
        if isinstance(identifier, str) and identifier:
            ids.append(identifier)
    if not ids:
        return None, "浏览器场景注册表没有可用的场景 ID"
    return tuple(ids), None


# ── 有效差异收集 ───────────────────────────────────────────────────────────

def git_output(repo: Path, args: list) -> tuple:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def collect_changes(repo: Path, baseline_commit: Optional[str], snapshot: dict) -> dict:
    """相对基线的有效差异：新增、删除、重命名两端、修改与未提交文件。

    staged 的重命名由 git 的 rename 检测给出两端；工作树里未暂存的重命名
    表现为「删除 + 未跟踪」，两端同样被收集，不会只看到一半。
    """
    changes = {"added": [], "deleted": [], "modified": [], "renamed": [], "untracked": [],
               "staged": list(snapshot.get("staged") or []),
               "unstaged": list(snapshot.get("unstaged") or []),
               "counts": dict(snapshot.get("counts") or {})}
    if not baseline_commit:
        return changes
    code, out = git_output(repo, ["diff", "--name-status", "-z", "-M", baseline_commit])
    if code != 0:
        return changes
    fields = out.split("\0")
    index = 0
    untracked = set(snapshot.get("untracked") or [])
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        kind = status[:1]
        path = fields[index] if index < len(fields) else ""
        index += 1
        if kind == "R" or kind == "C":
            # git diff 的 rename / copy 输出旧路径在前：R100\0旧\0新
            new_path = fields[index] if index < len(fields) else ""
            index += 1
            changes["renamed"].append({"from": path, "to": new_path})
            continue
        if kind == "A":
            changes["added"].append(path)
        elif kind == "D":
            changes["deleted"].append(path)
        elif kind in ("M", "T"):
            changes["modified"].append(path)
        else:
            changes["modified"].append(path)  # 未知状态按修改保守处理
    changes["untracked"] = sorted(untracked)
    for key in ("added", "deleted", "modified"):
        changes[key] = sorted(set(changes[key]) - untracked)
    return changes


# ── 分类与建议 ─────────────────────────────────────────────────────────────

def _allowlist_category(path: str, allowlist: tuple) -> Optional[str]:
    for entry in allowlist:
        if entry.prefix.endswith("/"):
            if path.startswith(entry.prefix):
                return entry.category
        elif path == entry.prefix:
            return entry.category
    return None


def _rule_hits(path: str, rules: tuple) -> list:
    hits = []
    for rule in rules:
        for prefix in rule.prefixes:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                hits.append(rule)
                break
    return hits


def _is_test_file(path: str) -> bool:
    return bool(TEST_FILE_RE.match(path))


def _relevant_paths(changes: dict) -> list:
    paths = []
    for key in ("added", "deleted", "modified", "untracked"):
        paths.extend(changes.get(key) or [])
    for item in changes.get("renamed") or []:
        paths.append(item["from"])
        paths.append(item["to"])
    return sorted(set(paths))


def documentation_only(selection: dict) -> bool:
    """仅内置允许列表可豁免 PR 产品测试；缺失、回退和覆盖映射均从紧。"""
    if (not isinstance(selection, dict) or selection.get("available") is not True
            or selection.get("strategy_source") != "builtin" or selection.get("fallback") is not None):
        return False
    guard = selection.get("policy_guard")
    if guard is not None and (not isinstance(guard, dict) or guard.get("active") is not False):
        return False
    changes = selection.get("changes")
    if not isinstance(changes, dict):
        return False
    # 暂存后又在工作树恢复的源码仍属于待提交改动，不能被净差异掩盖。
    paths = []
    for key in ("added", "deleted", "modified", "untracked", "staged", "unstaged"):
        values = changes.get(key)
        if not isinstance(values, list):
            return False
        paths.extend(values)
    renamed = changes.get("renamed")
    if not isinstance(renamed, list):
        return False
    for entry in renamed:
        if not isinstance(entry, dict):
            return False
        paths.extend((entry.get("from"), entry.get("to")))
    if any(not isinstance(path, str) or not path for path in paths):
        return False
    return bool(paths) and all(_allowlist_category(path, DOC_ALLOWLIST) is not None for path in paths)


def _classify(repo: Path, changes: dict, strategy: dict, scenario_ids: Optional[tuple]) -> dict:
    """逐路径分类：允许列表 -> tests 内建 -> 映射规则 -> 未知。"""
    rules = strategy["rules"]
    allowlist = strategy["allowlist"]
    result = {"matched_rules": {}, "doc_exempt": [], "unknown_paths": [],
              "fallbacks": [], "self_selectors": set(),
              "manifest": {"new_tests": [], "deleted_tests": [], "renamed_tests": []}}
    touched_tests = False
    for path in _relevant_paths(changes):
        category = _allowlist_category(path, allowlist)
        if category is not None:
            result["doc_exempt"].append({"path": path, "category": category})
            continue
        if path.startswith("tests/"):
            touched_tests = True
            renamed_entry = next((item for item in changes.get("renamed") or []
                                  if path in (item["from"], item["to"])), None)
            if _is_test_file(path):
                if renamed_entry is not None:
                    result["manifest"]["renamed_tests"].append(
                        {"from": renamed_entry["from"], "to": renamed_entry["to"]})
                elif path in (changes.get("added") or []) or path in (changes.get("untracked") or []):
                    result["manifest"]["new_tests"].append(path)
                elif path in (changes.get("deleted") or []):
                    result["manifest"]["deleted_tests"].append(path)
                else:
                    result["self_selectors"].add(path)  # 修改已有测试：建议运行自身
            elif renamed_entry is not None:
                result["fallbacks"].append(
                    ("infrastructure_change", "tests/ 下的基础设施文件被重命名：%s -> %s"
                     % (renamed_entry["from"], renamed_entry["to"])))
            elif path in (changes.get("added") or []) or path in (changes.get("untracked") or []):
                result["fallbacks"].append(
                    ("infrastructure_change", "tests/ 下新增了非测试文件：%s" % path))
            elif path in (changes.get("deleted") or []):
                result["fallbacks"].append(
                    ("infrastructure_change", "tests/ 下的文件被删除：%s" % path))
            elif path in INFRA_TEST_FILES:
                result["fallbacks"].append(
                    ("infrastructure_change", "测试基础设施变化：%s" % path))
            else:
                result["fallbacks"].append(
                    ("infrastructure_change", "tests/ 下的辅助文件变化：%s" % path))
            continue
        hits = _rule_hits(path, rules)
        if not hits:
            result["unknown_paths"].append(path)
            continue
        for rule in hits:
            if rule.fallback is not None:
                result["fallbacks"].append((rule.fallback, "规则 %s：%s（路径 %s）"
                                            % (rule.rid, rule.why, path)))
                continue
            entry = result["matched_rules"].setdefault(
                rule.rid, {"id": rule.rid, "why": rule.why, "paths": [],
                           "selectors": list(rule.selectors),
                           "scenarios": list(rule.scenarios), "serve_check": rule.serve_check})
            entry["paths"].append(path)
    if touched_tests and not (result["manifest"]["new_tests"] or result["manifest"]["deleted_tests"]
                              or result["manifest"]["renamed_tests"]):
        # tests/ 只有修改时清单检查通过；这里不做额外动作
        pass
    result["manifest"]["renamed_tests"] = sorted(
        {item["from"] + "->" + item["to"]: item for item in result["manifest"]["renamed_tests"]}.values(),
        key=lambda item: item["to"])
    return result


def _manifest_problems(repo: Path, strategy: dict, classified: dict, scenario_ids: Optional[tuple],
                       declared_scenarios: set) -> list:
    """测试清单完整性：映射声明的选择器与场景必须可解析，测试增删改名必须纳入映射。"""
    problems = []
    for rule in strategy["rules"]:
        for selector in rule.selectors:
            if not (repo / selector).is_file():
                problems.append("规则 %s 的选择器指向不存在的测试文件：%s" % (rule.rid, selector))
    if declared_scenarios:
        if scenario_ids is None:
            problems.append("规则引用了浏览器场景，但场景注册表不可用")
        else:
            known = set(scenario_ids)
            for scenario in sorted(declared_scenarios):
                if scenario != ALL_SCENARIOS and scenario not in known:
                    problems.append("规则引用了不存在的浏览器场景：%s" % scenario)
    if classified["manifest"]["new_tests"]:
        problems.append("新增或改名进来的测试未纳入任何映射规则：%s"
                        % "、".join(classified["manifest"]["new_tests"]))
    if classified["manifest"]["deleted_tests"]:
        problems.append("测试清单变化（删除）：%s" % "、".join(classified["manifest"]["deleted_tests"]))
    if classified["manifest"]["renamed_tests"]:
        problems.append("测试清单变化（重命名）：%s"
                        % "、".join("%s -> %s" % (item["from"], item["to"])
                                    for item in classified["manifest"]["renamed_tests"]))
    contract = "tests/test_i18n_catalog_contract.py"
    if any(rule.selectors for rule in strategy["rules"]) and not (repo / contract).is_file():
        problems.append("全局词典一致性契约模块缺失：%s" % contract)
    return problems


def _suggest_for_phase(phase: str, classified: dict, changes: dict, fallback: Optional[dict],
                       expanded_scenarios: list) -> dict:
    suggested = {"runs_tests": True, "groups": [], "selectors": [], "selectors_effective": [],
                 "contract_added": [], "scenarios": [], "serve_check": False,
                 "not_applicable": [], "notes": []}
    if phase == "issue":
        suggested["runs_tests"] = False
        suggested["notes"] = [
            "需求 / 分诊阶段不自动启动测试：写 issue、整理验收标准不需要运行产品测试。",
            "缺陷复现可以显式请求相关组：python3 verify.py run unit-related <选择器>。",
        ]
        return suggested
    if phase == "pr":
        suggested["groups"] = ["all"]
        suggested["notes"] = [
            "PR 收敛完成完整验收：完整单元 + 服务安全自检 + 完整浏览器。",
            "Python 3.9 / 3.12 双版本全量与服务安全自检由 CI 独立承担，本地结果不替代 CI。",
        ]
        return suggested
    if phase == "ci":
        suggested["groups"] = ["unit", "serve-check"]
        suggested["notes"] = [
            "CI 与现有矩阵一致：完整单元 + 服务安全自检；浏览器不在 CI 矩阵，由本地完整验收承担。",
        ]
        return suggested
    # development / review / manual
    if fallback is not None:
        # 回退建议取并集：完整单元之外，已命中规则的 serve-check 与场景计划保留在
        # 建议里（回退解释的是「为什么要完整检查」，不是「已命中的信息可以丢弃」）
        serve_check = False
        scenarios = set()
        for entry in classified["matched_rules"].values():
            scenarios.update(entry["scenarios"])
            serve_check = serve_check or entry["serve_check"]
        suggested["groups"] = ["unit"] + (["serve-check"] if serve_check else [])
        suggested["scenarios"] = (sorted(expanded_scenarios) if ALL_SCENARIOS in scenarios
                                  else sorted(scenarios))
        suggested["serve_check"] = serve_check
        suggested["notes"] = ["回退完整检查：%s" % fallback["reason"]]
        if serve_check or scenarios:
            suggested["notes"].append("回退时已命中规则的 serve-check 与场景计划保留在建议里。")
        return suggested
    selectors = set(classified["self_selectors"])
    serve_check = False
    scenarios = set()
    for entry in classified["matched_rules"].values():
        selectors.update(entry["selectors"])
        scenarios.update(entry["scenarios"])
        serve_check = serve_check or entry["serve_check"]
    if classified["doc_exempt"] and not selectors and not scenarios and not serve_check:
        suggested["not_applicable"] = ["unit", "unit-related", "serve-check", "browser"]
        suggested["notes"] = [
            "全部有效差异都在文档允许列表内：产品测试确认不适用（%s）。"
            % "、".join("%s（%s）" % (item["path"], item["category"]) for item in classified["doc_exempt"]),
            "影子模式下实际执行仍按本次请求的组进行，不因建议减少任何检查。",
        ]
        return suggested
    suggested["groups"] = ["unit-related"] + (["serve-check"] if serve_check else [])
    suggested["selectors"] = sorted(selectors)
    suggested["scenarios"] = (sorted(expanded_scenarios) if ALL_SCENARIOS in scenarios
                              else sorted(scenarios))
    suggested["serve_check"] = serve_check
    if scenarios:
        suggested["notes"] = ["浏览器场景为计划层对照：影子模式不因此增减实际执行的组。"]
    return suggested


def build_selection(snapshot: dict, baseline: dict, phase: str, repo: Path,
                    env: Optional[dict] = None) -> dict:
    """构建选测计划。snapshot 与 baseline 由调用方提供（与 run 记录同源）。"""
    selection = {"schema": SCHEMA, "strategy_version": None, "strategy_source": None,
                 "available": bool(snapshot.get("available")),
                 "reason": snapshot.get("reason"), "phase": phase,
                 "baseline": baseline, "changes": None, "fallback": None,
                 "matched_rules": [], "doc_exempt": [], "unknown_paths": [],
                 "manifest_check": {"new_tests": [], "deleted_tests": [], "renamed_tests": [],
                                    "problems": []},
                 "suggested": None}
    fallbacks = []
    if baseline.get("commit") is None:
        fallbacks.append(("bad_baseline",
                          baseline.get("error") or "基线缺失：无法确定有效差异的起点"))
    if not snapshot.get("available"):
        fallbacks.append(("input_unavailable", snapshot.get("reason") or "有效输入快照不可用"))
    strategy = load_strategy(repo, env)
    selection["strategy_source"] = strategy["source"]
    selection["strategy_version"] = strategy["version"]
    if strategy["override_used"]:
        selection.setdefault("notes", []).append(
            "使用 AE_SELECTION_MAP 覆盖映射（测试与实验用）；路径不进记录。")
    if strategy["problems"]:
        fallbacks.append(("broken_rules", "；".join(strategy["problems"])))
    if fallbacks and fallbacks[0][0] in ("bad_baseline", "input_unavailable", "broken_rules"):
        selection["fallback"] = {"kind": fallbacks[0][0],
                                 "reason": "；".join(reason for _, reason in fallbacks)}
        selection["suggested"] = _suggest_for_phase(phase, {"matched_rules": {}, "doc_exempt": [],
                                                            "self_selectors": set()}, {},
                                                    selection["fallback"], [])
        return selection
    changes = collect_changes(repo, baseline.get("commit"), snapshot)
    selection["changes"] = changes
    scenario_ids = None
    declared = set()
    for rule in strategy["rules"]:
        declared.update(rule.scenarios)
    if declared:
        scenario_ids, problem = load_scenario_ids(repo)
        if problem is not None:
            fallbacks.append(("incomplete_manifest", problem))
    classified = _classify(repo, changes, strategy, scenario_ids)
    selection["matched_rules"] = sorted(
        ({"id": entry["id"], "why": entry["why"], "paths": sorted(set(entry["paths"])),
          "selectors": sorted(set(entry["selectors"])), "scenarios": entry["scenarios"],
          "serve_check": entry["serve_check"]}
         for entry in classified["matched_rules"].values()),
        key=lambda entry: entry["id"])
    selection["doc_exempt"] = sorted(classified["doc_exempt"], key=lambda item: item["path"])
    selection["unknown_paths"] = classified["unknown_paths"]
    if classified["unknown_paths"]:
        fallbacks.append(("unknown_path",
                          "未分类路径不在任何映射规则或文档允许列表中：%s"
                          % "、".join(classified["unknown_paths"])))
    for kind, reason in classified["fallbacks"]:
        fallbacks.append((kind, reason))
    manifest_problems = _manifest_problems(repo, strategy, classified, scenario_ids, declared)
    selection["manifest_check"] = {
        "new_tests": sorted(classified["manifest"]["new_tests"]),
        "deleted_tests": sorted(classified["manifest"]["deleted_tests"]),
        "renamed_tests": classified["manifest"]["renamed_tests"],
        "problems": manifest_problems}
    if manifest_problems:
        fallbacks.append(("incomplete_manifest", "；".join(manifest_problems)))
    for kind in ("strategy_change", "infrastructure_change", "shared_resource", "guarded_doc"):
        reasons = [reason for name, reason in fallbacks if name == kind]
        if reasons:
            fallbacks = [item for item in fallbacks if item[0] != kind]
            fallbacks.append((kind, "；".join(reasons)))
    expanded = []
    if scenario_ids and ALL_SCENARIOS in declared:
        expanded = list(scenario_ids)
    if fallbacks:
        order = {kind: index for index, kind in enumerate(FALLBACK_KINDS)}
        fallbacks.sort(key=lambda item: order.get(item[0], len(order)))
        selection["fallback"] = {"kind": fallbacks[0][0],
                                 "reason": "；".join(reason for _, reason in fallbacks)}
    elif not _relevant_paths(changes):
        selection["fallback"] = {"kind": "no_changes",
                                 "reason": "相对基线没有可用差异；空差异不解释成不用测，保守按完整检查"}
    selection["suggested"] = _suggest_for_phase(phase, classified, changes,
                                                selection["fallback"], expanded)
    if phase == "pr" and documentation_only(selection):
        selection["suggested"].update(runs_tests=False, groups=[],
                                      not_applicable=["unit", "unit-related", "serve-check", "browser"],
                                      notes=[DOC_PR_REASON])
    return selection


def validate_builtin_map(repo_root: Path) -> list:
    """校验内置映射在真实仓库里的清单完整性（策略声明的外部行为，测试直接调用）。"""
    strategy = {"rules": SELECTION_RULES, "allowlist": DOC_ALLOWLIST}
    scenario_ids, problem = load_scenario_ids(Path(repo_root))
    declared = set()
    for rule in strategy["rules"]:
        declared.update(rule.scenarios)
    classified = {"manifest": {"new_tests": [], "deleted_tests": [], "renamed_tests": []}}
    return _manifest_problems(Path(repo_root), strategy, classified, scenario_ids, declared)
