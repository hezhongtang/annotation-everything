#!/usr/bin/env python3
"""验证证据复用策略层（issue #56，纯标准库，语法兼容 Python 3.9）。

verify.py 的复用策略：同一产物根（同一检出 / worktree）里，当前仍有效的通过
记录可以被后续阶段引用，跳过对应组并把引用写进记录与报告。判定从紧：任何一条
不满足就实际执行，绝不把失效证据冒充通过。

一个组被复用必须同时满足（复用判定矩阵）：
- 阶段允许：当前阶段与来源记录的阶段都在复用白名单（issue / development /
  review / manual）；pr / ci 是收敛与平台门禁，按口径必须实际执行，不引用。
- 组本身可复用：依赖同机宿主机状态的组（groups[].resources 非空，如完整单元、
  完整浏览器、命中敏感模块的相关单元）指纹相同也不复用，必须实际执行或要求
  隔离证明。
- 输入相同：来源记录的 input.fingerprint 与当前一致（指纹覆盖 HEAD 与 staged /
  unstaged / untracked 的完整内容），且来源记录确认覆盖最终输入；基线提交也须
  一致（显式换基线就是换口径）。唯一放宽：指纹与基线的差异被证明仅在文档允许
  列表内（与选测策略同一允许列表，覆盖映射下停用）时按「文档无关等价」处理，
  复用理由注明；无法证明独立性就回退重新验证。
- 环境相同：OS、Python、CI 标记与配置摘要在比对范围内一致。
- 范围覆盖：请求组的测试清单 ⊆ 来源组已验证的清单。serve-check 按组名；
  unit-related 按归一化选择器前缀（模块级），点名到类 / 用例的选择器是按疑点
  复测，不复用。
- 证据当前有效：与 report 同一套读取校验（schema、state、日志校验和）通过，
  且 result.usable_as_evidence 为真——不信任落盘时的旧标记。
- 未超期：来源记录结束时间在保鲜窗口（缺省 24 小时）内、不在未来（时间回退
  不可信）；时间不可解析同样拒用。
- 版本一致：入口版本、选测策略版本与复用策略版本都一致。没有复用策略版本的
  旧记录（统一入口 G 之前）不参与复用。

会话边界：只引用同一产物根、同一显式工作会话摘要的记录；无法确认会话身份时
实际执行。在此之上叠加 24 小时保鲜上限，不跨机器或检出复用。

本模块只读取记录做判定，不写任何文件、不启动任何子进程；`load_record` 与选择器
归一化由调用方注入，避免与 verify.py 循环导入。任何读取错误由调用方回退为
「全部实际执行」，不会把旧记录误判成功。
"""
from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Optional

try:
    import verify_selection
except Exception:  # pragma: no cover - 防御分支：选测层不可用时没有文档允许列表可依据
    verify_selection = None  # type: ignore[assignment]

SCHEMA = "ae-verify-reuse/1"
POLICY_VERSION = 2

# 本地证据的保鲜上限（小时）。这只是本地证据的新鲜度边界，不代表外部状态在
# 24 小时内稳定。
FRESHNESS_HOURS = 24.0

DECISION_REUSE = "reuse"
DECISION_RUN = "run"

# 允许复用的阶段；pr / ci 是收敛与平台门禁，必须实际执行。
REUSE_PHASES = ("issue", "development", "review", "manual")
GATE_PHASES = ("pr", "ci")

# 最多检查多少条候选记录（按 index 新到旧）；找到即停，控制读取开销。
MAX_CANDIDATES = 20
# 每个组保留多少条被拒候选的原因（解释「为什么没有复用」）。
MAX_ATTEMPTS = 5

# 环境比对范围（记录里已有的字段；任何一项缺失或不同都判不一致，从紧）。
ENVIRONMENT_KEYS = ("os", "python", "ci", "config_summary")


def environment_basis(environment: Optional[dict]) -> dict:
    """抽取参与比对的环境字段；比对范围之外的字段（CPU 数、时区、锁位置）不参与。"""
    environment = environment or {}
    python = environment.get("python") or {}
    ci = environment.get("ci") or {}
    return {
        "os": environment.get("os"),
        "python": {"version": python.get("version"),
                   "implementation": python.get("implementation")},
        "ci": {"is_ci": ci.get("is_ci")},
        "config_summary": environment.get("config_summary"),
    }


def environment_diff(left: dict, right: dict) -> list:
    """两份环境基线的差异键；空列表即环境一致。"""
    return [key for key in ENVIRONMENT_KEYS if left.get(key) != right.get(key)]


def parse_timestamp(text) -> Optional[datetime.datetime]:
    """解析记录里的 ISO 8601 时间；缺失、损坏或没有时区一律返回 None（不可信）。"""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def doc_exempt_path(path: str) -> bool:
    """路径是否在选测策略的狭窄文档允许列表内（与 verify_selection 同一来源）。

    verify.py 的输入快照用它排除文档路径，得到「非文档有效输入摘要」；本模块的
    文档无关等价判断用同一函数，保证两侧口径一致。选测层不可用时没有可依据的
    允许列表，一律不算豁免（从紧）。
    """
    if verify_selection is None:
        return False
    for entry in verify_selection.DOC_ALLOWLIST:
        if entry.prefix.endswith("/"):
            if path.startswith(entry.prefix):
                return True
        elif path == entry.prefix:
            return True
    return False


def _git_diff_paths(repo: Path, from_head: str, to_head: str) -> tuple:
    """两个 HEAD 之间的变更路径列表；返回 (0, 路径) 或 (非 0, 错误文本)。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", "-z", from_head, to_head],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    if proc.returncode != 0:
        return proc.returncode, proc.stdout.strip() or "git diff 失败"
    return 0, [item for item in proc.stdout.split("\0") if item]


# ── 候选收集 ───────────────────────────────────────────────────────────────

def _candidates(artifact_dir: Path, eligible_groups: list) -> list:
    """index.jsonl 里可能覆盖请求组的记录 run ID，新到旧。

    只做廉价预筛（state / 组相交）；结论与证据的真伪判定在读取记录之后——失败、
    中止等非通过记录也进入候选，让「为什么没有复用」带上具体原因。
    """
    index = artifact_dir / "index.jsonl"
    if not index.is_file():
        return []
    wanted = set(eligible_groups)
    ids = []
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("state") != "complete":
            continue
        if not wanted.intersection(entry.get("groups") or []):
            continue
        if isinstance(entry.get("run_id"), str):
            ids.append(entry["run_id"])
    return list(reversed(ids))  # 追加序 = 结束序；反转后新到旧


# ── 文档无关等价（验收：仅变化显式允许且已证明无关的文档时仍可复用） ─────────

def _inputs_test_equivalent(source: dict, record: dict, repo: Path) -> tuple:
    """指纹不同时判断两次输入是否「测试等价」：差异仅在文档允许列表内。

    依据两侧记录里的非文档有效输入摘要（core_digests，记录时按内置允许列表
    排除文档路径）与 HEAD 差异：
    - 任一侧选测映射不是内置版本（覆盖映射的允许列表没有落进记录）→ 无法证明；
    - HEAD 不同时，HEAD 差异必须只含允许列表内的文档路径；
    - 非文档输入摘要逐路径相等（某路径只在一侧出现即视为不同——它在该侧相对
      HEAD 有改动，而另一侧等于 HEAD 内容）。
    返回 (是否等价, 拒绝原因)。任何读不出来、解析不了的情况都判不等价（从紧）。
    """
    s_sel = ((source.get("context") or {}).get("selection") or {})
    c_sel = ((record.get("context") or {}).get("selection") or {})
    if s_sel.get("strategy_source") != "builtin" or c_sel.get("strategy_source") != "builtin":
        return False, "选测映射不是内置版本，无法证明文档无关"
    s_snap = (source.get("input") or {}).get("at_start") or {}
    c_snap = (record.get("input") or {}).get("at_start") or {}
    s_map = s_snap.get("core_digests")
    c_map = c_snap.get("core_digests")
    if not isinstance(s_map, dict) or not isinstance(c_map, dict):
        return False, "记录缺少非文档有效输入摘要，无法证明文档无关"
    if s_snap.get("core_scope") != c_snap.get("core_scope"):
        return False, "非文档有效输入摘要口径不一致，无法证明文档无关"
    s_head = s_snap.get("head")
    c_head = c_snap.get("head")
    if s_head != c_head:
        code, payload = _git_diff_paths(repo, s_head, c_head)
        if code != 0:
            return False, "HEAD 差异解析失败，无法证明文档无关"
        non_doc = sorted(item for item in payload if not doc_exempt_path(item))
        if non_doc:
            return False, "提交差异包含非文档路径（%s）" % "、".join(non_doc[:3])
    if s_map != c_map:
        only_one_side = sorted(set(s_map).symmetric_difference(c_map))
        changed = sorted(item for item in set(s_map) & set(c_map) if s_map[item] != c_map[item])
        detail = "、".join((only_one_side + changed)[:3]) or "内容摘要不同"
        return False, "非文档输入不同（%s）" % detail
    return True, ""


def _baselines_doc_equivalent(repo: Path, source_baseline: Optional[str],
                              current_baseline: Optional[str]) -> tuple:
    """两个基线提交的差异是否只在文档允许列表内（缺基线或解析失败一律不算）。"""
    if not source_baseline or not current_baseline:
        return False, "（基线缺失，无法证明文档无关）"
    code, payload = _git_diff_paths(repo, source_baseline, current_baseline)
    if code != 0:
        return False, "（基线差异解析失败，无法证明文档无关）"
    non_doc = sorted(item for item in payload if not doc_exempt_path(item))
    if non_doc:
        return False, "（基线差异包含非文档路径：%s）" % "、".join(non_doc[:3])
    return True, ""


# ── 来源记录的整体门槛 ─────────────────────────────────────────────────────

def _source_reject_reason(source: dict, record: dict, fingerprint: str, basis: dict,
                          current_strategy, current_wrapper,
                          baseline_commit: Optional[str], now: datetime.datetime,
                          repo: Path) -> tuple:
    """候选记录不满足整体门槛的原因。

    返回 (拒用原因或 None, 文档无关等价)。None 表示可以进入逐组覆盖判断；
    文档无关等价时复用理由要注明输入差异仅限文档允许列表。
    """
    run_id = source.get("run_id") or "?"
    result = source.get("result") or {}
    if result.get("usable_as_evidence") is not True:
        return "run %s 当前不能作为通过证据" % run_id, False
    session = (record.get("context") or {}).get("session") or {}
    source_session = (source.get("context") or {}).get("session") or {}
    if not session.get("id") or session.get("id") != source_session.get("id"):
        return "run %s 的逻辑工作会话不同或无法确认，实际执行" % run_id, False
    source_input = source.get("input") or {}
    doc_equivalent = False
    if source_input.get("fingerprint") != fingerprint:
        equivalent, why = _inputs_test_equivalent(source, record, repo)
        if not equivalent:
            return "run %s 输入指纹不同：%s" % (run_id, why or "有效输入已变化"), False
        doc_equivalent = True
    if source_input.get("covers_final_input") is not True:
        return "run %s 未确认覆盖最终输入（运行期间输入可能已变化）" % run_id, doc_equivalent
    diff = environment_diff(basis, environment_basis(source.get("environment")))
    if diff:
        return "run %s 环境不同（%s）" % (run_id, "、".join(diff)), doc_equivalent
    if source.get("wrapper_version") != current_wrapper:
        return "run %s 入口版本不同（来源 %s，当前 %s）" % (
            run_id, source.get("wrapper_version"), current_wrapper), doc_equivalent
    source_strategy = ((source.get("context") or {}).get("selection") or {}).get("strategy_version")
    if source_strategy != current_strategy:
        return "run %s 策略版本不同（来源 %s，当前 %s）" % (run_id, source_strategy, current_strategy), doc_equivalent
    if (source.get("reuse") or {}).get("policy_version") != POLICY_VERSION:
        return "run %s 复用策略版本不同（来源 %s，当前 %s）；统一入口 G 之前的记录不参与复用" % (
            run_id, (source.get("reuse") or {}).get("policy_version"), POLICY_VERSION), doc_equivalent
    source_phase = (source.get("context") or {}).get("phase")
    if source_phase not in REUSE_PHASES:
        return "run %s 的阶段是 %s，不在可引用阶段内（%s）" % (
            run_id, source_phase, "、".join(REUSE_PHASES)), doc_equivalent
    source_baseline = (source_input.get("baseline") or {}).get("commit")
    if source_baseline != baseline_commit:
        ok, why = _baselines_doc_equivalent(repo, source_baseline, baseline_commit)
        if not ok:
            return "run %s 基线不同（来源 %s，当前 %s）%s" % (
                run_id, source_baseline, baseline_commit, why), doc_equivalent
    ended = parse_timestamp(source.get("ended_at"))
    if ended is None:
        return "run %s 结束时间不可解析，时间不可信" % run_id, doc_equivalent
    age_hours = (now - ended).total_seconds() / 3600.0
    if age_hours < 0:
        return "run %s 结束时间在未来（%s），时间回退不可信" % (run_id, source.get("ended_at")), doc_equivalent
    if age_hours > FRESHNESS_HOURS:
        return "run %s 已超过 %.0f 小时保鲜窗口（结束于 %s）" % (
            run_id, FRESHNESS_HOURS, source.get("ended_at")), doc_equivalent
    if result.get("outcome") != "passed":
        return "run %s 整次结论是 %s，不是通过" % (run_id, result.get("outcome")), doc_equivalent
    return None, doc_equivalent


# ── 逐组覆盖判断 ───────────────────────────────────────────────────────────

def _selectors_covered(requested: list, source: dict, prefix_of) -> tuple:
    """请求的选择器清单是否被来源记录覆盖。返回 (是否覆盖, 拒绝原因)。"""
    run_id = source.get("run_id") or "?"
    if not requested:
        return False, "请求的选择器清单为空，无法证明覆盖"
    dotted = [item for item in requested if "." in (prefix_of(item) or "")]
    if dotted:
        return False, ("请求显式点名到类 / 用例级（%s）：按疑点复测，不复用"
                       % "、".join(dotted))
    source_selectors = ((source.get("context") or {}).get("selectors_effective")) or []
    if not source_selectors:
        return False, "run %s 没有可核对的选择器清单" % run_id
    source_prefixes = [prefix_of(item) for item in source_selectors]
    for item in requested:
        req = prefix_of(item)
        if not any(req == src or req.startswith(src + ".")
                   for src in source_prefixes if src):
            return False, ("run %s 的验证范围不覆盖请求（%s 不在已验证清单 %s 内）"
                           % (run_id, item, "、".join(source_selectors)))
    return True, None


def _decide_group(name: str, meta: dict, sources: list, general_attempts: list,
                  prefix_of, candidate_order: dict) -> dict:
    """为一个可复用类组挑选最新的合格来源；找不到则给出从紧原因。"""
    attempts = list(general_attempts)
    requested = meta.get("selectors_effective") or []
    counter = meta.get("counter")
    for run_id, source, age_hours, doc_equivalent in sources:
        group = next((item for item in source.get("groups") or []
                      if item.get("name") == name), None)
        if group is None:
            attempts.append((run_id, "run %s 没有执行 %s 组" % (run_id, name)))
            continue
        if group.get("outcome") != "passed":
            attempts.append((run_id, "run %s 的 %s 组结论是 %s，不是实际通过的执行"
                             % (run_id, name, group.get("outcome"))))
            continue
        if counter == "unittest":
            covered, why = _selectors_covered(requested, source, prefix_of)
            if not covered:
                attempts.append((run_id, why))
                continue
        input_note = ("同输入（差异仅在文档允许列表内，已证明与产品测试无关）"
                      if doc_equivalent else "同输入")
        return {
            "group": name,
            "decision": DECISION_REUSE,
            "reason": "%s、同环境、范围覆盖，且来源证据当前校验有效（%.1f 小时前结束）"
                      % (input_note, age_hours),
            "source_run_id": run_id,
            "source_ended_at": source.get("ended_at"),
            "source_fingerprint": (source.get("input") or {}).get("fingerprint"),
            "source_phase": (source.get("context") or {}).get("phase"),
            "source_scope": {"group": name,
                             "selectors": ((source.get("context") or {}).get("selectors_effective") or [])
                             if counter == "unittest" else []},
            "source_environment": environment_basis(source.get("environment")),
            "age_hours": round(age_hours, 6),
            "attempts": _trim_attempts(attempts, candidate_order),
        }
    trimmed = _trim_attempts(attempts, candidate_order)
    reason = trimmed[0]["reason"] if trimmed else "没有可引用的已有记录"
    return {"group": name, "decision": DECISION_RUN, "reason": reason,
            "attempts": trimmed}


def _trim_attempts(attempts: list, candidate_order: dict) -> list:
    # 整体拒因与组级拒因分开收集，合并后仍按候选的新旧顺序呈现。
    attempts = sorted(attempts, key=lambda item: candidate_order.get(item[0], MAX_CANDIDATES))
    return [{"run_id": run_id, "reason": reason}
            for run_id, reason in attempts[:MAX_ATTEMPTS]]


def reference_reject_reason(source: dict, record: dict, group: dict, repo: Path, prefix_of) -> Optional[str]:
    """报告与规划共用有效性、会话和覆盖判断；保鲜时间按本次读取计算。"""
    context = record.get("context") or {}
    if context.get("phase") not in REUSE_PHASES:
        return "引用记录的阶段不允许复用"
    now = datetime.datetime.now(datetime.timezone.utc)
    reason, doc_equivalent = _source_reject_reason(
        source, record, (record.get("input") or {}).get("fingerprint"),
        environment_basis(record.get("environment")),
        (context.get("selection") or {}).get("strategy_version"), record.get("wrapper_version"),
        ((record.get("input") or {}).get("baseline") or {}).get("commit"), now, repo)
    if reason:
        return reason
    if group.get("resources"):
        return "依赖宿主机状态的组不能引用已有验证"
    run_id = source.get("run_id")
    decision = _decide_group(
        group.get("name"), {"counter": group.get("counter"),
                            "selectors_effective": context.get("selectors_effective") or []},
        [(run_id, source, 0.0, doc_equivalent)], [], prefix_of, {run_id: 0})
    return decision["reason"] if decision["decision"] != DECISION_REUSE else None


def _run_decision(name: str, reason: str) -> dict:
    return {"group": name, "decision": DECISION_RUN, "reason": reason, "attempts": []}


# ── 复用规划 ───────────────────────────────────────────────────────────────

def build_reuse_plan(artifact_dir: Path, record: dict, requested: list, group_meta: dict,
                     prefix_of, load_record, repo: Path,
                     bypass_reason: Optional[str] = None,
                     now: Optional[datetime.datetime] = None) -> dict:
    """决定请求的每个组「引用已有验证」还是「实际执行」。

    artifact_dir：本次运行的产物根目录（候选记录只来自这里，即同一检出的会话）。
    record：本次运行的记录（至少含 context.phase、input、environment、wrapper_version）。
    group_meta：组名 -> {resources, counter, selectors_effective}。
    prefix_of：选择器 -> 模块名前缀的归一化函数（verify.py 注入，单一实现）。
    load_record：记录读取与证据校验函数（verify.py 注入，返回 (记录或 None, 问题)）。
    repo：仓库根（文档无关等价判断需要解析 HEAD 差异）。
    bypass_reason：非 None 时整体停用（显式 --no-reuse），原因写进记录。

    返回 schema 为 ae-verify-reuse/1 的规划；decisions 与请求组一一对应。
    """
    context = record.get("context") or {}
    phase = context.get("phase")
    plan = {
        "schema": SCHEMA,
        "policy_version": POLICY_VERSION,
        "phase": phase,
        "enabled": False,
        "reason": None,
        "freshness_hours": FRESHNESS_HOURS,
        "session_scope": "候选记录只来自同一产物根，且逻辑工作会话摘要一致；无身份时实际执行",
        "environment_scope": list(ENVIRONMENT_KEYS),
        "decisions": [],
    }

    disabled = bypass_reason
    if disabled is None and phase in GATE_PHASES:
        disabled = "阶段 %s 是收敛 / 平台门禁：按口径必须实际执行，不引用已有证据" % phase
    if disabled is None and phase not in REUSE_PHASES:
        disabled = "阶段 %s 不在复用白名单（%s），保守实际执行" % (phase, "、".join(REUSE_PHASES))
    fingerprint = (record.get("input") or {}).get("fingerprint")
    if disabled is None and not fingerprint:
        disabled = "当前输入指纹不可用，无法匹配同输入证据"
    if disabled is None and not (context.get("session") or {}).get("id"):
        disabled = "无法确认逻辑工作会话身份，保守实际执行"
    eligible = [name for name in requested if not (group_meta.get(name) or {}).get("resources")]
    if disabled is None and not eligible:
        disabled = "请求的组都依赖同机宿主机状态（resources 非空），没有可复用的组"
    if disabled is not None:
        plan["reason"] = disabled
        plan["decisions"] = [_run_decision(name, disabled) for name in requested]
        return plan

    plan["enabled"] = True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    basis = environment_basis(record.get("environment"))
    current_strategy = (context.get("selection") or {}).get("strategy_version")
    current_wrapper = record.get("wrapper_version")
    baseline_commit = ((record.get("input") or {}).get("baseline") or {}).get("commit")

    attempts = []  # (run_id, 原因)：整体门槛拒掉的候选，新到旧
    sources = []   # (run_id, 记录, 年龄小时)：通过整体门槛的候选，新到旧
    candidates = _candidates(artifact_dir, eligible)[:MAX_CANDIDATES]
    candidate_order = {run_id: index for index, run_id in enumerate(candidates)}
    for run_id in candidates:
        source, problems = load_record(artifact_dir / "runs" / run_id)
        if source is None or problems:
            attempts.append((run_id, "证据当前校验不可用：%s"
                             % ("；".join(problems) or "读不出来")))
            continue
        ended = parse_timestamp(source.get("ended_at"))
        age_hours = ((now - ended).total_seconds() / 3600.0) if ended is not None else None
        reason, doc_equivalent = _source_reject_reason(
            source, record, fingerprint, basis, current_strategy,
            current_wrapper, baseline_commit, now, repo)
        if reason is not None:
            attempts.append((run_id, reason))
            continue
        sources.append((run_id, source, age_hours or 0.0, doc_equivalent))

    for name in requested:
        meta = group_meta.get(name) or {}
        if meta.get("resources"):
            plan["decisions"].append(_run_decision(
                name, "组依赖同机宿主机状态（资源：%s），指纹相同也不可复用，"
                      "必须实际执行或要求隔离证明" % "、".join(meta["resources"])))
            continue
        plan["decisions"].append(_decide_group(name, meta, sources, attempts, prefix_of, candidate_order))
    return plan
