# 浏览器场景注册与局部执行

浏览器端到端入口 `tests/e2e_browser.py` 按场景组织（issue #53）。场景的稳定 ID、
行为说明、fixture、隔离边界与旧参数别名登记在 `tests/e2e_scenarios.py` 的注册表里；
该模块只用标准库，`python3 -m unittest discover tests` 可以导入，playwright 仍只在
`tests/e2e_browser.py` 导入。清单、执行计划和结构化摘要都从这一份注册表生成，
不另维护副本。场景内的等待基于可观察状态（#54）：等待机制分类、固定等待保留
清单与前后性能样本见 `docs/verify/browser-waits.md`。

## 用法

```bash
uv run --no-project --with playwright python tests/e2e_browser.py --list              # 场景清单：ID、行为、fixture、隔离、是否全量必选
uv run --no-project --with playwright python tests/e2e_browser.py                     # 全量：全部注册场景按注册顺序执行
uv run --no-project --with playwright python tests/e2e_browser.py --only skill-i18n   # 只跑目标场景（可逗号分隔多个，去重）
uv run --no-project --with playwright python tests/e2e_browser.py --skills-only       # 旧参数保留为别名
```

- 无参数 = 现有完整验收，场景集合与顺序见 `--list`。
- `--only` 在启动浏览器之前完成解析；多选去重并按注册顺序执行。
- 未知、空和互相冲突的选择（`--only` 与旧参数混用、多个旧参数并用、`--list` 带选择）
  在执行任何产品操作前报错，退出码 2，并给出恢复指引。
- 运行输出先打印执行计划，再逐场景打印横幅与检查；结尾保留「N/M 通过」人类摘要，
  其后追加一行 `AE-SCENARIOS: <JSON>` 场景执行清单（稳定场景 ID、起止时间、实际检查
  数量、结果），`verify.py run --group browser` 把它写进记录的 `groups[].scenarios`
  （见 `docs/verify/record-contract.md`）。
- 中断（Ctrl-C）时正在执行的场景记为 `interrupted`，其后场景记为 `not_run`，退出码 130。
  通过 `verify.py` 取消时也保留已完成检查、场景明细与原始日志；清理期间的再次中断不打断回收。
  没有收到中断信号的浏览器异常按错误记录；空检查与未完整执行的计划都不能作为通过结果。

## 旧行为到新场景的对照

改造前 `main()` 无参数时的执行顺序与新场景一一对应（顺序不变）：

| 改造前的段落（tests/e2e_browser.py） | 新场景 ID | 旧局部参数 |
| --- | --- | --- |
| `console_layout_checks`（#118 抽屉开合与刷新记忆、双语标题栏、窄屏） | `console-layout` | — |
| `focus_mode_checks`（#118 全屏批注、快捷键、分层 Esc 与 CSS 退路） | `focus-mode` | — |
| `installed_delivery_checks`（#36 三种语言组合的安装副本流程） | `installed-delivery` | `--delivery-only` |
| `locale_live_controls_checks`（同页切语言只改系统标签） | `locale-live-controls` | — |
| `skill_search_checks`（技能输入栏搜索与选择） | `skill-search` | `--skills-only` |
| `skill_recommendation_checks`（技能推荐） | `skill-recommendations` | `--skills-only` |
| `skill_i18n_checks`（技能与常驻技能的中英界面） | `skill-i18n` | `--skills-only` |
| `full_check_checks`（#32/#36 全量勾稽提交） | `full-check` | `--full-check-only` |
| `locale_checks`（语言最小闭环 + 编辑操作双语） | `locale-loop` | — |
| `english_script_checks`（#34 英文讲稿） | `english-script` | — |
| `handoff_i18n_checks`（#32 提交 / 复核 / 处理记录双语） | `handoff-i18n` | `--handoff-only` |
| `current_task_controls_checks`（#81 新增：详情开关、保存失败拦截、停止服务与重启恢复） | `current-task-controls` | — |
| `receiver_binding_checks`（#84 接收方身份、状态、旧服务与断线） | `receiver-binding` | — |
| `export_import_checks`（#35 双语导出与跨语言导入） | `export-import` | — |
| main() 内联段 1（file:// 退回模式） | `file-mode` | — |
| main() 内联段 2（报告样式保真） | `css-fidelity` | — |
| `review_overview_checks`（#100 审阅总览：分组、详情、返回与双语） | `review-overview` | — |
| `review_overview_stop_checks`（#144 总览单项停止、保存保护和实际退出确认） | `review-overview-stop` | — |
| `review_overview_stop_scope_checks`（#145 总览按范围批量停止、固定目标、部分完成和失败重试） | `review-overview-stop-scope` | — |
| `overview_home_checks`（#160 总览首页：零运行审阅直接打开、默认与 `--under` 范围、按组排序、无返回与顶栏停止、视图内双语、失效清理命令、新标签页打开对照页、接收提示与可复制等待命令、桌面事件绑定展示、过滤后的批量停止与全部回收） | `overview-home` | — |
| `docx_review_checks`（#135：DOCX 原分页、限制清单、点 / 框与只读提示；#136：段落定位、文本反查、保存重试、同文异段导入、旧服务阻断；#137：可回写提示及选区不可读时的只读结构提示） | `docx-review` | — |
| main() 内联段 3–8（服务模式全流程、断线补传、写入失败、服务卡死、同一端口换审阅、恶意网页与导入） | `server-mode` | — |
| `poll_repaint_checks`（#93 新增：轮询数据不变时不重画，其他触发点照旧立即更新） | `poll-repaint` | — |

对照说明：

- 段 3–8 在改造前就是同一次服务会话上的连续阶段（共享页面、审阅目录与批次状态），
  按用户行为拆成一个 `server-mode` 场景；拆成多个场景需要复刻会话状态而不增加行为
  覆盖，属于按函数长度机械切分。
- `server-mode` 原来消费段 1 写下的 `export.md`；现在改为场景自己的准备步骤——
  用真实 CLI（`notes.py resolve` 标已处理后 `notes.py export --all`）从独立合成审阅
  导出同一格式的 Markdown，不再依赖其他场景留下的数据。
- 旧局部参数的行为修正：改造前所有旧参数都会先执行 `installed-delivery` 与
  `locale-live-controls` 再判断参数；现在别名只执行原定目标场景，目标行为本身不变。
- #53 改造保留原有检查；后续新增的 `current-task-controls` 在全量模式中执行，也可通过 `--only current-task-controls` 单独运行。不能用通过数下降证明性能。
- `overview-home`（#160）随总览首页功能加入：临时台账（入口的临时 `AE_HOME`）加七份合成审阅（运行中、两份已停、归档、失效、桌面事件绑定与 `--under` 范围外），首页由 `serve.py overview` 真实子进程提供，默认与 `--under` 各启一次；场景结束回收首页与全部审阅服务（含代启动的）。该场景可在总览首页工单的验证交付里单独运行（`--only overview-home`），等待全部基于可观察状态。

## 新增场景的接入规则

1. 在 `tests/e2e_scenarios.py` 的 `SCENARIOS` 登记：稳定 ID（小写加连字符）、行为说明、
   fixture（声明的准备步骤，只承担本场景需要的状态）、隔离边界、是否全量必选、
   旧参数别名（如有）。场景准备必须走真实产品路径，不能用测试捷径代替被测行为。
2. 在 `tests/e2e_browser.py` 的 `SCENARIO_RUNNERS` 挂执行函数；入口启动时做两侧一致性
   检查（`--list` 也先过这道检查），缺任何一边都以退出码 2 失败并说明缺口，不会静默少跑。
3. 场景必须能单独运行：独立浏览器上下文、临时审阅目录或明确声明的隔离边界；
   可以共享浏览器进程等启动成本，不能共享会改变结果的批注、提交或断言状态。
   场景以 error 结束时，入口会立即停掉该场景遗留的服务器进程（`TestCleanup.stop_procs`），
   临时目录仍在进程退出时统一清理；服务器「用后即停」不因错误路径失效。
4. 更新 `tests/test_e2e_scenarios.py` 的 `FULL_ORDER`（全量顺序契约）与本文件的对照表。
