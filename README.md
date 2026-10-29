# annotation-everything

Claude Code skill：把材料变成本地审阅页，用户在浏览器里逐页批注，批注经只监听本机的小服务自动落盘，Claude 直接读取、改稿、标已处理。

当前支持的材料类型：**汇报材料（逐页 HTML 报告）+ 讲稿（Markdown）**。名字留了余地：以后可以扩展到文字稿等其他材料。

页面上能做的：选中文字直接删除 / 改写或引用写批注（另一侧同一页有对应句时提示「同时改」）、在报告上标注（单击标点、拖动框选区域，可拖动、调大小）、风格优化（整页 / 标注处）、给批注指定技能（也可设常驻技能，按范围约束每条批注）、「交给 Agent」并看到处理进度、勾选全量勾稽让 Agent 检查整份报告和讲稿的前后一致性、按批查处理记录（每批解决几条、处理摘要）、先改后审（弱模型初改 → 强模型复核，或停在待复核由你决定；每条批注留处理痕迹）、重新打开时提示上次批注到哪页。代理侧用 `notes.py wait` 等待用户提交，处理后 `resolve` / `done` 回写状态。

## 目录

```
skill/annotation-everything/   ← skill 本体（安装时整体拷贝）
  SKILL.md
  references/formats.md        ← 输入输出格式
  scripts/build_viewer.py      ← 生成对照页 index.html + review.json
  scripts/viewer_template.html ← 对照页模板（服务模式 / 本地文件模式）
  scripts/serve.py             ← 本地服务（serve / status / list / forget / check）
  scripts/ledger.py            ← 本机审阅台账（审阅标识 → 审阅目录、专属端口）
  scripts/notes.py             ← 批注命令行（show / resolve / reopen / import / wait / done / skills）
  scripts/match.py             ← 报告 ↔ 讲稿对应句查找
  scripts/notes_store.py       ← 批注存储（原子写、文件锁、操作日志）
  evals/                       ← skill-creator 测试用例
fixtures/sample/               ← 合成样例（虚构数据）
tests/test_core.py             ← 单元测试（标准库）
tests/e2e_browser.py           ← 浏览器端到端测试（真 Chrome，测试专用依赖 playwright）
tools/install.sh               ← 安装到 ~/.claude/skills/annotation-everything
```

## 开发

```bash
python3 -m unittest discover tests                                   # 单元测试
uv run --no-project --with playwright python tests/e2e_browser.py   # 浏览器端到端（用本机 Google Chrome）
python3 skill/annotation-everything/scripts/serve.py check          # 服务安全自检
tools/install.sh                                                     # 安装
```

测试会把审阅台账指到临时目录（`AE_HOME`）。手动试用或跑 skill-creator eval 时也建议 `export AE_HOME=$(mktemp -d)`，免得临时审阅登记进你真实的台账、占掉专属端口。

## 快速试用

```bash
S=skill/annotation-everything/scripts
python3 $S/build_viewer.py --report fixtures/sample/report.html --script fixtures/sample/script.md --out /tmp/ae-demo
python3 $S/serve.py /tmp/ae-demo
python3 $S/serve.py list          # 本机有哪些审阅、各自的专属端口和状态
```

## 约束

- 运行时只用 Python 标准库；不联网、无遥测；服务只绑 127.0.0.1。
- 仓库里只放合成样例。真实材料、批注、审阅目录一律不进仓库、不进 skill 目录。
