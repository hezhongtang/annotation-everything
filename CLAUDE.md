@AGENTS.md

## Agent skills

### Issue tracker

GitHub Issues for `hezhongtang/annotation-everything`; use `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles map to GitHub labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.

## PR 的 CI

PR 出现 CI 失败时，立即按 AGENTS.md「分支、issue 与 PR」中的「CI 失败自动修复（auto-fix）」规则处理，无需等待用户再次要求。

## README 维护

每次提交合并进 `main` 或 PR 合并后，按 AGENTS.md「README 维护」检查并同步更新中英两份 README（`README.md`、`README.en.md`）和两版头图。
