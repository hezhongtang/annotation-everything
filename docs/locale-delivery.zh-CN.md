# 中英文审阅交付验收

[English](locale-delivery.en.md)

对应父规格 #28 和最终子项 #36。2026-10-31 在 macOS 26.6.2（Apple Silicon）、Python 3.13.13、Google Chrome 156.0.8078.74 上验证。沿用版本 0.9.0，没有发版或打标签。实施工作区的审查基线为 `70bdd9c875cbbfc3d5d45ae0d175f8d56841d4b6`；这是 #29–#35 全部合并后的主线。

## 交付内容

- 一个 `annotation-everything` 安装副本和双语发现入口。按请求语言加载 `workflow.zh-CN.md` 或 `workflow.en.md`，两者先读同一份 `protocol.md`；不要求重复安装或同时加载两套完整流程。
- 中英文格式参考、英文合成报告、按技能发现与停止条件组织的 eval 输入。指引覆盖语言、生成、重建、等待、技能、改稿、复核、收尾、导入和恢复。
- 集成验收发现的遗漏一并修复：生成器的帮助、结果和资源诊断，批注 CLI 的帮助及导入导出反馈，页面批注入口、导入与恢复提示。显示翻译不参与对象值、身份、状态或安全拒绝的判定。
- 两份 README 与中英文截图同步，版本徽章和头图继续对应实际版本 0.9.0。

## 复现完整产品流程

以下命令从仓库根目录依次运行。单元测试涉及安装维护的进程盘点，不要与浏览器服务回归并行运行。

```bash
python3 -m unittest discover tests
AE_DELIVERY_ARTIFACTS="$PWD/skill/annotation-everything-workspace/locale-delivery" \
  uv run --no-project --with playwright python tests/e2e_browser.py
AE_HOME=$(mktemp -d) python3 skill/annotation-everything/scripts/serve.py check --locale en
```

只跑安装副本交付流程时，在浏览器命令末尾加 `--delivery-only`。临时技能副本、审阅台账、源文件和服务互相隔离，退出时回收测试进程与临时目录。显式指定的 `AE_DELIVERY_ARTIFACTS` 目录保留合成证据：环境与检查结果 `evidence.json`，每条流程的 `commands.json`、修改前后源文件、`notes.json`、`handoff.json`、离线 Markdown / JSON 和截图。该目录已被忽略，不属于已安装技能的本体。

| 流程 | 界面 / 材料 | 验证行为 |
|---|---|---|
| `english` | 英文 / 英文 | 从临时安装脚本生成，浏览器批注与实际提交唤醒 `wait`，修改报告和讲稿，重建，初改停在待复核；用户在页面显式请求后才复核并核对处理计数 |
| `chinese` | 中文 / 中文 | 同一安装入口完成同样的生成、提交、改稿、重建与复核流程，原有中文能力保留 |
| `mixed` | 英文 / 中文 | 界面、交接与系统反馈用英文，材料、批注、附言和处理说明保持原文，身份与 v1 中文对象值不变 |
| 三条流程的文件模式 | 与上述相同 | 无服务打开同一个 HTML，批注刷新恢复，真实下载 Markdown 与 JSON，再跨语言导入；不会制造服务端提交 |

英文流程还检查导入草稿在关闭、切换语言和重新打开后保留，跨审阅导入的确认使用英文，取消后不写入，审阅名中的恶意属性不执行。宽窗口交付截图与窄窗口的文件模式共用真实浏览器操作。

## 英文 Agent 演示

`evals/english-apply-and-stop/` 使用独立合成材料。把临时安装路径放进 `AE_SKILL_DIR`、临时台账目录放进 `AE_HOME`，在空评测目录运行该用例的 `place-files.sh`，然后把 `prompt.md` 交给 Agent。安装本身仍只运行一次 `ae.py install`，没有第二套语言技能。

本次由当前 Agent 实际读取临时安装副本的入口、共用约束和英文流程，接收英文请求，检查全部 6 页报告与讲稿并修改源文件；没有向子代理分派这批工作。第 3 页 KPI 改为 `YoY +12%`，讲稿原本已说明同比，无须再改。第 6 页两侧均改成评估调价。供应商选择缺乏质量证据，保持未处理。还发现分段时间预算与逐页时长不同，说明开篇 4:20 / 4:00、主线 3:30 / 4:00 的差异与待确认问题，没有擅自改时长；总长 7:50 仍在 8 分钟内。

实际收尾结果是批次 `drafted`、两条批注 `drafted`、一条 `open`。没有自行复核或定稿，没有直接改写受锁保护状态，没有残留服务或等待进程。可检查的请求、命令输出、结果 JSON 和英文回报保存在 `skill/annotation-everything-workspace/agent-eval/`。这是当前会话的指引演示，不是盲测或跨模型发现率评测。

`evals/discovery-cases.json` 另列中英文的生成、按批注改稿、保存恢复共 6 条发现与路由请求。已人工核对入口描述与应加载流程；这些用例可用于后续检查实际模型的技能加载轨迹。字面关键词检查不能证明模型触发率，本次没有把未运行的多模型自动评测记为通过。

## 代表性兼容矩阵

| 约束 | 回归证据 |
|---|---|
| 旧审阅、无配置调用保持中文；`auto` 回退；切换不写批注或改身份 | `test_locale_loop.py`、`test_i18n.py`、浏览器 `locale_checks` |
| 旧中文 Markdown、两种标记文法、跨语言导入和 JSON 结构 | `test_export_format.py`、浏览器 `export_import_checks`、安装副本的文件导出与 CLI 导入 |
| 机器 JSON、判定代码、退出码和中文存储值 | `test_ae_cli_i18n.py`、`test_serve_locale.py`、`test_notes_exchange_i18n.py` |
| 旧服务或旧 Agent 的提交要求、兼容提示、未确认选项 | `test_handoff_i18n.py`、`test_core.py`、浏览器 `full_check_checks` / `handoff_i18n_checks` |
| 断线、卡住、5xx、补传顺序、服务错配与暂存隔离 | 完整浏览器回归的现有故障与恢复场景 |
| 脚本结束标记、引号、花括号、HTML 与恶意属性安全插值 | `test_review_page_i18n.py`、`test_script_formats.py`、浏览器 CSP / 注入场景及跨审阅导入 |
| 词典缺项、占位符、必要复数分支与 0 / 1 / 2 | `test_i18n.py` 与浏览器语言回归 |
| 临时技能副本完整、按需指引链接可达 | `test_ae_cli.py` 与安装副本交付流程；既有安装快照检查覆盖全部资源 |

## 支持范围与限制

目前支持简体中文和英文；`zh-Hant`、`zh-TW` 等不作为简体别名接受。用户材料、批注、第三方技能说明和原始系统错误不会自动翻译。界面语言不会决定材料语言、地区或时区。`--locale` / `AE_LOCALE` / 审阅配置的优先级保持明确；导出包装另用 `--lang`。

文件模式无法唤醒 Agent，且 `file://` 与 HTTP 暂存隔离。Markdown 不保留批注 id、页面键、时间戳、批次与完整痕迹，保留旧文法的元信息行、空行和状态前缀限制；JSON 是完整批注备份，不替代单独的提交历史和源文件快照。旧服务的兼容提示与附言上限仍需按共用约束处理。

真实浏览器验收仅在上述 Chrome 环境运行；没有将 Safari、Firefox、其他操作系统或不同模型的行为标为已验证。本机个人电脑的既有信任边界不变：服务只绑定回环，保留 Host、同源 Origin、JSON Content-Type、无 CORS 与 CSP 约束。
