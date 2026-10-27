# Agent instructions

- 对话用中文；标识符、命令、路径保持原文。提交信息用约定式格式 `<type>: <description>`，描述用中文。
- skill 本体在 `skill/annotation-everything/`，改完跑 `tools/install.sh` 同步到 `~/.claude/skills/`。
- 运行时脚本只用 Python 标准库；测试专用依赖（playwright）只出现在 `tests/e2e_browser.py`。
- 验证：`python3 -m unittest discover tests`；动了页面模板或服务，再跑 `uv run --no-project --with playwright python tests/e2e_browser.py`。
- 安全底线不能退：只绑 127.0.0.1、Host 校验、写操作 Origin 同源 + JSON Content-Type、无 CORS 头、只暴露 index.html 与 /api/*。改服务后跑 `serve.py check`。
- 测试与 eval 只用合成数据（`fixtures/`、`skill/annotation-everything/evals/*/files/`）。任何真实业务材料、批注不得进入本仓库或 skill 目录。
- eval 运行产物在 `skill/annotation-everything-workspace/`（已忽略）。
