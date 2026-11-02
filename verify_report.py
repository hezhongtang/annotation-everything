"""验证效率历史投影。只读验证证据，不执行测试；规则修改须经 PR 审查。"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "ae-verify-efficiency/1"
RULE_VERSION = 2
MAX_HISTORY = 200
MAX_REVIEWS = 12
FRESHNESS_HOURS = 24
REGRESSION_SAMPLES = 3
REGRESSION_RATIO = 1.2
REGRESSION_SECONDS = 10

_ABSOLUTE_PATH = re.compile(
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>，；：,;\]\[{}()]+)"
    r"|(?<![A-Za-z0-9_:/])(?:[A-Za-z]:[\\/]|/)[^\s\"'<>，；：,;\]\[{}()]+")


def redact_paths(value, roots=()):
    """投影只保存来源别名与根内定位；异常中的其他绝对路径也不落盘。"""
    replacements = []
    primary = Path(roots[0]).absolute() if roots else None
    for root in roots:
        absolute = Path(root).absolute()
        try:
            relative = absolute.relative_to(primary)
            alias = "local" + ("/" + relative.as_posix() if relative.parts else "")
        except ValueError:
            alias = "source-" + digest(str(absolute))[:12]
        replacements.append((str(absolute), alias))
    replacements.sort(key=lambda item: -len(item[0]))

    def text_value(text):
        for path, alias in replacements:
            text = text.replace(path, alias)
        return _ABSOLUTE_PATH.sub(lambda match: match.group("url") or "<external-path>", text)

    def visit(item):
        if isinstance(item, str):
            return text_value(item)
        if isinstance(item, dict):
            return {text_value(key) if isinstance(key, str) else key: visit(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [visit(child) for child in item]
        return item
    return visit(value)


def error_label(exc):
    return type(exc).__name__ if isinstance(exc, OSError) else redact_paths(str(exc))


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def number(value, default=0.0):
    try:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else default
    except OverflowError:
        return default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def union_seconds(intervals):
    end = None
    total = 0.0
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
        elif stop > end:
            total += stop - end
        end = max(end or stop, stop)
    return round(total, 6)


def _history(roots, loader, now):
    entries, gaps, seen = [], [], {}
    truncated = 0
    for root in roots:
        root = Path(root)
        paths = set()
        try:
            paths.update(path for path in (root / "runs").iterdir() if path.is_dir())
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError) or not root.is_dir():
                gaps.append({"source": str(root), "reason": "运行历史不可读取：%s" % type(exc).__name__})
        index = root / "index.jsonl"
        if index.exists():
            try:
                for line in index.read_text(encoding="utf-8").splitlines():
                    try:
                        rid = json.loads(line).get("run_id")
                        if isinstance(rid, str) and rid and Path(rid).name == rid:
                            paths.add(root / "runs" / rid)
                        else:
                            raise ValueError("run ID")
                    except (ValueError, AttributeError):
                        gaps.append({"source": str(index), "reason": "索引事件损坏"})
            except (OSError, UnicodeError) as exc:
                gaps.append({"source": str(index), "reason": "索引不可读取：%s" % type(exc).__name__})
        ordered = []
        for path in paths:
            try:
                ordered.append((path.stat().st_mtime, path))
            except OSError:
                gaps.append({"source": str(path), "reason": "索引指向的运行目录缺失"})
        truncated += max(0, len(ordered) - MAX_HISTORY)
        for _, path in sorted(ordered, key=lambda item: (item[0], str(item[1])))[-MAX_HISTORY:]:
            try:
                record, problems = loader(path)
                if record is None:
                    raise ValueError("；".join(problems))
                identity = record.get("run_id")
                if not identity or identity != path.name:
                    raise ValueError("run ID 与目录不匹配")
                raw_digest = hashlib.sha256((path / "record.json").read_bytes()).hexdigest()
                if identity in seen:
                    if seen[identity] != raw_digest:
                        gaps.append({"source": str(path), "reason": "相同 run ID 的内容冲突"})
                    continue
                seen[identity] = raw_digest
                if (not isinstance(record.get("groups"), list) or not record["groups"]
                        or not all(isinstance(record.get(key), dict) for key in ("environment", "context", "input", "result", "timing"))):
                    raise ValueError("记录缺少分组或环境")
                for group in record["groups"]:
                    if not isinstance(group, dict) or not isinstance(group.get("name"), str):
                        raise ValueError("分组结构损坏")
                    for key in ("cases", "scenarios"):
                        members = group.get(key)
                        if members is not None and (not isinstance(members, list) or not all(
                                isinstance(item, dict) and isinstance(item.get("id"), str) for item in members)):
                            raise ValueError("分组 %s 结构损坏" % key)
                    for key in ("timing", "counts", "lock"):
                        if group.get(key) is not None and not isinstance(group[key], dict):
                            raise ValueError("分组 %s 结构损坏" % key)
                ended = instant(record.get("ended_at"))
                historical = ended is None or ended > now or now - ended > FRESHNESS_HOURS * 3600
                if historical:
                    gaps.append({"source": str(path), "reason": "记录超出24小时保鲜窗口或结束时间不可确认；仅作历史"})
                for problem in problems:
                    gaps.append({"source": str(path), "reason": problem})
                entries.append({"record": record, "run_id": identity, "record_path": str(path / "record.json"),
                                "content_digest": raw_digest,
                                "problems": problems, "historical_only": historical})
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                gaps.append({"source": str(path), "reason": "证据不可用：%s" % error_label(exc)})
    entries.sort(key=lambda item: (instant(item["record"].get("ended_at")) or 0, item["run_id"]))
    truncated += max(0, len(entries) - MAX_HISTORY)
    return entries[-MAX_HISTORY:], gaps, truncated


def _comparison(record, group):
    env = record.get("environment") or {}
    selection = (record.get("context") or {}).get("selection") or {}
    scope = {"group": group.get("name"),
             "cases": sorted(str(case.get("id")) for case in group.get("cases") or []),
             "scenarios": sorted(str(case.get("id")) for case in group.get("scenarios") or []),
             "selectors": (record.get("context") or {}).get("selectors_effective") or [],
             "checks": (group.get("counts") or {}).get("total")}
    dimensions = {"os": env.get("os"), "python": env.get("python"), "ci": env.get("ci"),
                  "browser": env.get("browser") or group.get("browser_environment"),
                  "dependencies": env.get("dependencies"), "config": env.get("config_summary"),
                  "isolation": env.get("isolation"), "strategy_version": selection.get("strategy_version"),
                  "wrapper_version": record.get("wrapper_version"), "scope": scope}
    missing = []
    if not env.get("os") or not env.get("python") or not env.get("isolation"):
        missing.append("环境或隔离信息不完整")
    if selection.get("strategy_version") is None:
        missing.append("缺少策略版本")
    if group.get("name") == "browser":
        browser = dimensions["browser"]
        if (not isinstance(browser, dict) or browser.get("schema") != "ae-browser-environment/1"
                or not all(isinstance(browser.get(key), str) and browser[key]
                           for key in ("browser", "channel", "version", "playwright"))):
            missing.append("浏览器或测试依赖实测版本未知")
    if not scope["cases"] and not scope["scenarios"] and scope["checks"] is None:
        missing.append("测试集合不可确认")
    return digest(dimensions)[:16], dimensions, missing


def _candidate(kind, key, evidence, scope, risk, acceptance, priority=1):
    return {"id": "%s:%s" % (kind, digest(key)[:16]), "kind": kind, "priority": priority,
            "evidence": evidence, "scope": scope, "quality_risk": risk, "suggested_acceptance": acceptance}


def build_summary(roots, loader, now=None, events=None):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    entries, gaps, truncated = _history(roots, loader, now)
    metrics = {"runs": len(entries), "execution_seconds": 0.0, "execution_union_seconds": 0.0,
               "verification_interval_union_seconds": 0.0, "queue_seconds": 0.0,
               "lock_wait_seconds": 0.0, "cleanup_seconds": 0.0, "evidence_references": 0,
               "actual_duplicate_executions": 0, "first_related_feedback_seconds": [],
               "outcomes": {}, "skipped_cases": 0, "interval_precision": "estimated",
               "interval_clipped_runs": []}
    buckets, intervals, verification_intervals, slow, candidates = {}, [], [], [], []
    repeats, attempts = defaultdict(list), defaultdict(list)
    outcomes = Counter()
    for entry in entries:
        record = entry["record"]
        timing = record.get("timing") or {}
        result = record.get("result") or {}
        outcomes[result.get("outcome") or "incomplete"] += 1
        for key in ("queue_seconds", "lock_wait_seconds", "cleanup_seconds"):
            metrics[key] += number(timing.get(key))
        queued = instant(timing.get("queued_at") or record.get("started_at"))
        ended = instant(timing.get("ended_at") or record.get("ended_at"))
        window = None
        if queued is not None:
            # 旧秒级日历戳会截断亚秒执行。至少覆盖已记录的排队 + 单调墙钟时长。
            duration = number(timing.get("queue_seconds")) + number(timing.get("wall_seconds"))
            stop = max(ended or queued, queued + duration)
            if stop > queued:
                window = (queued, stop)
                verification_intervals.append(window)
        first_group = None
        for group in record["groups"]:
            if group.get("outcome") == "reused":
                metrics["evidence_references"] += 1
                continue
            seconds = number((group.get("timing") or {}).get("child_seconds"), default=None)
            started = instant(group.get("started_at"))
            if started is not None and seconds:
                if window is not None:
                    interval = (max(started, window[0]), min(started + seconds, window[1]))
                    if interval[1] > interval[0]:
                        intervals.append(interval)
                    if interval != (started, started + seconds) and entry["run_id"] not in metrics["interval_clipped_runs"]:
                        metrics["interval_clipped_runs"].append(entry["run_id"])
                if first_group is None:
                    first_group = group
            metrics["execution_seconds"] += seconds if seconds is not None else 0
            metrics["skipped_cases"] += int(number((group.get("counts") or {}).get("skipped")))
            key, dimensions, missing = _comparison(record, group)
            bucket = buckets.setdefault(key, {"id": key, "dimensions": dimensions,
                      "comparable": not missing, "incomparable_reasons": missing, "samples": []})
            valid = (not entry["problems"] and record.get("state") == "complete"
                     and (record.get("input") or {}).get("covers_final_input") is True
                     and seconds is not None and started is not None)
            sample = {"run_id": entry["run_id"], "record_path": entry["record_path"],
                      "seconds": seconds, "outcome": group.get("outcome"),
                      "valid": valid, "historical_only": entry["historical_only"]}
            bucket["samples"].append(sample)
            fingerprint = (record.get("input") or {}).get("fingerprint")
            if valid and fingerprint and started is not None:
                attempts[(fingerprint, key)].append(sample)
                if group.get("outcome") == "passed":
                    repeats[(fingerprint, key)].append(sample)
            for case in ((group.get("cases") or []) + (group.get("scenarios") or [])) if valid else []:
                if case.get("seconds") is not None:
                    slow.append({"id": case.get("id"), "seconds": number(case.get("seconds")),
                                 "run_id": entry["run_id"], "group": group.get("name"),
                                 "comparison_group": key, "record_path": entry["record_path"]})
        if first_group is not None and (record.get("context") or {}).get("phase") == "development":
            measured = number(timing.get("first_feedback_seconds"), default=None)
            if measured is not None:
                feedback, precision = measured, "monotonic"
            else:
                # 旧秒级时间戳不能与高精度 child_seconds 相减；按已记录的时长估算。
                feedback = (number(timing.get("queue_seconds"))
                            + number((first_group.get("lock") or {}).get("wait_seconds"))
                            + number((first_group.get("timing") or {}).get("child_seconds")))
                if timing.get("wall_seconds") is not None:
                    feedback = min(feedback, number(timing.get("queue_seconds")) + number(timing["wall_seconds"]))
                precision = "estimated"
            metrics["first_related_feedback_seconds"].append({"run_id": entry["run_id"],
                     "seconds": round(feedback, 6), "granularity": "first-completed-group", "precision": precision})
        shadow = record.get("shadow") or {}
        if not entry["problems"] and shadow.get("comparable"):
            for omission in shadow.get("candidate_omissions") or []:
                candidates.append(_candidate("selection-omission-candidate", omission,
                    [entry["record_path"]], omission.get("target") or "未确定范围",
                    "尚未排除环境和不同范围；不得宣称已确认漏选",
                    "复现相同输入/环境的完整失败，人工确认前执行完整门禁", 3))
    for key, samples in repeats.items():
        if len(samples) > 1:
            metrics["actual_duplicate_executions"] += len(samples) - 1
            candidates.append(_candidate("repeated-execution", key, [s["record_path"] for s in samples],
                "同输入、同环境、同集合的成功执行", "可能是必要门禁、宿主状态或独立疑点；不自动消除",
                "核对阶段、资源和疑点；仅引用满足既有复用策略的证据"))
    for key, samples in attempts.items():
        for index, sample in enumerate(samples):
            if sample["outcome"] in ("failed", "error", "timeout", "interrupted") and any(
                    item["outcome"] == "passed" for item in samples[index + 1:]):
                candidates.append(_candidate("flaky-failure-candidate", (key, sample["run_id"]),
                    [s["record_path"] for s in samples[index:]], "同输入/环境/集合的失败后成功",
                    "重试不能覆盖原始失败；尚未确认是偶发失败", "保留首败并定位环境、时序或状态原因", 2))
                break
    groups = list(buckets.values())
    for bucket in groups:
        valid_samples = [s for s in bucket["samples"] if s["valid"] and s["outcome"] == "passed"]
        values = [s["seconds"] for s in valid_samples]
        bucket["sample_count"] = len(values)
        bucket["raw_seconds"] = values
        bucket["median_seconds"] = statistics.median(values) if values else None
        bucket["trend_note"] = "仅报告原始值、样本数和中位数；不推断百分位趋势"
        if bucket["comparable"] and len(values) >= REGRESSION_SAMPLES * 2:
            baseline = statistics.median(values[:REGRESSION_SAMPLES])
            recent = bucket["samples"][-REGRESSION_SAMPLES:]
            if all(s["valid"] and s["outcome"] == "passed" and s["seconds"] >= baseline * REGRESSION_RATIO
                   and s["seconds"] >= baseline + REGRESSION_SECONDS for s in recent):
                candidates.append(_candidate("performance-regression", bucket["id"],
                    [s["record_path"] for s in valid_samples[:REGRESSION_SAMPLES] + recent],
                    bucket["dimensions"]["scope"], "报告线索，不是新增 CI 失败门槛；需排除负载差异",
                    "同环境同集合3个样本复核，保留覆盖与断言", 2))
    metrics["execution_union_seconds"] = union_seconds(intervals)
    metrics["verification_interval_union_seconds"] = union_seconds(verification_intervals)
    metrics["outcomes"] = dict(sorted(outcomes.items()))
    for key, value in list(metrics.items()):
        if isinstance(value, float):
            metrics[key] = round(value, 6)
    if not entries:
        gaps.append({"source": "history", "reason": "没有可读样本，不能推断零失败或零遗漏"})
    event_data = events or {"schema": "ae-verify-events/1", "merged_prs": [], "gaps": []}
    gaps.extend(event_data.get("gaps") or [])
    for finding in event_data.get("selection_findings") or []:
        if isinstance(finding, dict) and finding.get("status") in ("confirmed", "uncertain"):
            candidates.append(_candidate("confirmed-selection-omission" if finding["status"] == "confirmed" else "uncertain-selection-impact",
                finding, finding.get("evidence") or [], finding.get("reason") or "影响待界定",
                "确认事件来自维护者；执行策略保守扩大，缩小范围仍须审查",
                "补充失败复现与映射回归，经 PR 审查后递增策略版本", 4))
    if slow:
        slowest = max(slow, key=lambda item: item["seconds"])
        if slowest["seconds"] >= 10:
            candidates.append(_candidate("slow-item", (slowest["comparison_group"], slowest["id"]),
                [slowest["record_path"]], slowest["id"], "单次慢项只提供定位线索，不证明回退",
                "先固定环境与集合采样，保留信号、时序和安全断言"))
    unique = {candidate["id"]: candidate for candidate in candidates}
    chosen = sorted(unique.values(), key=lambda item: (-item["priority"], item["id"]))[:3]
    return redact_paths({"schema": SCHEMA, "rule_version": RULE_VERSION,
            "generated_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            "status": "gaps" if gaps else "observed", "metrics": metrics,
            "comparison_groups": groups, "comparison_note": "跨环境、跨测试/场景集合不可直接比较；缺失版本不推断性能收益",
            "slow_items": sorted(slow, key=lambda item: -item["seconds"])[:10],
            "candidates": chosen, "gaps": gaps[:MAX_HISTORY], "gap_count": len(gaps),
            "history": [{k: v for k, v in entry.items() if k != "record"} for entry in entries],
            "retention": {"max_runs": MAX_HISTORY, "omitted_runs": truncated,
                          "raw_evidence": "原始证据不改写；投影有界，旧构件按原保留策略管理"},
            "merged_prs": list(dict.fromkeys(str(item) for item in event_data.get("merged_prs") or [])),
            "source_artifacts": event_data.get("source_artifacts") or [],
            "review": {"generated": False, "reason": "只读报告；--rebuild 更新归档与周期状态"}}, roots)


def read_events(path):
    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("schema") != "ae-verify-events/1"
                or not isinstance(payload.get("merged_prs"), list)
                or not all(isinstance(item, (int, str)) and not isinstance(item, bool)
                           for item in payload["merged_prs"])
                or not isinstance(payload.get("gaps", []), list)
                or not all(isinstance(item, dict) and isinstance(item.get("reason"), str)
                           for item in payload.get("gaps", []))):
            raise ValueError("事件清单 schema 或字段不兼容")
        if not isinstance(payload.get("selection_findings", []), list):
            payload["selection_findings"] = [None]
            payload.setdefault("gaps", []).append({"source": str(path), "reason": "影响确认事件结构不兼容"})
        if not isinstance(payload.get("sources", []), list):
            payload["sources"] = []
            payload.setdefault("gaps", []).append({"source": str(path), "reason": "CI记录根清单结构不兼容"})
        return payload
    except (OSError, ValueError) as exc:
        return {"schema": "ae-verify-events/1", "merged_prs": [],
                "gaps": [{"source": str(path), "reason": "远端事件清单不可用：%s" % error_label(exc)}]}


def sources(root, extra, events):
    roots = [Path(root)] + [Path(path) for path in extra]
    for source in (events or {}).get("sources") or []:
        if (isinstance(source, str) and source and not Path(source).is_absolute()
                and ".." not in Path(source).parts and "\\" not in source):
            roots.append(Path(root) / source)
        else:
            events.setdefault("gaps", []).append({"source": "sources", "reason": "CI 记录根路径不合法"})
    return list(dict.fromkeys(roots))


def _version(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_guard(guard):
    return (isinstance(guard, dict) and guard.get("schema") == "ae-verify-policy-guard/1"
            and guard.get("active") is True and _version(guard.get("strategy_version"))
            and isinstance(guard.get("reason"), str) and bool(guard["reason"])
            and isinstance(guard.get("evidence", []), list)
            and all(isinstance(item, str) for item in guard.get("evidence", [])))


def _merge_guard(directory, state, findings, roots):
    """已有守卫和确认事件取最高策略版本；乱序、重放或缺损均不能降级。"""
    try:
        import verify_selection
        current = verify_selection.STRATEGY_VERSION
        if not _version(current):
            raise ValueError("策略版本未知")
    except Exception:
        current = 2 ** 31 - 1  # 版本无法确认时保持守卫，待恢复可信策略后人工处理。
    candidates, problems = [], []
    path = directory / "selection-guard.json"

    def uncertain(reason, version=None):
        candidates.append({"schema": "ae-verify-policy-guard/1", "active": True,
                           "strategy_version": version if _version(version) else current,
                           "reason": reason, "evidence": []})

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not _valid_guard(existing):
                raise ValueError("守卫字段不完整")
            candidates.append(existing)
        except (OSError, ValueError) as exc:
            uncertain("既有策略守卫不可确认，保守扩大完整检查：%s" % error_label(exc))
    prior = state.get("guard_strategy_version")
    if state.get("guard_required") and (_version(prior) and prior > max(
            [item["strategy_version"] for item in candidates] or [-1]) or not _version(prior)):
        uncertain("状态要求的策略守卫缺失或版本倒退，保守扩大完整检查", prior)
    if not isinstance(findings, list):
        findings = [None]
    for finding in findings:
        if (isinstance(finding, dict) and finding.get("status") in ("confirmed", "uncertain")
                and _version(finding.get("strategy_version")) and isinstance(finding.get("reason"), str)
                and finding["reason"] and isinstance(finding.get("evidence"), list)
                and finding["evidence"] and all(isinstance(item, str) for item in finding["evidence"])):
            candidates.append({"schema": "ae-verify-policy-guard/1", "active": True,
                               "strategy_version": finding["strategy_version"],
                               "reason": finding["reason"], "evidence": finding["evidence"]})
        else:
            uncertain("影响确认事件不完整，无法界定影响，保守扩大完整检查")
            problems.append({"source": "selection_findings", "reason": "影响确认事件不完整；已保守扩大范围"})
    if not candidates:
        return None, False, problems
    highest = max(item["strategy_version"] for item in candidates)
    applicable = [redact_paths(item, roots) for item in candidates if item["strategy_version"] == highest]
    reasons = sorted({reason for item in applicable for reason in
                      (item.get("reasons") if isinstance(item.get("reasons"), list) else [item["reason"]])
                      if isinstance(reason, str)})[:MAX_HISTORY]
    evidence = sorted({source for item in applicable for source in item.get("evidence", [])})[:MAX_HISTORY]
    guard = {"schema": "ae-verify-policy-guard/1", "active": True, "strategy_version": highest,
             "reason": "；".join(reasons), "reasons": reasons, "evidence": evidence}
    _write(path, json.dumps(guard, ensure_ascii=False, indent=2))
    return highest, True, problems


def selection_guard(selection, root):
    """守卫只扩大范围；损坏从紧，旧策略守卫由受审查的新版本替代。"""
    path = Path(root) / "efficiency" / "selection-guard.json"
    try:
        path.stat()
        missing = False
    except FileNotFoundError:
        missing = True
    except OSError:
        missing = False  # 无法读取不能被 exists() 的 False 误解为不存在。
    if missing:
        state_path = Path(root) / "efficiency" / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            prior = state.get("guard_strategy_version")
            current = selection.get("strategy_version")
            if (not state.get("guard_required") or
                    _version(prior) and _version(current) and prior < current):
                return selection
        except (OSError, ValueError, AttributeError):
            pass
        reason = "效率策略守卫缺失或状态无法确认，保守扩大完整检查"
        selection["policy_guard"] = {"active": True, "reason": reason, "rule_version": RULE_VERSION}
        selection["fallback"] = {"kind": "efficiency_guard", "reason": reason}
        selection["suggested"] = {"runs_tests": True, "groups": ["all"], "selectors": [],
                                  "scenarios": ["*"], "serve_check": True, "notes": [reason]}
        return selection
    try:
        guard = json.loads(path.read_text(encoding="utf-8"))
        if not _valid_guard(guard):
            raise ValueError("守卫字段不完整")
        current = selection.get("strategy_version")
        if isinstance(current, int) and guard["strategy_version"] < current:
            selection["policy_guard"] = {"active": False, "reason": "守卫属于旧策略版本；当前受版本控制策略已更新"}
            return selection
        reason = redact_paths(guard["reason"], [root])
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        reason = "效率策略守卫不可确认，保守扩大完整检查：%s" % error_label(exc)
    selection["policy_guard"] = {"active": True, "reason": reason, "rule_version": RULE_VERSION}
    selection["fallback"] = {"kind": "efficiency_guard", "reason": reason}
    selection["suggested"] = {"runs_tests": True, "groups": ["all"], "selectors": [],
                              "selectors_effective": [], "scenarios": ["*"], "serve_check": True,
                              "notes": ["效率守卫扩大范围并停用证据复用；缩小范围需受审查的策略版本变更"]}
    return selection


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".efficiency-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _new_merge_events(summary, state, events):
    """有限窗口外只以 merged_at 晚于上次回顾证明新合并，不用编号推断时序。"""
    known = list(dict.fromkeys(state.get("reviewed_prs") or []))
    truncated = state.get("pr_history_truncated", len(known) >= MAX_HISTORY)
    clock, previous = instant(summary["generated_at"]), instant(state.get("reviewed_at"))
    times, invalid, gaps = {}, set(), []
    rows = (events or {}).get("merged_pr_events", [])
    if not isinstance(rows, list):
        rows = [{"id": identity} for identity in summary["merged_prs"]]
    for item in rows:
        identity = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identity, (str, int)) or isinstance(identity, bool):
            gaps.append({"source": "merged_pr_events", "reason": "合并时间事件缺少有效身份"})
            continue
        identity = str(identity)
        merged_at = instant(item.get("merged_at"))
        if merged_at is None or merged_at > clock or identity in times and times[identity] != merged_at:
            invalid.add(identity)
            gaps.append({"source": "merged_pr_events", "reason": "合并时间缺失、在未来或存在冲突"})
        else:
            times[identity] = merged_at
    new, uncertain = [], []
    known_set = set(known)
    for identity in summary["merged_prs"]:
        if identity in known_set:
            continue
        if identity in invalid or state.get("incomplete_history") or truncated and not (
                identity in times and previous is not None and times[identity] > previous):
            uncertain.append(identity)
        else:
            new.append(identity)
    if uncertain:
        gaps.append({"source": "merged_prs", "reason": "合并事件超出可证明的去重窗口或时间不可确认；未计为新增"})
    return known, new, uncertain, bool(truncated), gaps


def rebuild(root, roots, loader, now=None, events=None):
    """在既有资源锁协议内更新有界投影，原始记录不改写。"""
    import host_isolation
    handle = host_isolation.acquire(("verification-reports",), timeout=5)
    try:
        directory = Path(root) / "efficiency"
        state_path = directory / "state.json"
        state_problem = None
        try:
            state_exists = state_path.exists()
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_exists else {}
            if not isinstance(state, dict) or state.get("schema", SCHEMA) != SCHEMA:
                raise ValueError("回顾状态 schema 不兼容")
            if state_exists and (state.get("schema") != SCHEMA
                    or not all(isinstance(state.get(key), list) and all(isinstance(item, str) for item in state[key])
                               for key in ("sample_ids", "reviewed_prs"))
                    or not isinstance(state.get("candidate_versions"), dict)
                    or "pr_history_truncated" in state and not isinstance(state["pr_history_truncated"], bool)
                    or instant(state.get("reviewed_at")) is None):
                raise ValueError("回顾状态字段不完整")
            if not state_exists and ((directory / "summary.json").exists()
                    or (directory / "summary.md").exists() or any((directory / "reviews").glob("*.json"))):
                state_problem = "回顾状态缺失但已有衍生历史；须恢复可信状态后再生成增量回顾"
            elif state.get("incomplete_history"):
                state_problem = "回顾去重历史不完整；须恢复可信状态后再生成增量回顾"
        except (OSError, ValueError) as exc:
            state = {}
            state_problem = "回顾状态损坏；不生成增量回顾：%s" % error_label(exc)
        if any(item.get("source") == "review-state" for item in (events or {}).get("gaps", []) if isinstance(item, dict)):
            state_problem = "远端回顾去重历史不可确认；恢复可信状态后再生成增量回顾"
        summary = build_summary(roots, loader, now=now, events=events)
        guard_version, guard_required, guard_problems = _merge_guard(
            directory, state, (events or {}).get("selection_findings") or [], roots)
        summary["gaps"].extend(guard_problems)
        if guard_problems:
            summary["status"] = "gaps"
        clock = instant(summary["generated_at"])
        ids = sorted(item["run_id"] + ":" + item["content_digest"] for item in summary["history"])
        previous_ids = set(state.get("sample_ids") or [])
        new_samples = sorted(set(ids) - previous_ids)
        merge_state = dict(state, incomplete_history=True) if state_problem else state
        consumed_prs, new_prs, uncertain_prs, history_truncated, merge_gaps = _new_merge_events(summary, merge_state, events)
        summary["gaps"].extend(merge_gaps)
        summary["merge_events"] = {"new": new_prs, "unconfirmed": uncertain_prs,
                                   "history_truncated": history_truncated}
        if merge_gaps:
            summary["status"] = "gaps"
        previous_time = instant(state.get("reviewed_at"))
        days_due = previous_time is not None and clock - previous_time >= 7 * 86400
        merges_due = len(new_prs) >= 10
        due = bool(new_samples) and (days_due or merges_due) and not state_problem
        reason = ("无新样本，保持安静" if not new_samples else
                  "第10个新增合并 PR" if merges_due else "满7天且有新样本" if days_due else "未达到回顾周期")
        if due and days_due and merges_due:
            reason = "第10个新增合并 PR 与满7天同时满足，只生成一次"
        candidate_versions = dict(state.get("candidate_versions") or {})
        changed = []
        for candidate in summary["candidates"]:
            version = digest(candidate)
            if candidate_versions.get(candidate["id"]) != version:
                changed.append(candidate)
            candidate_versions[candidate["id"]] = version
        if due:
            review_id = digest({"samples": ids, "prs": summary["merged_prs"], "previous": state.get("reviewed_at")})[:20]
            review = {"schema": SCHEMA, "review_id": review_id, "generated_at": summary["generated_at"],
                      "reason": reason, "new_samples": new_samples,
                      "new_merged_prs": new_prs, "candidates": changed[:3], "gaps": summary["gaps"]}
            _write(directory / "reviews" / (review_id + ".json"), json.dumps(review, ensure_ascii=False, indent=2))
            consumed_prs.extend(new_prs)
            history_truncated = history_truncated or len(consumed_prs) > MAX_HISTORY
            summary["merge_events"]["history_truncated"] = history_truncated
            summary["review"] = {"generated": True, "review_id": review_id, "reason": reason,
                                 "candidates": changed[:3]}
        else:
            summary["review"] = {"generated": False, "reason": reason}
        if state_problem:
            summary["gaps"].append({"source": str(state_path), "reason": state_problem})
            summary["status"] = "gaps"
        # 首次收集建立7天起点；只有回顾消费 PR 事件和候选，避免未到期时吞掉增量。
        updated = {"schema": SCHEMA, "sample_ids": ids if due or not state else state.get("sample_ids", []),
                   "reviewed_at": summary["generated_at"] if due or previous_time is None else state["reviewed_at"],
                   "reviewed_prs": consumed_prs[-MAX_HISTORY:], "pr_history_truncated": history_truncated,
                   "candidate_versions": dict(list(candidate_versions.items())[-MAX_HISTORY:]) if due else state.get("candidate_versions", {}),
                   "incomplete_history": bool(state_problem),
                   "guard_required": guard_required, "guard_strategy_version": guard_version}
        summary = redact_paths(summary, roots)
        _write(state_path, json.dumps(redact_paths(updated, roots), ensure_ascii=False, indent=2))
        _write(directory / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        _write(directory / "summary.md", render(summary) + "\n")
        review_dir = directory / "reviews"
        if review_dir.exists():
            archived = sorted(review_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in archived[:-MAX_REVIEWS]:
                path.unlink()
            for path in archived[-MAX_REVIEWS:]:
                try:
                    previous = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    previous = {"schema": SCHEMA, "review_id": path.stem, "status": "gaps",
                                "gaps": [{"source": "reviews/" + path.name,
                                          "reason": "旧衍生回顾不可读取：%s" % error_label(exc)}]}
                _write(path, json.dumps(redact_paths(previous, roots), ensure_ascii=False, indent=2))
        return summary
    finally:
        host_isolation.release(handle)


def render(summary):
    metrics = summary["metrics"]
    lines = ["验证效率摘要（规则 v%s，%s）" % (summary["rule_version"], summary["status"]),
             "样本 %s；累计执行 %.3f 秒；执行区间并集 %.3f 秒；含排队的验证区间并集 %.3f 秒" % (
                 metrics["runs"], metrics["execution_seconds"], metrics["execution_union_seconds"],
                 metrics["verification_interval_union_seconds"]),
             "排队 %.3f 秒；锁等待 %.3f 秒；清理 %.3f 秒；实际重复执行 %d；证据引用 %d" % (
                 metrics["queue_seconds"], metrics["lock_wait_seconds"], metrics["cleanup_seconds"],
                 metrics["actual_duplicate_executions"], metrics["evidence_references"]),
             "运行结论 %s；用例跳过 %s；原始失败保留" % (metrics["outcomes"], metrics["skipped_cases"]),
             "区间并集按记录估算并限制在验证窗口内；原始累计执行不裁剪（interval_precision=estimated）",
             summary["comparison_note"]]
    for bucket in summary["comparison_groups"]:
        lines.append("  %s %s：n=%d，原始秒数=%s，中位数=%s；%s" % (
            bucket["id"], bucket["dimensions"]["scope"]["group"], bucket["sample_count"],
            bucket["raw_seconds"], bucket["median_seconds"],
            "同组可比" if bucket["comparable"] else "不可直接比较：" + "；".join(bucket["incomparable_reasons"])))
    feedback = metrics["first_related_feedback_seconds"]
    lines.append("首个相关反馈（首个组完成，含此前等待）：%s" % feedback)
    for candidate in summary["candidates"]:
        lines.append("候选 %s：%s；证据 %s；风险 %s；建议验收 %s" % (
            candidate["kind"], candidate["scope"], candidate["evidence"],
            candidate["quality_risk"], candidate["suggested_acceptance"]))
    for gap in summary["gaps"]:
        lines.append("缺口：%s（%s）" % (gap.get("reason"), gap.get("source")))
    lines.append("增量回顾：%s" % summary["review"]["reason"])
    return "\n".join(lines)
