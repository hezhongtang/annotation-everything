---
name: annotation-everything
description: 从原始材料生成逐页报告初稿、本机逐页审阅、批注、分析和改稿。生成走内置默认范式（需求澄清、材料读取登记、逐页 HTML、可选讲稿、核验记录、公开核验入口）；审阅将 HTML 报告、Word/DOCX 文档（按原分页直接审阅）或 PowerPoint/PPTX 演示文稿（每张幻灯片一页、本机渲染、幻灯片定位批注）与讲稿并排展示，收集引用、删除、改写、点或区域标注，接收提交后按批注同步修改报告和讲稿。用户说「帮我生成一份报告」「从这份材料做汇报」「审阅这份报告」「审阅这份 Word / DOCX」「审阅这份 PPT / PPTX」「讲稿对照」「看我的批注」「按批注改」「交给你了」，或报告对照页打不开、批注未保存时使用。Use when the user asks to generate a report draft from raw material, review a slide-style HTML report, a Word (DOCX) document or a PowerPoint (PPTX) deck with speaker notes, collect page annotations, apply annotations, revise an annotated report, receive a submission or analysis round, or recover a local review page. Triggers include “generate a report”, “turn this material into a briefing”, “review this report”, “review this Word document”, “review this PPTX deck”, “set up a review”, “I finished annotating”, “apply my annotations”, and “the review page cannot save”.
license: Apache-2.0
---

# annotation-everything

Generate a report draft or a local review page → bind its receiving session → start its service → receive work. Revision batches follow the source-editing workflow; analysis rounds follow the matching-language analysis guide and produce questions instead of report edits.

按用户请求的语言选择工作流。Choose the workflow from the user's request language, independently of the material language or the page's display language. For a bare activation such as `/annotation-everything`, use the current conversation language. If the conversation gives no language cue, use Chinese (`zh-CN`).

Before loading the selected workflow, resolve `$S` to the `scripts/` directory beside this loaded `SKILL.md` and run `python3 "$S/update.py" auto --locale LANG`, using `zh-CN` or `en` for `LANG`. The check runs at most once per day and allows the workflow to continue offline. If it installs an update, reload this `SKILL.md` and its references; before resuming an existing review, stop any old service, rebuild the review page, start the new service, and ask the user to refresh the browser. Briefly tell the user about an installed update or a warning from the command. If the copy lacks `update.py`, continue to the selected workflow.

1. **两种语言都先读 / Always read** [共用安全与协议约束 / Shared safety and protocol](references/protocol.md). This is the single source for safety, permissions, stop conditions, and compatibility rules.
2. **中文请求**：读取[中文操作流程](references/workflow.zh-CN.md)，只加载本次需要的格式参考。要从原始材料生成新报告时，再读[报告生成：默认范式](references/generation.zh-CN.md)——生成方法的选择（内置范式或外部生成技能）与接入约定也在其中。
3. **English requests**: read the [English workflow](references/workflow.en.md). Load the [English format reference](references/formats.en.md) when checking inputs, importing annotations, or reading structured fields. To generate a new report from raw material, also read [Report generation: default paradigm](references/generation.en.md) — it also covers choosing the generation method (built-in paradigm or an external generation skill) and the integration rules.
4. Read one workflow per request. If the user explicitly requests another language, use that workflow. Do not load both complete workflows by default.
5. **无任务激活 / Activation without a task**. If the user only activates the skill and the current turn contains no material, task, or handoff event, open the standalone overview home with `serve.py overview`. After the update check, follow [启动总览首页](references/workflow.zh-CN.md#启动总览首页) or [Start the overview home](references/workflow.en.md#start-the-overview-home) in the selected workflow.
6. **分析事件**：收到 `analysis` 轮次或配置分析接收时，中文请求读取[分析操作指引](references/analysis.zh-CN.md)，按该流程处理。**Analysis events**: for an `analysis` round or analysis-receiving setup, English requests use the [analysis guide](references/analysis.en.md). Load only the matching language.

`$S` in the guides means the `scripts/` directory beside this installed `SKILL.md`. Resolve it from the skill copy that was loaded. The scripts use the Python standard library. Both languages use the same installation, scripts, translation resources, review identity, and storage protocol.

## 各 Agent 的对应机制 / Agent mechanisms

- **中文请求**：首次配置等待与唤醒、调用批注指定技能、分派子代理或设置 `--by` 时，读取[各 Agent 的对应机制](references/agent-mechanisms.zh-CN.md)。
- **English requests**: read [Agent mechanisms](references/agent-mechanisms.en.md) before configuring a waiter, invoking annotation skills, delegating work, or choosing a `--by` name.

Load only the matching language reference. The shared protocol still governs permissions and stage boundaries.

接收方身份：首次接收、换 harness 或换会话前，读取[接收方绑定与交接](references/receiver-binding.zh-CN.md)。Receiver identity: before first receipt or changing harness or session, read [Receiver binding and transfer](references/receiver-binding.en.md). Load only the matching language.

Codex 桌面接入与服务重启：首次配置自动接收时，读取匹配语言的 Agent 机制中的持久绑定流程；先验证空闲会话实际接手，再宣布自动处理可用。Codex desktop delivery and service restarts: follow the persistent binding procedure in the matching Agent mechanisms reference; verify receipt by an idle chat before declaring automatic processing ready.
