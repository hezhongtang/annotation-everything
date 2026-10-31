# 统一验证入口接入说明

这份文档给后续子项（B–H）的实施者和日常开发者看：怎么用 `verify.py`，怎么复现一份记录，记录里各字段的含义见 [record-contract.md](record-contract.md)。不需要读原始聊天记录。

## 它解决什么问题

以前完整单元、浏览器和服务安全自检都靠人拼命令，结果只有终端里滚过的文本。失败、跳过、超时、信号中断和「输入在跑的过程中被改过」在文本里分不开，也没法跨阶段比较。`verify.py` 把这三件事收到一个入口：`plan` 解释范围，`run` 执行并留下结构化记录，`report` 只读记录。它只包裹现有执行行为，不选测、不复用旧结果、不并行化、不删慢项。

入口放在仓库根目录，和 `ae.py` 并列，而不是并进 `ae.py`：`ae.py` 是发给用户的产品管理命令，输出走中英双语、围绕技能副本和本机台账；验证入口面向仓库检出，技能副本还没装也能验证，输出只要中文。两者生命周期不同，理由见 [docs/adr/0002-unified-verification-entry.md](../adr/0002-unified-verification-entry.md)。

## 快速开始

```bash
python3 verify.py plan                      # 将执行哪些组、跑什么命令，不启动测试
python3 verify.py run                       # 完整单元（缺省组），结束后打印摘要
python3 verify.py report --latest           # 读最近一次记录，不启动测试
```

`run` 支持 `--json`：stdout 只输出记录 JSON，子进程输出改走 stderr，方便脚本消费。

## 验证组

| 组 | 内容 | 等价命令 | 计数 | 计时粒度 |
| --- | --- | --- | --- | --- |
| `unit` | 完整单元 | `python3 -m unittest discover tests` | 按用例 | 用例 |
| `unit-related` | 相关单元，显式指定 | `python3 -m unittest <选择器>` | 按用例 | 用例 |
| `browser` | 完整浏览器 | `uv run --no-project --with playwright python tests/e2e_browser.py` | 按日志摘要 | 整组 |
| `serve-check` | 服务安全自检 | `python3 skill/annotation-everything/scripts/serve.py check` | 按日志摘要 | 整组 |
| `all` | 完整验收 | `unit` → `serve-check` → `browser` 依次执行 | 分别计数 | 分别计时 |

`--group` 可以重复。组名也可以直接写在 `run` 后面：`python3 verify.py run unit`、`python3 verify.py run unit-related tests/test_core.py`。不写 `--group` 时，有选择器按 `unit-related`，否则按 `unit`。

相关单元的选择器接受三种写法：`tests/test_core.py`、`tests.test_core`、`tests.test_core.ClassName.test_method`。不给选择器就是空集合，入口返回 2 并说明原因——空集合不算通过。

## 常用跑法

```bash
# 开发中：只跑改动涉及的模块
python3 verify.py run unit-related tests/test_core.py tests/test_i18n.py

# PR 收敛：完整单元 + 服务安全自检 + 完整浏览器
python3 verify.py run --group all --phase pr --pr 50 --reason "PR 收敛前验收"

# CI：完整单元 + 服务安全自检（浏览器依赖不进 CI）
python3 verify.py run --group unit --group serve-check --phase ci

# 某个组设超时，超时按 124 退出并回收子进程
python3 verify.py run --group browser --timeout 300
```

`--issue`、`--pr`、`--phase`、`--reason` 都写进记录，是后续判断「这次为什么跑」的依据。`--phase` 只接受 `issue`、`development`、`review`、`pr`、`ci`、`manual`。

## 怎么读报告

报告分七段：结论、阶段与关联、提交与基线、输入指纹与环境、耗时、分组结果、证据。三个要点：

- 结论行保留原运行的结论和退出码；「能否作为通过证据」反映本次读取时的校验结果。日志丢失或损坏时，报告标为证据不可用并返回 1，即使原运行通过也不例外。`report --json` 同样输出当前校验结果和问题，不改写磁盘上的原记录。
- 耗时段把排队、墙钟、入口自身开销、用例耗时合计、类 / 模块级开销、未归属开销分开列。用例耗时合计是各用例之和，不是等待时间；要看等待时间看墙钟。
- 输入段说明开始与结束的指纹是否一致。指纹按完整文件内容计算，包括大文件；输入不可读时，快照标为不可用。指纹变化或任一快照不可用时，保留原测试结果，但记录不能作为覆盖最终输入的通过证据，`report` 返回 1。

## 重现步骤

从干净克隆开始，不需要任何额外安装：

```bash
git clone <仓库地址> && cd annotation-everything
python3 verify.py plan                      # 1. 看将执行什么
python3 verify.py run --phase development --issue 50 --reason "复现样本"
python3 verify.py report --latest           # 2. 读这次记录
python3 verify.py report --run <run ID> --json   # 3. 获取当前校验后的 JSON 报告
```

记录在 `.verify/runs/<run ID>/`：`record.json` 是结构化记录，`unit.log` 等是子进程原始输出，`unit.cases.json` 是用例级计时明细。`.verify/` 已被 `.gitignore` 忽略；用 `AE_VERIFY_DIR` 或 `--out-dir` 可以改位置。记录中的路径参数会脱敏：仓库内选择器保存为相对路径，仓库外产物位置保存为占位符，实际执行路径不受影响。

只读一份已有的产物目录也可以报告，不需要仓库：

```bash
python3 verify.py report --out-dir <解压后的构件目录> --latest
```

## 参考环境

2026-10-31 的验证环境：

| 环境 | 系统 | Python | 用途 |
| --- | --- | --- | --- |
| macOS 开发机 | Darwin 25.6.0，arm64 | CPython 3.14.6 / 3.12.13 / 3.9.25 | 完整单元、服务安全自检、完整浏览器 |
| Linux CI | ubuntu-22.04 | CPython 3.9 / 3.12 | 完整单元、服务安全自检 |

记录结构在两个系统上一致（同一个 schema，`environment` 段记实际系统与 Python）。耗时数字不能跨系统直接比较：机器负载、CPU 和文件系统都不同。同系统比较时用 `input.fingerprint` 与 `environment` 相同作为前提。

## 初始样本

`docs/verify/samples/` 下两份样本都在合成仓库里生成，不含真实审阅材料：

- `run-ci-sample.json`：`--group unit --group serve-check --phase ci` 全部通过，`usable_as_evidence` 为 `true`，展示完整字段。
- `run-pr-sample.json`：`--group all --phase pr --pr 50`，单元通过而自检与浏览器各有一项失败，`usable_as_evidence` 为 `false`，展示失败态与按日志摘要计数的组。

样本里的用例、检查项和提交信息都是合成的。`run_id`、时间戳和指纹是该次运行的实测值。

## 采集开销

同一台机器、同一份输入、同样的完整单元范围，多次实测：

| 跑法 | unittest 自报耗时 | 入口自身开销 |
| --- | --- | --- |
| `python3 -m unittest discover tests` | 146.121 秒 / 144.593 秒 | — |
| `python3 verify.py run --group unit` | 149.220 秒 / 144.417 秒 / 155.127 秒 | 0.0014–0.0024 秒 |

入口自身开销约 0.001%，远低于 5% 的目标。两种跑法的墙钟差落在同一模式的运行间波动范围内，不能归因于计时：计时探针跑在被测进程内，没有为计时把纯逻辑测试改成独立进程，也没有并行累计值冒充等待时间。整包套件里新增了 43 个验收用例，其中多数要起子进程，这是测试数量的增加，不是单次验证的采集成本。

浏览器组的真实接入：`python3 verify.py run --group browser` 在 macOS 开发机上 557/557 通过，墙钟约 189 秒，入口自身开销 0.0006 秒。浏览器组按整组计时，记录里用例耗时合计为 0 是预期的——场景级计时要等 D 项。

## 给 B–H 的接入点

- 记录契约已版本化，后续只在 `ae-verify-run/1` 上追加字段，升版本才改结构。字段语义和必填 / 可空规则见 [record-contract.md](record-contract.md)。
- C（同机隔离与锁）：`children.json` 已经是所有权登记簿，只登记本次入口拉起的进程；锁字段加在 `groups[]` 上，不要改登记语义。
- D（浏览器场景）：把 `groups[].scenarios` 从空数组填成场景记录，`granularity` 从 `group` 细化到 `scenario`；完整模式的行为矩阵不变。
- F（阶段选测）：`input.fingerprint`、`input.changed_since_baseline` 和 `context.phase` 已经够了；选择理由和策略版本加在 `context` 与 `groups[]`。
- G（证据复用）：按 `input.fingerprint` + `environment` + `groups[].name` 匹配已有记录，只认当前校验后 `result.usable_as_evidence` 为 `true` 的记录，不能直接信任落盘时的通过标记。
- H（持续报告）：读 `.verify/index.jsonl` 汇总，不新增记录字段，也不在产品测试之外再跑东西。

## 兼容与迁移

- 原来的直接命令继续有效，完整执行的成功 / 失败退出码语义一致：`python3 -m unittest discover tests`、`uv run --no-project --with playwright python tests/e2e_browser.py`、`python3 skill/annotation-everything/scripts/serve.py check`。
- 例外是空集合与全跳过：直接跑 `unittest` 返回 0，入口返回 1，两者都不算完整成功。
- 运行时只用 Python 标准库，语法兼容 Python 3.9。playwright 仍然只从 `tests/e2e_browser.py` 导入，入口不导入它。
- 服务行为、审阅格式、Git 钩子和 CI 矩阵的 Python 版本集合都没有改。

## 已知限制与未验证项

- 浏览器组和自检组按整组计时；场景级计时要等 D 项。
- playwright 版本在记录里是声明依赖，没有实测值。
- 指纹不覆盖被 git 忽略的目录（例如 `.venv/`），那些算环境不算输入。
- 本地记录与 Linux CI 记录的结构一致性靠同一 schema 保证；Linux 上的实测运行要等 PR 的 CI。
- 入口不阻止两次验证并发跑。实测过一次：完整验收跑到浏览器组时，另一条命令同时在跑完整单元，浏览器组的 `serve.py` 让 `ae.py` 的进程盘点类测试失败。同机互斥归 C 项，本项只把干扰暴露出来。
- 观测到两例偶发失败，都与入口无关，复跑即过，建议各自开 issue：
  - `tests/test_pptx_fonts.py` 的 `test_pdf_tool_timeout_is_reported` 把 `TOOL_TIMEOUT` 压到 0.5 秒，整包高压下 `fc-list` 可能被判超时。
  - `tests/test_e2e_servers.py` 的 `test_stop_all_escalates_to_sigkill_when_a_service_ignores_sigterm` 用 `time.time()` 量耗时，却和 `communicate` 的单调时钟 deadline 比较；系统时钟回跳几十分之一毫秒就断言不成立（实测 4.9990859 < 5.0）。改成 `time.monotonic()` 即可。
