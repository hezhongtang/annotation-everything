# 分析模式：实现与验收对照

规格为 [#109](https://github.com/hezhongtang/annotation-everything/issues/109)，由整合 [PR #121](https://github.com/hezhongtang/annotation-everything/pull/121) 交付。领域规则见 [ADR 0007](../adr/0007-analysis-inside-review.md) 和 [GLOSSARY.md](../../GLOSSARY.md)。

当前为开发阶段：#111–#114 代码和相关快速检查已完成，#115 正在实施。完整单元、服务安全自检、完整浏览器、整线独立审查及最新提交 CI 待验证。以下测试对应关系随最终实现更新，未运行项保持待验证。

## 原型九情形

测试通过公开存储操作建立合成审阅，断言状态、身份、批注联动和拒绝原因。原型中的演示目录、固定编号和关闭锁定开关不属于产品接口。

| 情形 | 关键断言与测试入口 | 当前覆盖 |
| --- | --- | --- |
| 从批注到结论 | [存储](../../tests/test_analysis_store.py) 的 `AnalysisStartTests`、`ConvertTests`；[生命周期](../../tests/test_analysis_lifecycle.py) 的五种回答、假设与事实；[历史](../../tests/test_analysis_history.py) 的 `test_close_requires_no_queued_work_seals_and_reopen_requests_questions` | 前序规则已通过；#115 核对完整链路 |
| 改选答案，再改回来 | [保护](../../tests/test_analysis_protection.py) 的 `test_redecide_detaches_and_revives_the_same_decisions_and_note`；批注搁住和恢复、原决策身份、已领取快照保留 | 已通过；失效假设例外由 #115 补齐 |
| 从检查点分出新思路 | [历史](../../tests/test_analysis_history.py) 的 `test_fork_inherits_ids_and_switches_restore_latest_state_and_note_basis`、`test_sealing_preserves_the_snapshot_and_identity_after_later_answers` | 已通过 |
| 推翻一个假设 | 推翻前快照、直接依赖才分线、原答案与撤下原因、未受影响下游恢复、受影响旧决策不复用 | #115 实施中 |
| 标记死局 | 提议经确认才阻断、跨线拦截重裁/作答/切线/分线、依赖推翻后解除并待复核、死局身份稳定 | #115 实施中 |
| Agent 处理时只能看和作答 | [保护](../../tests/test_analysis_protection.py) 的整分析锁和 claimed 作答；[历史](../../tests/test_analysis_history.py) 的 `test_a_claim_on_another_line_locks_all_history_operations_but_not_answers`；已处理轮次拒答 | 前序规则已通过；推翻保护由 #115 补齐 |
| 如果不锁住会怎样 | [保护](../../tests/test_analysis_protection.py) 的 `test_rejected_writeback_voids_and_reissues_once`、`test_void_round_carries_only_valid_answers_and_uses_latest_claimed_answer`、`test_reissue_carries_applied_structure_answers_without_reapplying_them` | 已通过；产品保持锁定 |
| 已交出但没被接手的批注 | [存储](../../tests/test_analysis_store.py) 的 `test_start_pulls_note_out_of_unclaimed_batch`、`test_start_emptying_a_batch_voids_it`；[Agent 契约](../../tests/test_analysis_agent.py) 的跨队列顺序 | 已通过 |
| 已经在改的批注 | [存储](../../tests/test_analysis_store.py) 的 `test_start_refuses_note_already_under_revision`、`test_start_refuses_processed_note` | 已通过 |

原型有 9 个显式“应被拒绝”步骤：死局 2 个、处理期间保护 5 个、改稿中的源批注 2 个。基线失效另有一次语义拒收：测试须断言旧轮作废与自动重排，不能只检查调用返回。最终映射还需覆盖父规格扩展的轮次作答、整线死局和从死局线检查点分线等入口。

## 跨情形契约

| 契约 | 测试入口与核对点 |
| --- | --- |
| 回答即时生效、原话保留 | [轮次存储](../../tests/test_analysis_store.py) 的幂等修改、原话不截断、部分作答和重复交出；[生命周期](../../tests/test_analysis_lifecycle.py) 的五种回答及默认禁止外查 |
| 结构变化须确认 | [生命周期](../../tests/test_analysis_lifecycle.py) 的 `test_structure_split_merge_and_void_require_confirmation_and_keep_history`；[保护](../../tests/test_analysis_protection.py) 的结构作废/还原批注联动 |
| 草稿与批注确认 | [存储](../../tests/test_analysis_store.py) 的 `ConvertTests`；[保护](../../tests/test_analysis_protection.py) 的更新文本先起草后确认、保留旧文、停止跟踪与删除 |
| 身份与历史 | [历史](../../tests/test_analysis_history.py) 的全局同选项决策复用、跨线自由文本选项身份、检查点稳定、未确认提议继承、完整历史不受后续投影改变 |
| 所属线与当前线分开 | [历史](../../tests/test_analysis_history.py) 的 `test_queued_round_uses_its_own_line_while_another_line_is_displayed`；假设确认按线、事实跨线共享；批注有效性只看当前线 |
| 唯一轮次与共享队首 | [存储](../../tests/test_analysis_store.py) 的审阅内唯一轮次号；[Agent 契约](../../tests/test_analysis_agent.py) 的统一交出顺序、精确领取、旧等待进程不领取分析和一次一项 |
| 勾稽只列待确认 | [保护](../../tests/test_analysis_protection.py) 的 `test_full_check_reads_conclusions_without_an_open_round_and_never_rewrites_notes`；[Agent 契约](../../tests/test_analysis_protection_agent.py) 的四类提示与只读命令 |
| 下一轮确定性组合 | #115 核对死局 → 批注更新 → 假设 → 结构提议 → 当前线前沿问题；有效回答仅预填一次，已确认实体不重复创建 |

## 界面与协议证据

存储规则以外，服务测试只核对请求/响应、错误码与安全门禁，页面快速测试执行真实 JavaScript 呈现和交互入口，词表契约核对中英 key、占位符和复数分支。

- [基础页面](../../tests/test_analysis_page.py)、[保护页面](../../tests/test_analysis_protection_page.py)、[历史页面](../../tests/test_analysis_history_page.py) 覆盖输入缓存、确认与保护、只读历史、自然名称和错误文案。
- [服务及 Agent 契约](../../tests/test_analysis_agent.py)、[保护契约](../../tests/test_analysis_protection_agent.py)、[历史契约](../../tests/test_analysis_history_agent.py) 覆盖绑定、投递、CLI 与 HTTP；服务安全自检代码沿用既有 Host、Origin 和 JSON Content-Type 门禁，不添加 CORS，不暴露 `index.html` 和 `/api/*` 以外的路径。
- 浏览器冒烟 `analysis-smoke` 已登记；第二条界面场景与页卡片浮层由 #115 补齐。场景实际执行待 PR review，见[场景协议](../verify/browser-scenarios.md)。

## 当前证据与后续门禁

本地整合 tip `23e4f83` 的 #114 最终相关组为 run `20261105T142909Z-3bbf04`：125/125、13.97 秒、锁等待 0、干净 HEAD 与稳定输入。历史阶段记录保留在 PR 评论和本机交接目录；该结果覆盖其记录中的范围，后续新实现仍需对应证据。

Lint、格式化和类型检查未配置；`git diff --check` 只用于空白检查。PR review 需执行项目规定的完整门禁并跟进最新提交 CI，满足合并门禁后由 PR 自动关闭父子 issues。分析不做导出/导入，完整记录须保留审阅目录。
