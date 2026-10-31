# Agent instructions

- 对话用中文；标识符、命令、路径保持原文。提交信息用约定式格式 `<type>: <description>`，描述用中文。
- skill 本体在 `skill/annotation-everything/`，改完跑 `python3 ae.py install` 同步到 `~/.claude/skills/`。
- 运行时脚本只用 Python 标准库；测试专用依赖（playwright）只出现在 `tests/e2e_browser.py`。
- 验证：`python3 -m unittest discover tests`；动了页面模板或服务，再跑 `uv run --no-project --with playwright python tests/e2e_browser.py`。
- 安全底线不能退：只绑 127.0.0.1、Host 校验、写操作 Origin 同源 + JSON Content-Type、无 CORS 头、只暴露 index.html 与 /api/*。改服务后跑 `serve.py check`。
- 测试与 eval 只用合成数据（`fixtures/`、`skill/annotation-everything/evals/*/files/`）。任何真实业务材料、批注不得进入本仓库或 skill 目录。
- eval 运行产物在 `skill/annotation-everything-workspace/`（已忽略）。

## 分支、issue 与 PR

`main` 只通过 PR 的 squash 合并更新。仓库是免费版私有仓库，GitHub 无法在服务器端锁定 `main`，下面的规则靠本地钩子和这份约定执行。

- **先启用钩子**：克隆后运行一次 `tools/setup-dev.sh`。钩子会拒绝在 `main` 上提交、推送到 `main` 和删除 `main`，本地 `main` 只能快进到 `origin/main` 上已有的提交。不要用 `--no-verify` 绕过；确需绕过时先征得用户同意。
- **一个 issue 一个分支、一个 PR**：从 issue 开分支，`gh issue develop <编号> --name <type>/<编号>-<简述> --base main --checkout`，`type` 与提交类型一致（feat / fix / docs / chore 等），不用工具名作前缀。没有 issue 的小改动也要开分支，命名 `<type>/<简述>`。
- **issue 跟着合并关闭**：PR 正文写 `Closes #编号`，issue 在合并进 `main` 时自动关闭。有子 issue 的父 issue，在最后一个子 issue 的 PR 里同时写 `Closes #父编号`。不要手动关闭已实现的 issue；只有不做（`wontfix`）、重复或无效的 issue 才手动关闭，并留评论说明原因。
- **验收项在 PR 里核对**：开 PR 前逐条核对 issue 的验收标准，在 issue 里勾选已满足的项；做不到的写进 PR 正文，不要关闭 issue。
- **开发前先有验收标准**：新 issue 按 `.github/ISSUE_TEMPLATE/` 的模板写（命令行创建时正文也按模板的小节写）并标 `needs-triage`；验收标准和兼容性要求写清楚后改为 `ready-for-agent`，Agent 只领取 `ready-for-agent` 的 issue。
- **CI 失败自动修复（auto-fix）**：创建或更新 PR 后主动跟进 CI。出现失败时，读取失败日志、定位原因并自动修复，在原 PR 分支验证、提交和推送修复，持续跟进至最新提交的 CI 全部通过，无需等待用户再次要求。保留既有检查与测试要求；若因权限、凭据或外部服务故障无法继续，明确报告阻塞原因及所需协助。
- **合并前**：CI 通过，非小改动先做独立审查；合并用 squash，分支在合并后自动删除。
- **发版**：修改版本号（`skill/annotation-everything/scripts/serve.py` 中的 `VERSION` 常量）的 PR 挂到对应版本的里程碑（如 `v0.9.0`）；合并后在合并提交上打附注标签 `git tag -a v0.9.0 <提交> -m "annotation-everything v0.9.0"` 并推送，再关闭里程碑。

## README 维护

每次有提交合并进 `main`、或 PR 合并后，都要检查并更新 README，让它和仓库现状一致；能在同一个 PR 里改就一起改，否则合并后另开分支和 PR，补一个 `docs:` 提交。

- README 是中英双语两份：`README.md`（中文）和 `README.en.md`（英文），两份内容与结构保持同步，改一份就改另一份。
- 头图同样分两版：`docs/assets/banner.zh.svg` 只用中文，`docs/assets/banner.en.svg` 只用英文（项目名、Claude Code、Agent 等专名除外）；改文案时两版一起改。
- 两份 README 的版本徽章、两版头图里的版本号，与 `skill/annotation-everything/scripts/serve.py` 的 `VERSION` 一致。
- 「功能」「常用命令一览」「项目结构」覆盖新增、改名或删除的功能、命令和文件。
- 界面有明显变化时，用 `fixtures/sample/` 的合成样例重拍 `docs/assets/` 里的截图；截图里不得出现真实材料。
- 项目定位变了，同步更新 GitHub 仓库的 About 描述和 topics（`gh repo edit`）。
