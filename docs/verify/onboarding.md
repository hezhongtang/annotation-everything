# 统一验证入口接入说明

这份文档给后续子项（B–H）的实施者和日常开发者看：怎么用 `verify.py`，怎么复现一份记录，记录里各字段的含义见 [record-contract.md](record-contract.md)。不需要读原始聊天记录。

## 它解决什么问题

以前完整单元、浏览器和服务安全自检都靠人拼命令，结果只有终端里滚过的文本。失败、跳过、超时、信号中断和「输入在跑的过程中被改过」在文本里分不开，也没法跨阶段比较。`verify.py` 把这三件事收到一个入口：`plan` 解释范围，`run` 执行并留下结构化记录，`report` 只读记录。选测建议默认只作影子对照；满足下文 G 项条件的本次工作会话证据可以复用，其他组实际执行。入口不并行化执行，也不删除慢项；`unit-related` 固定附带全局词典一致性契约（见下）。

入口放在仓库根目录，和 `ae.py` 并列，而不是并进 `ae.py`：`ae.py` 是发给用户的产品管理命令，输出走中英双语、围绕技能副本和本机台账；验证入口面向仓库检出，技能副本还没装也能验证，输出只要中文。两者生命周期不同，理由见 [docs/adr/0002-unified-verification-entry.md](../adr/0002-unified-verification-entry.md)。

## 两阶段工作流

执行时机以 [AGENTS.md「开发与 PR review」](../../AGENTS.md#开发与-pr-review) 为准：issue 实现阶段做快速相关单元测试与已配置的 Lint、格式检查，完成后开 PR；用户手动调用 agents 后做全量审查与验证，全部通过后才能合并。

- 快速单元优先选择直接相关的用例或类，单次反馈目标 60 秒。`unit-related` 自动附带的全局词典契约仍保留。选定范围过慢、涉及宿主状态资源，或计划要求全量回退时，记录移交范围，不在实现阶段自动启动完整检查。
- Lint 与格式化尚未配置，另开 PR 接入；当前记录「未配置」。`git diff --check` 只检查空白。纯协作文档、README 和 PR 模板改动检查文本与 diff，单元测试记录「不适用」；skill 指引、fixture 与执行配置不按文件后缀豁免。
- 已知快速检查失败先修复；超时、锁等待或环境阻塞如实记录，不能写成通过。开 PR 时列明待验证项，开发 Agent 随后结束交接。
- PR review 的门禁使用 `--group all --phase pr`：符合内置文档允许列表的纯文档 PR 记录产品测试 `not_applicable`、由 CI 兜底；其余 PR 实际执行完整单元、服务安全自检和完整浏览器。豁免边界见下文「阶段口径」及 [selection-strategy.md](selection-strategy.md)。CLI 的 `review` 阶段只用于补测或报告交接；已有快速检查证据不替代门禁要求的实际执行。CI 保留自动触发，由审查 Agent 跟进失败修复。

## 快速开始

```bash
python3 verify.py plan                      # 将执行哪些组、跑什么命令，不启动测试
python3 verify.py run --group all --phase pr # 用户发起 PR review 后：完整验收
python3 verify.py report --latest           # 读最近一次记录，不启动测试
```

纯文档 PR 仍运行同一条门禁命令。允许列表、核实依据和边界见 [selection-strategy.md](selection-strategy.md#文档允许列表核实策略-v11issue-117)；`report --latest` 显示不适用原因与 CI 责任。文档门禁记录不作为产品测试复用来源。

`run` 支持 `--json`：stdout 只输出记录 JSON，子进程输出改走 stderr，方便脚本消费。

## 验证组

| 组 | 内容 | 等价命令 | 计数 | 计时粒度 | 共享资源 |
| --- | --- | --- | --- | --- | --- |
| `unit` | 完整单元 | `python3 -m unittest discover tests` | 按用例 | 用例 | `host-state` |
| `unit-related` | 相关单元，显式指定，并自动附带全局词典契约 | `python3 -m unittest <选择器> tests/test_i18n_catalog_contract.py` | 按用例 | 用例 | 命中敏感模块时 `host-state` |
| `browser` | 完整浏览器 | `uv run --no-project --with playwright python tests/e2e_browser.py` | 按日志摘要 | 场景 | `host-state` |
| `serve-check` | 服务安全自检 | `python3 skill/annotation-everything/scripts/serve.py check` | 按日志摘要 | 整组 | 无（进程内自检） |
| `all` | 完整验收 | `unit` → `serve-check` → `browser` 依次执行 | 分别计数 | 分别计时 | 各成员组自持 |

`unit-related` 自动附带的契约：无参数的全词典一致性检查只执行一次，权威位置在 `tests/test_i18n_catalog_contract.py`（issue #51）。完整单元组经 discover 执行它；相关局部组由入口自动带上，独立运行相关模块也不丢全局保护。调用方已显式指定契约模块（含类 / 方法级选择器）时不重复附带；契约模块被删时局部组加载失败报 `error`，不会静默失去保护。各功能模块自己专有的 key、占位符、复数与接线断言仍在各自模块里。

`--group` 可以重复。组名也可以直接写在 `run` 后面：`python3 verify.py run unit`、`python3 verify.py run unit-related tests/test_core.py`。不写 `--group` 时，有选择器按 `unit-related`，否则按 `unit`。

相关单元的选择器接受三种写法：`tests/test_core.py`、`tests.test_core`、`tests.test_core.ClassName.test_method`。不给选择器就是空集合，入口返回 2 并说明原因——空集合不算通过。

## 常用跑法

```bash
# issue 实现：选择直接相关的快速单元；以下用语言逻辑模块举例
python3 verify.py run unit-related tests/test_i18n.py --phase development --timeout 60

# 看当前改动建议跑什么、为什么（不启动测试）
python3 verify.py plan --phase development

# 用户手动发起 PR review 后：符合文档允许列表时记录不适用，其余实际执行全量
python3 verify.py run --group all --phase pr --pr 50 --reason "PR review 全量验收"

# CI：完整单元 + 服务安全自检（浏览器依赖不进 CI）
python3 verify.py run --group unit --group serve-check --phase ci

# 审查阶段针对具体疑点补测：点名到类 / 用例的选择器不复用旧证据
python3 verify.py run unit-related tests.test_core.ServerTests --phase review

# 完全独立复测：绕过证据复用，全部组实际执行（原因写进记录）
python3 verify.py run --group all --phase pr --no-reuse --reason "PR review 独立复测"

# 某个组设超时，超时按 124 退出并回收子进程
python3 verify.py run --group browser --timeout 300
```

`--issue`、`--pr`、`--phase`、`--reason` 都写进记录，是后续判断「这次为什么跑」的依据。`--phase` 只接受 `issue`、`development`、`review`、`pr`、`ci`、`manual`。

`--timeout 60` 限制该组的执行时间，不代表整次调用连同排队一定在 60 秒内结束。快速阶段选择无宿主资源锁的逻辑用例；若需资源敏感验证，移交 PR review，继续遵守既有互斥协议。

## 怎么读报告

报告分段：结论、阶段与关联、提交与基线、输入指纹与环境、耗时、分组结果、影子对照、证据复用（有引用决策时）、证据。三个要点：

- 结论行保留原运行的结论和退出码；「能否作为通过证据」反映本次读取时的校验结果。日志丢失或损坏时，报告标为证据不可用并返回 1，即使原运行通过也不例外。`report --json` 同样输出当前校验结果和问题，不改写磁盘上的原记录。
- 耗时段把排队、锁等待、墙钟、入口自身开销、清理、用例耗时合计、类 / 模块级开销、未归属开销分开列。用例耗时合计是各用例之和，不是等待时间；锁等待是同机资源互斥的排队时长，单独一列，既不摊进执行耗时也不算入口开销。要看总等待看墙钟。
- 输入段说明开始与结束的指纹是否一致。指纹按完整文件内容计算，包括大文件；输入不可读时，快照标为不可用。指纹变化或任一快照不可用时，保留原测试结果，但记录不能作为覆盖最终输入的通过证据，`report` 返回 1。

## 同机并发与资源锁（C 项）

同一台机器上多个 worktree / checkout 并行验证时，资源敏感组会互相干扰（安装维护
检查的真实进程枚举会看到浏览器测试启动的 serve.py）。C 项起，这些参与方按共享
资源互斥，锁等待单独计时。要点：

- 资源敏感的范围与锁位置、超时、嵌套豁免、安全边界见 [isolation.md](isolation.md)；
  机器可读声明在 `plan --json` 的 `isolation` 字段。
- 旧直接入口（`python3 -m unittest …`、直跑 `tests/e2e_browser.py`）与新入口共用
  同一协议，没有绕过路径；纯逻辑组（如 `unit-related tests/test_i18n.py`、
  `serve-check`）不被无关锁阻塞。
- 等待上限缺省 600 秒，`AE_LOCK_WAIT_SECONDS` 可调；锁等待超时是明确的非成功
  （组 `timeout`、退出码 124），不是通过。
- 锁只协调参与者：外部进程与用户真实服务不受影响，也绝不因锁被停止。

## 阶段选测与影子对照（F 项）

选测回答「这次改动该跑什么、为什么」，影子对照保证它先被证明再被信任。要点：

- `python3 verify.py plan --phase development`（可加 `--baseline`）：解释有效差异（含新增 / 删除 / 重命名两端与未提交文件）、命中的映射规则、未知路径、回退原因与建议范围；不启动任何测试。
- `run` 默认影子模式：除符合内置文档允许列表的 PR 豁免外，实际执行仍是请求的组（缺省完整单元），建议范围写进 `context.selection`；结束时对照写进 `shadow` 字段与 `shadow.json`。候选遗漏（完整检查发现而建议范围没覆盖的失败）保留并归因，不会被丢弃，也不会自动改规则。
- 未知路径、坏基线、损坏规则、清单缺口、策略自身变化一律建议回退完整检查并解释原因；空差异不解释成「不用测」。需求阶段不自动启动测试；issue 实现按快速检查范围执行，全量回退要求移交 PR review。若效率守卫会强制扩大实际执行范围，实现阶段只读计划并记录待验证项，不绕过守卫。
- 策略版本、回退情形、允许列表边界与样本清单见 [selection-strategy.md](selection-strategy.md)；影子报告可被 G / H 直接读取（`shadow.json` + `context.selection`）。

## 证据复用与流程接入（G 项）

有效验证证据可以用于快速检查、PR review 补测与报告交接；PR review 的 `pr` 门禁按下文「阶段口径」执行。判定策略在仓库根 `verify_evidence.py`（版本化，`POLICY_VERSION` 随判定逻辑递增），不因工作流调整而改变。

- **复用判定（从紧，任何一条不满足就实际执行）**：同输入指纹（HEAD + 工作树全部有效输入的内容摘要，含 staged / unstaged / untracked）+ 同环境（OS、Python、CI 标记、配置摘要）+ 范围覆盖（请求组的测试清单 ⊆ 已验证组的清单）+ 来源记录当前校验后 `usable_as_evidence=true`（不信任落盘时的旧标记）+ 未超 24 小时保鲜窗口且结束时间不在未来 + 入口版本、选测策略版本、复用策略版本一致 + 基线一致。唯一放宽：差异被证明仅在文档允许列表内（`README.md`、`README.en.md`、`docs/assets/`、`GLOSSARY.md`、`docs/adr/`，与选测策略同一允许列表；覆盖映射下停用）时仍可复用，复用理由注明「差异仅在文档允许列表内」；无法证明独立性就回退重新验证。
- **永远不复用的组**：依赖同机宿主机状态的组（`groups[].resources` 非空——完整单元、完整浏览器、命中敏感模块的相关单元直跑）。指纹相同也不复用，必须实际执行或要求隔离证明。
- **阶段口径**：CLI 的 `issue` / `development` / `review` / `manual` 保留复用能力；这些标签本身不决定何时运行测试，执行时机服从上文工作流。用户发起 PR review 后，`pr` / `ci` 不引用旧证据。PR 的全部有效差异仅在内置文档允许列表内，且没有回退或效率守卫时，本地产品测试记为 `not_applicable`，原因写明「差异仅在文档允许列表内，由 CI 兜底」，可作为本地门禁证据；不计执行、用例、检查或通过数。其余 PR 仍实际执行完整单元、服务安全自检与完整浏览器。CI 始终执行原矩阵，本地记录不替代 Linux 双 Python CI；`--no-reuse` 可强制文档 PR 实际执行。补测与报告交接可用 `review` / `manual`。
- **按疑点复测不受限**：`unit-related` 点名到类 / 用例的选择器不复用（如 `python3 verify.py run unit-related tests.test_synth.TouchTests --phase review`）；要完全独立复测用 `--no-reuse`，绕过原因写进记录。
- **呈现与统计**：被引用的组 `outcome` 为 `reused`（引用已有验证），明确区别于「跳过（空集合）」——后者不是通过；`result.counts` 不累计引用组的组数、用例或检查。报告列出来源 run、时间、指纹、已验证范围、来源环境与理由；`report` 读取时使用与规划相同的有效性和覆盖判断，来源损坏、超期或不再满足条件时，完整性、可用性与影子结论一起降级，返回 1。
- **迁移与故障恢复**：统一入口 G 之前的记录没有复用策略版本，一律不参与复用；未经统一入口生成的旧日志更不会被当作可信记录。读取错误一律回退为全部实际执行，不把旧记录误判成功。全量回退模式始终可用：`python3 verify.py run --no-reuse`（等价于 G 之前的完整执行行为）。
- **会话口径（策略 v2）**：候选只来自同一产物根，且必须具有相同的逻辑工作会话摘要。入口按 `AE_VERIFY_SESSION`、`CODEX_THREAD_ID`、`CLAUDE_SESSION_ID` 的顺序取首个已设置的值，只保存带来源名的 SHA-256 摘要，不记录聊天身份原值。普通终端需要复用时，在同一工作会话里 `export AE_VERIFY_SESSION="$(python3 -c 'import uuid; print(uuid.uuid4())')"`，各阶段沿用该值；新任务重新生成。没有可用身份或显式设置为空时实际执行，不用目录和时间窗口代替会话身份。跨检出的记录互相不可见，不跨机器复用；同一会话仍受 24 小时保鲜上限约束。

## 重现步骤

从干净克隆开始，不需要任何额外安装：

```bash
git clone <仓库地址> && cd annotation-everything
python3 verify.py plan                      # 1. 看将执行什么
python3 verify.py run --group all --phase pr --pr 50 --reason "PR review 复现样本"
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

浏览器组的真实接入：`python3 verify.py run --group browser` 在 macOS 开发机上 557/557 通过（A 交付时点的样本，当时场景少于现在的 629 项），墙钟约 189 秒，入口自身开销 0.0006 秒。浏览器组自 D 项起按场景计时（`granularity: scenario`），逐场景的起止时间与耗时在 `groups[].scenarios` 里；记录里用例耗时合计为 0 仍是预期的——那是单元组的口径，两组不混用。

## 给 B–H 的接入点

- 记录契约已版本化，后续只在 `ae-verify-run/1` 上追加字段，升版本才改结构。字段语义和必填 / 可空规则见 [record-contract.md](record-contract.md)。
- B（清理测试与词典契约提速，已交付）：清理测试的短等待经 `tests/e2e_servers.py` 的受控参数注入（`TestCleanup(terminate_timeout=...)`、`start_server(ready_timeout=...)`），缺省仍是 5 秒 / 8 秒的产品语义；权威词典契约模块是 `tests/test_i18n_catalog_contract.py`，入口常量 `RELATED_CONTRACT_SELECTORS`（verify.py）声明局部组的固定附带，F 的组映射直接引用它。
- C（同机隔离与锁，已交付）：`groups[].resources` 与 `groups[].lock` 记录组级锁获取，`timing.lock_wait_seconds` 单列排队，`children.json` 登记语义未动；协议本体在仓库根 `host_isolation.py`，新入口要接资源锁时复用它，不要另起一套。
- D（浏览器场景，已交付）：`groups[].scenarios` 已填充场景记录，`granularity` 细化到 `scenario`；场景注册表在 `tests/e2e_scenarios.py`（纯标准库），局部执行用 `--only` 或旧参数别名，清单用 `--list`；完整模式行为矩阵不变（629=629 前后对照）。
- F（阶段选测与影子对照，已交付）：`plan --phase` 解释建议范围与回退原因；`run` 默认影子模式——`context.selection` 记录建议（策略版本、命中规则、未知路径、回退原因、建议组与场景），`shadow` 字段与运行目录 `shadow.json` 记录建议与实际执行的对照（候选遗漏、归因、不可比较原因）。策略本体与映射表在仓库根 `verify_selection.py`，语义、回退情形与样本清单见 [selection-strategy.md](selection-strategy.md) 与 `docs/verify/shadow-samples.json`。
- G（证据复用，已交付）：`run` 在执行前按 `input.fingerprint` + `environment` + 范围覆盖 + 阶段与版本口径决定哪些组引用已有验证（`reuse` 字段与 `groups[].outcome = "reused"`），只认当前校验后 `usable_as_evidence` 为 `true` 的记录，不能直接信任落盘时的通过标记；`--no-reuse` 强制全部重跑。语义见下文「证据复用与流程接入（G 项）」。
- H（持续报告，已接入）：每次运行后从索引和原始记录重建有界效率投影；`report --efficiency` 只读历史，`--rebuild` 更新投影与增量回顾。比较、缺口、CI 导入和策略守卫见 [efficiency.md](efficiency.md)；总验收结果、原始证据与限制见 [final-acceptance.md](final-acceptance.md)。

## 兼容与迁移

- 原来的直接命令继续有效，完整执行的成功 / 失败退出码语义一致：`python3 -m unittest discover tests`、`uv run --no-project --with playwright python tests/e2e_browser.py`、`python3 skill/annotation-everything/scripts/serve.py check`。
- 例外是空集合与全跳过：直接跑 `unittest` 返回 0，入口返回 1，两者都不算完整成功。
- 运行时只用 Python 标准库，语法兼容 Python 3.9。playwright 仍然只从 `tests/e2e_browser.py` 导入，入口不导入它。
- 服务行为、审阅格式、Git 钩子和 CI 矩阵的 Python 版本集合都没有改。

## 已知限制与未验证项

- 自检组按整组计时；浏览器组自 D 项起按场景计时。
- playwright 版本在记录里是声明依赖，没有实测值。
- 指纹不覆盖被 git 忽略的目录（例如 `.venv/`），那些算环境不算输入。
- 本地记录与 Linux CI 记录的结构一致性靠同一 schema 保证；Linux 上的实测运行要等 PR 的 CI。
- ~~入口不阻止两次验证并发跑~~：该干扰（完整验收的浏览器组 + 并行完整单元，`serve.py` 进入 `ae.py` 的进程盘点）已由 C 项的同机资源隔离协议治理，见 [isolation.md](isolation.md) 与 `tests/test_host_interference.py` 的复现回归；并发敏感组现在按 `host-state` 资源锁排队，锁等待在记录里单独计时。
- 观测到两例偶发失败，都与入口无关，复跑即过，建议各自开 issue：
  - `tests/test_pptx_fonts.py` 的 `test_pdf_tool_timeout_is_reported` 把 `TOOL_TIMEOUT` 压到 0.5 秒，整包高压下 `fc-list` 可能被判超时。
  - `tests/test_e2e_servers.py` 的升级终止测试曾用 `time.time()` 量耗时，和 `communicate` 的单调时钟 deadline 比较；issue #51 改造时已换成 `time.monotonic()`，并把该测试改为注入短超时。

## 组内进程隔离与串行回退

完整范围与原资源锁保持不变。已审查的独立任务最多使用 3 个进程，未知和敏感单元模块继续串行；场景默认不允许并行。`AE_VERIFY_SERIAL=1` 恢复串行，执行方式与失败保留规则见 [parallel-execution.md](parallel-execution.md)。
