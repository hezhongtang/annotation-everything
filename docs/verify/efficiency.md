# 持续效率报告与维护

`verify_report.py` 从已有验证记录生成效率投影。报告不启动产品测试、不访问真实审阅材料。两阶段工作流见 [onboarding.md](onboarding.md)：开发只做快速检查，全量验证和效率问题处理留到用户手动发起的 PR review，不为开 PR 额外采样或重跑。总规格的实测与未完成项见 [final-acceptance.md](final-acceptance.md)。

## 读取与重建

```bash
python3 verify.py report --efficiency                         # 只读本地历史
python3 verify.py report --efficiency --json                  # 结构化历史报告
python3 verify.py report --efficiency --rebuild               # 只重建投影与回顾状态
python3 verify.py report --efficiency --source <构件目录>     # 导入已有 CI 记录根，可重复
python3 verify.py report --efficiency --events <事件清单>     # 读取既有合并事件与采集缺口
```

普通 `report --efficiency` 不写文件。投影和回顾只保存 `local/runs/<ID>/record.json` 等根内定位；外部记录根用稳定的 `source-*` 别名，异常中的其他绝对路径脱敏。重建同时清理既有衍生回顾的路径，不改原始证据。`--rebuild` 和每次 `run` 收尾更新 `.verify/efficiency/summary.json`、`summary.md`、`state.json`，不改原始 `record.json` 或日志。`--efficiency` 与单记录选项 `--run` / `--latest` 互斥。`--now` 接受带时区的 ISO 8601 时间，用于重放合成历史；不会修改验证记录的时间。

有数据缺口时，效率报告返回 1 并保留可读部分；缺口不是测试失败，也不是健康结论。自动摘要写入失败时保留原测试退出码，并在终端说明缺口。原始验证结论和证据链仍通过 `report --latest` 或 `report --run <ID>` 读取。

## 指标口径

| 指标 | 含义与限制 |
| --- | --- |
| `execution_seconds` | 实际执行组的单调时钟耗时累计；引用组不计入，不等于用户等待 |
| `execution_union_seconds` | 根据实际组起点和执行耗时计算的区间并集；并行重叠只计一次 |
| `verification_interval_union_seconds` | 各次验证从入队到结束的区间并集，含排队与收尾；不包含思考和开发时间 |
| `queue_seconds` / `lock_wait_seconds` / `cleanup_seconds` | 入口规划排队、资源锁等待、登记进程清理，分别展示；不把锁等待当测试性能回退 |
| `first_related_feedback_seconds` | 开发阶段首个实际执行组完成的时间；新记录采用单调时钟，旧记录按已有时长估算并标注 `precision=estimated`，不会超过已有验证窗口；后续浏览器门禁仍须另行完成 |
| `actual_duplicate_executions` | 同输入、同环境、同集合的重复成功执行；可能是必要平台门禁、宿主状态或独立疑点，报告不自动消除 |
| `evidence_references` | `reused` 组的引用次数；不增加新执行数或通过数 |
| `outcomes` / `skipped_cases` | 原始失败、取消、中断、超时、跳过等状态保留，失败后成功只标偶发失败候选 |

区间并集按已记录起点与单调时长估算（`interval_precision=estimated`），组区间限制在验证窗口内，裁剪的 run ID 保留在 `interval_clipped_runs`；原始累计执行不裁剪。它不能直接当作某个用户的全部等待时间。组时间戳精度受旧记录限制；严格性能验收使用同环境、固定集合的原始运行耗时。

比较键包含 OS、Python、CI、浏览器与依赖、配置、隔离方式、策略和入口版本，以及实际用例 / 场景集合。不同键明确标为不可直接比较。历史浏览器记录没有实测版本时保持不可比较；新浏览器入口记录实际 Chrome 与 Playwright 版本，Playwright 依赖仍只在原浏览器入口导入。用例 ID 相同不能自行证明断言语义未变，性能验收仍须核对行为矩阵。

每组输出样本数、原始值和中位数，不输出小样本百分位趋势。规则 v2 的回退线索是：同组最早 3 个有效通过样本作为可比较基线，最新连续 3 个有效通过样本各自同时慢至少 20% 且 10 秒。中间失败或无效样本会打断连续性。该规则只产生候选，不增加 CI 失败门槛。

## 原始证据、保留与并发

每个 run ID 只算一次，包括复制到不同 worktree 的同一记录。同 ID 内容冲突、索引损坏、缺失日志、校验和不符、不支持的 schema、未结束运行、结束时间不可确认或超出 24 小时保鲜窗口都显示缺口。超期记录仅作历史观察，不作为当前通过证明；旧日志不会被转换成成功记录。`report`、G 复用和 H 投影共用必要计时字段、实际执行组日志的定位 / 字节数 / 校验和校验；缺失时长、非有限值、负数、布尔值或错误类型均不作为有效性能样本，未知时长不伪装成零秒通过。原声明结论保留并标记证据不可用。引用组无需本地日志，仍重校验来源链。

投影最多保留最近 200 个运行样本、200 个已消费合并事件 / 候选版本、12 份增量回顾，并报告截断数量。合并事件按消费时序保留，不按编号字符串排序。超出去重窗口的未识别事件只在 GitHub `merged_at` 晚于上次回顾时间时可证明为新合并；旧事件、缺时间、未来时间或冲突时间保留缺口，不计为新增。旧状态无法证明保留边界时同样从紧。采集器的 `merged_pr_events` 提供这些时序，保留只有 `merged_prs` 的旧清单兼容。原始本地运行记录继续保留，CI 原始构件维持原有 14 天保留期。投影不替代原件；丢失或过期原件仍是缺口。

重建使用 `host_isolation.py` 的 `verification-reports` 共享资源。原子替换与同一资源锁保证并发重建不会重复消费事件或丢失去重状态；产品测试执行不持有此资源。锁获取失败时说明摘要缺口，不绕开锁。损坏的回顾状态保留为 `incomplete_history`，恢复可信的 `state.json` 后再生成回顾；不能据此宣称过去没有事件。

## 周期回顾与 CI

每次验证结束刷新摘要。只有上次回顾后有新样本，且新增合并 PR 满 10 个或距上次回顾满 7 天时才归档增量回顾；两个条件同时满足只归档一次。重复事件、没有新样本均保持安静。首次收集建立 7 天起点。回顾最多展示 3 个候选，每个包含证据定位、范围、质量风险和建议验收；相同候选的证据未变时不重复列入新回顾。

本地 `run` 不发网络请求。需要汇总远端证据时，使用现有 `gh` 登录态执行：

```bash
python3 tools/collect_verify_history.py --repo hezhongtang/annotation-everything --out-dir .verify
python3 verify.py report --efficiency --rebuild --events .verify/ci-events.json
```

采集器只读最近 100 个构件与最近 100 个已关闭 PR，缺省最多导入 20 个 `verify-python*` 构件，`--limit` 可设为 1–50。截断、权限不足、离线、构件过期或只有旧格式日志都保留缺口。远端构件先进行路径、符号链接、文件数和展开体积检查，不执行其中任何代码；来源运行链接保留在 `source_artifacts`。缓存只保留本次选择的构件，不触及本地原始运行目录。

`.github/workflows/verify-efficiency.yml` 在既有 CI 完成后、每日定时和手动触发时只采集和重建报告，不运行完整回归。工作流只使用 `contents: read`、`actions: read`、`pull-requests: read`；始终检出默认分支，只有同仓库默认分支报告工作流产出的状态才可恢复为去重历史。报告与状态通过 `verify-efficiency-history` 构件延续，原有 Python 3.9 / 3.12 CI 保持完整执行。报告不发聊天或邮件、不自动创建工单、不自动合并或关闭 issue。

## 确认遗漏与策略维护

影子发现只标候选。维护者排除输入、环境、范围差异后，可在本地事件清单记录确认结果，再执行 `report --efficiency --rebuild --events <清单>`：

```json
{
  "schema": "ae-verify-events/1",
  "merged_prs": [],
  "gaps": [],
  "selection_findings": [{
    "status": "confirmed",
    "strategy_version": 3,
    "reason": "已确认遗漏的说明；无法界定影响时使用 uncertain",
    "evidence": ["runs/<run ID>/record.json"]
  }]
}
```

`confirmed` / `uncertain` 事件与已有守卫按最高策略版本合并，乱序、重放或后续旧事件均不能降级当前守卫。未来版本或无法解析的影响继续保持完整验证；只有已知旧版本被当前受审查策略替代时失效。事件形成 `.verify/efficiency/selection-guard.json`。后续 `plan` 解释扩大原因，`run` 将实际请求扩大到 `all` 并停用证据复用。日志里的普通文字不能设置确认状态。守卫损坏、或状态要求守卫但文件缺失时继续完整验证；不完整的确认事件按无法界定影响处理。

修正映射、缩小范围、删除重复断言或调整性能目标通过独立小 PR 审查，递增对应策略版本并记录理由。新策略版本替代旧版本守卫，后续运行记录保存所用版本；保留旧提交可追溯。即时完整验证入口始终是 `python3 verify.py run --group all --no-reuse`。性能目标未达成时记录瓶颈并保持父项开放，由维护者明确决定后续优化或规格调整。
