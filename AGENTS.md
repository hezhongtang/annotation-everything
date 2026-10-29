# Agent instructions

- 对话用中文；标识符、命令、路径保持原文。提交信息用约定式格式 `<type>: <description>`，描述用中文。
- skill 本体在 `skill/annotation-everything/`，改完跑 `tools/install.sh` 同步到 `~/.claude/skills/`。
- 运行时脚本只用 Python 标准库；测试专用依赖（playwright）只出现在 `tests/e2e_browser.py`。
- 验证：`python3 -m unittest discover tests`；动了页面模板或服务，再跑 `uv run --no-project --with playwright python tests/e2e_browser.py`。
- 安全底线不能退：只绑 127.0.0.1、Host 校验、写操作 Origin 同源 + JSON Content-Type、无 CORS 头、只暴露 index.html 与 /api/*。改服务后跑 `serve.py check`。
- 测试与 eval 只用合成数据（`fixtures/`、`skill/annotation-everything/evals/*/files/`）。任何真实业务材料、批注不得进入本仓库或 skill 目录。
- eval 运行产物在 `skill/annotation-everything-workspace/`（已忽略）。

## README 维护

每次有提交合并进 `main`、或 PR 合并后，都要检查并更新 README，让它和仓库现状一致；能在同一个 PR 里改就一起改，否则合并后补一个 `docs:` 提交。

- README 是中英双语两份：`README.md`（中文）和 `README.en.md`（英文），两份内容与结构保持同步，改一份就改另一份。
- 头图同样分两版：`docs/assets/banner.zh.svg` 只用中文，`docs/assets/banner.en.svg` 只用英文（项目名、Claude Code、Agent 等专名除外）；改文案时两版一起改。
- 两份 README 的版本徽章、两版头图里的版本号，与 `skill/annotation-everything/scripts/serve.py` 的 `VERSION` 一致。
- 「功能」「常用命令一览」「项目结构」覆盖新增、改名或删除的功能、命令和文件。
- 界面有明显变化时，用 `fixtures/sample/` 的合成样例重拍 `docs/assets/` 里的截图；截图里不得出现真实材料。
- 项目定位变了，同步更新 GitHub 仓库的 About 描述和 topics（`gh repo edit`）。
