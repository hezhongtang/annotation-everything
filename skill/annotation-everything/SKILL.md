---
name: annotation-everything
description: 本机逐页审阅、批注和改稿。将 HTML 报告与讲稿并排展示，收集引用、删除、改写、点或区域标注，接收提交后按批注同步修改报告和讲稿。用户说「审阅这份报告」「讲稿对照」「看我的批注」「按批注改」「交给你了」，或报告对照页打不开、批注未保存时使用。Use when the user asks to review a slide-style HTML report with speaker notes, collect page annotations, apply annotations, revise an annotated report, receive a submission, or recover a local review page. Triggers include “review this report”, “set up a review”, “I finished annotating”, “apply my annotations”, and “the review page cannot save”.
---

# annotation-everything

Generate a local review page → bind its receiving session → start its service → receive a submission → edit the source report and speaker notes → rebuild → record each result → finish the batch → receive the next submission.

Before loading a workflow, resolve `$S` to the `scripts/` directory beside this loaded `SKILL.md` and run `python3 "$S/update.py" auto --locale LANG`, replacing `LANG` with `zh-CN` or `en` for the user's request. The check runs at most once per day and does not block the review when offline. If it installs an update, reload this `SKILL.md` and its references; before resuming an existing review, stop any old service, rebuild the review page, start the new service, and ask the user to refresh the browser. Briefly tell the user about an installed update or a warning from the command.

先按用户请求的语言选择操作指引。Choose the workflow from the user's request language, independently of the material language or the page's display language.

1. **两种语言都先读 / Always read** [共用安全与协议约束 / Shared safety and protocol](references/protocol.md). This is the single source for safety, permissions, stop conditions, and compatibility rules.
2. **中文请求**：读取[中文操作流程](references/workflow.zh-CN.md)，只加载本次需要的格式参考。
3. **English requests**: read the [English workflow](references/workflow.en.md). Load the [English format reference](references/formats.en.md) when checking inputs, importing annotations, or reading structured fields.
4. Read one workflow per request. If the user explicitly requests another language, use that workflow. Do not load both complete workflows by default.

`$S` in the guides means the `scripts/` directory beside this installed `SKILL.md`. Resolve it from the skill copy that was loaded. The scripts use the Python standard library. Both languages use the same installation, scripts, translation resources, review identity, and storage protocol.

## 各 Agent 的对应机制 / Agent mechanisms

- **中文请求**：首次配置等待与唤醒、调用批注指定技能、分派子代理或设置 `--by` 时，读取[各 Agent 的对应机制](references/agent-mechanisms.zh-CN.md)。
- **English requests**: read [Agent mechanisms](references/agent-mechanisms.en.md) before configuring a waiter, invoking annotation skills, delegating work, or choosing a `--by` name.

Load only the matching language reference. The shared protocol still governs permissions and stage boundaries.

接收方身份：首次接收、换 harness 或换会话前，读取[接收方绑定与交接](references/receiver-binding.zh-CN.md)。Receiver identity: before first receipt or changing harness or session, read [Receiver binding and transfer](references/receiver-binding.en.md). Load only the matching language.

Codex 桌面接入与服务重启：首次配置自动接收时，读取匹配语言的 Agent 机制中的持久绑定流程；先验证空闲会话实际接手，再宣布自动处理可用。Codex desktop delivery and service restarts: follow the persistent binding procedure in the matching Agent mechanisms reference; verify receipt by an idle chat before declaring automatic processing ready.
