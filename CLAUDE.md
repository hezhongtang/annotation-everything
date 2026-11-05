@AGENTS.md

## 用户问答

向用户提问时，必须调用当前环境可用的问答工具互动；具体规则见 AGENTS.md「用户问答」。

## Agent skills

### Issue tracker

GitHub Issues for `hezhongtang/annotation-everything`; use `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles map to GitHub labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.

## 验证与效率回顾

按 AGENTS.md「开发与 PR review」执行两阶段流程：issue 实现阶段只做快速相关单元测试与已配置的 Lint、格式检查，开 PR 后交接；用户手动调用 agents 后才做全量审查与测试。范围选择和证据交接见 `docs/verify/onboarding.md`；排查效率报告缺口、回顾触发或修改策略时，读取 `docs/verify/efficiency.md`。

## PR 的 CI

CI 保留自动触发。用户发起 PR review 后，由审查 Agent 按 AGENTS.md「分支、issue 与 PR」中的「CI 失败自动修复（auto-fix）」规则持续处理；开发 Agent 开 PR 后结束交接。

## README 维护

每次提交合并进 `main` 或 PR 合并后，按 AGENTS.md「README 维护」检查并同步更新中英两份 README（`README.md`、`README.en.md`）和两版头图。
