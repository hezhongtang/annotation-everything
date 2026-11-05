# Agent instructions

- 对话用中文；标识符、命令、路径保持原文。提交信息用约定式格式 `<type>: <description>`，描述用中文。
- skill 本体在 `skill/annotation-everything/`，改完跑 `python3 ae.py install` 同步到 `~/.claude/skills/`。
- 运行时脚本只用 Python 标准库；测试专用依赖（playwright）只出现在 `tests/e2e_browser.py`。
- 验证统一使用 `python3 verify.py`，执行时机按下文「开发与 PR review」。范围选择、证据交接和故障恢复见 `docs/verify/onboarding.md`；效率回顾或策略更新见 `docs/verify/efficiency.md`；记录契约见 `docs/verify/record-contract.md`。
- 同机并发：资源敏感的验证（完整单元、完整浏览器、敏感模块直跑）按 `host-state` 共享资源自动互斥，跨 worktree 与独立 checkout 生效，锁等待在记录里单独计时（上限 `AE_LOCK_WAIT_SECONDS`，缺省 600 秒，超时不是通过）；纯逻辑组不受影响。协议与安全边界见 `docs/verify/isolation.md`，不要绕开或另建互斥机制。
- 安全底线不能退：只绑 127.0.0.1、Host 校验、写操作 Origin 同源 + JSON Content-Type、无 CORS 头、只暴露 index.html 与 /api/*。服务安全自检统一在 PR review 的全量验证中执行。
- 测试与 eval 只用合成数据（`fixtures/`、`skill/annotation-everything/evals/*/files/`）。任何真实业务材料、批注不得进入本仓库或 skill 目录。
- eval 运行产物在 `skill/annotation-everything-workspace/`（已忽略）。

## 开发与 PR review

- **需求阶段**：只整理需求与验收标准，不自动运行测试。
- **issue 实现阶段**：完成代码、必要的测试代码与文档，只执行与改动直接相关的快速单元测试、已配置的 Lint 和格式检查。相关单元优先点名用例或类，以单次反馈 60 秒为目标；完整单元、浏览器、服务安全自检、eval 和依赖宿主状态的慢项统一留到 PR review。选测建议回退全量时，将全量要求写入 PR，不在实现阶段自动扩大执行范围。
- **快速检查工具**：Lint 和格式化工具另开 PR 接入，当前仓库尚未配置；如实标注「未配置」。接入后按项目配置检查本次改动文件，格式化限定在本次改动范围。`git diff --check` 用于空白检查，不能记作 Lint 或格式化通过。纯协作文档、README 与 PR 模板改动检查文本和 diff，单元测试标注「不适用」；skill 指引、fixture 和执行配置不适用此豁免。
- **开 PR 与交接**：快速检查中发现的实现问题先修复，再提交并开 PR。超时、锁等待或环境阻塞如实记录为未完成，列出已运行的检查、结果和移交范围；未运行项不得标记通过。开发 Agent 开 PR 后结束交接，不自动启动 review agents，也不等待全量测试或 CI 收敛。
- **PR review 阶段**：由用户手动调用 agents，完成全量代码审查，并运行 `python3 verify.py run --group all --phase pr --pr <编号>`。全部有效差异位于内置文档允许列表且通过策略判定时，记录产品测试 `not_applicable`、由 CI 兜底；其余 PR 实际执行完整单元、服务安全自检和完整浏览器。豁免边界见 `docs/verify/selection-strategy.md`，`--no-reuse` 可强制文档 PR 实际执行。CLI 的 `review` 阶段用于按疑点补测，不能替代 `pr` 门禁；已有快速检查证据不替代门禁要求的实际执行。审查发现问题后在原 PR 分支修复并完成验证，跟进最新提交的 CI。
- **合并门禁**：全量审查、适用的 PR 门禁及 CI 全部通过后才可合并；开 PR 不代表完成验收。现有 CI 自动触发、同机资源互斥与证据有效性要求继续执行。

## 用户问答

需要用户补充信息、澄清需求、选择方案或确认设计结论时，必须调用当前环境可用的问答工具互动；有可选方案时给出推荐选项，不以普通回复中的问题列表代替工具。沿用用户已明确的决定，避免重复提问。问答工具确实不可用时，先说明原因，再用文字提问。

## 分支、issue 与 PR

`main` 只通过 PR 的 squash 合并更新。仓库是免费版私有仓库，GitHub 无法在服务器端锁定 `main`，下面的规则靠本地钩子和这份约定执行。

- **先启用钩子**：克隆后运行一次 `tools/setup-dev.sh`。钩子会拒绝在 `main` 上提交、推送到 `main` 和删除 `main`，本地 `main` 只能快进到 `origin/main` 上已有的提交。不要用 `--no-verify` 绕过；确需绕过时先征得用户同意。
- **一个 issue 一个分支、一个 PR**：从 issue 开分支，`gh issue develop <编号> --name <type>/<编号>-<简述> --base main --checkout`，`type` 与提交类型一致（feat / fix / docs / chore 等），不用工具名作前缀。没有 issue 的小改动也要开分支，命名 `<type>/<简述>`。
- **issue 跟着合并关闭**：PR 正文写 `Closes #编号`，issue 在合并进 `main` 时自动关闭。有子 issue 的父 issue，在最后一个子 issue 的 PR 里同时写 `Closes #父编号`。不要手动关闭已实现的 issue；只有不做（`wontfix`）、重复或无效的 issue 才手动关闭，并留评论说明原因。
- **验收项在 PR 里核对**：开 PR 时逐条说明 issue 验收项的实现状态，依赖全量测试的项标记「待 PR review 验证」；审查阶段在 issue 里勾选已有证据支持的项。未满足的项写进 PR 正文，不要关闭 issue。
- **开发前先有验收标准**：新 issue 按 `.github/ISSUE_TEMPLATE/` 的模板写（命令行创建时正文也按模板的小节写）并标 `needs-triage`；验收标准和兼容性要求写清楚后改为 `ready-for-agent`，Agent 只领取 `ready-for-agent` 的 issue。
- **CI 失败自动修复（auto-fix）**：用户发起 PR review 后，由审查 Agent 跟进 CI。出现失败时，读取失败日志、定位原因，在原 PR 分支验证、提交和推送修复，持续跟进至最新提交的 CI 全部通过，无需用户逐次要求。若因权限、凭据或外部服务故障无法继续，明确报告阻塞原因及所需协助。
- **合并前**：完成「开发与 PR review」的合并门禁，非小改动包含独立审查；合并用 squash，分支在合并后自动删除。
- **发版**：修改版本号（`skill/annotation-everything/scripts/serve.py` 中的 `VERSION` 常量）的 PR 挂到对应版本的里程碑（如 `v0.9.0`）；合并后在合并提交上打附注标签 `git tag -a v0.9.0 <提交> -m "annotation-everything v0.9.0"` 并推送，再关闭里程碑。

## README 维护

每次有提交合并进 `main`、或 PR 合并后，都要检查并更新 README，让它和仓库现状一致；能在同一个 PR 里改就一起改，否则合并后另开分支和 PR，补一个 `docs:` 提交。

- README 是中英双语两份：`README.md`（中文）和 `README.en.md`（英文），两份内容与结构保持同步，改一份就改另一份。
- 头图同样分两版：`docs/assets/banner.zh.svg` 只用中文，`docs/assets/banner.en.svg` 只用英文（项目名、Claude Code、Agent 等专名除外）；改文案时两版一起改。
- 两份 README 的版本徽章、两版头图里的版本号，与 `skill/annotation-everything/scripts/serve.py` 的 `VERSION` 一致。
- 「功能」「常用命令一览」「项目结构」覆盖新增、改名或删除的功能、命令和文件。
- 界面有明显变化时，用 `fixtures/sample/` 的合成样例重拍 `docs/assets/` 里的截图；截图里不得出现真实材料。
- 项目定位变了，同步更新 GitHub 仓库的 About 描述和 topics（`gh repo edit`）。
