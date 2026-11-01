# 验证记录字段契约

`verify.py` 是仓库的统一验证入口：`plan` 解释将执行什么，`run` 执行并留下结构化记录，`report` 只读已有结果。这份文档固定记录的结构、字段语义和必填 / 可空规则。后续子项（B–H）在同一个 schema 上追加字段，不重新定义已有字段的含义。

当前版本：`ae-verify-run/1`（`verify.py` 入口版本 1）。schema 字符串进记录，也进文件名；改结构就升版本号，旧记录由 `report` 判为「不支持的记录 schema」，不静默当成新结构读。

## 落点与文件

记录写在仓库根目录的 `.verify/`（已被 `.gitignore` 忽略）。每次运行一个目录：

```
.verify/
  latest.json                  最近一次运行的 run ID 与开始时间
  index.jsonl                  每次运行一行的摘要索引（追加写）
  runs/<run ID>/
    record.json                结构化记录；先写 state=running，结束时改成 complete 或 aborted
    children.json              本次入口自己拉起的子进程登记簿
    <组名>.log                  子进程原始输出（stdout + stderr 合并）
    <组名>.cases.json           用例级计时明细（仅 unittest 计数的组）
```

`AE_VERIFY_DIR` 或 `--out-dir` 可以改根目录；CI 用默认值，再把 `.verify/` 作为构件上传。记录里不放本机绝对路径：解释器写成 `python3`，入口写成 `verify.py`，仓库内选择器写成相对路径，仓库外产物位置用占位符表示。日志与明细只记运行目录内的相对名；脱敏只影响记录，不改变实际执行路径。

## 字段分组

落盘记录的顶层字段按下列语义分组。组内字段只增不改；删字段或改含义要升 schema 版本。`report` 输出另附本次校验信息，见「证据可用性」。

### 标识

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema` | 字符串 | 是 | 固定 `ae-verify-run/1` |
| `wrapper_version` | 整数 | 是 | 入口版本，与 schema 同步升 |
| `state` | 字符串 | 是 | `running` / `complete` / `aborted`，见「状态机」 |
| `run_id` | 字符串 | 是 | UTC 时间戳加随机后缀，同一秒内也不重复 |
| `started_at` / `ended_at` | ISO 8601 字符串 | `started_at` 必填，`ended_at` 未结束时为 `null` | 带本地时区偏移 |
| `wrapper` | 对象 | 是 | 入口名、版本、schema、实际 argv（已脱敏）、`cwd_mode` 固定 `repo-root`、Python 版本 |
| `context` | 对象 | 是 | `phase`、`issue`、`pr`、`reason`、`groups_requested`、`groups_selected`、`selectors`、`selectors_effective` |
| `groups` | 数组 | 是 | 实际执行的组，按执行顺序；每项含命令、结论、计数、计时与用例清单 |

`context.issue` 与 `context.pr` 可空：没关联就写 `null`，不写 0。`context.phase` 只接受 `issue`、`development`、`review`、`pr`、`ci`、`manual`，其余值入口直接报错。`context.reason` 可空，但建议写，它是后续判断「这次为什么跑」的唯一来源。`context.selectors` 只记调用方给出的选择器；`context.selectors_effective` 记实际生效的选择器——`unit-related` 会固定附带全局词典一致性契约模块（issue #51），附带与去重都在这里可见。

### 输入

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `input.available` | 布尔 | 是 | 能否取到 git 输入快照并读取全部有效输入的内容 |
| `input.reason` | 字符串 | 可取时为 `null` | 取不到快照的原因 |
| `input.fingerprint` | 字符串 | 快照可用时必填 | 运行开始时被测输入的内容指纹 |
| `input.final_fingerprint` | 字符串或 `null` | 是 | 运行结束时的指纹；未结束或结束快照不可用时为 `null` |
| `input.fingerprint_scope` | 字符串 | 是 | 指纹覆盖范围的固定说明 |
| `input.baseline` | 对象 | 是 | `commit`、`source`（`explicit` / `merge-base(<ref>)` / `head`）；解析不了时 `commit` 为 `null` |
| `input.changed_since_baseline` | 数组 | 可空 | 相对基线的变更路径，未跟踪文件并入；取不到为 `null` |
| `input.at_start` / `input.at_end` | 对象 | 各必填 | 完整快照，见下 |
| `input.changed_during_run` | 布尔或 `null` | 是 | 开始与结束的指纹是否不同；未结束或任一快照不可用时为 `null` |
| `input.covers_final_input` | 布尔或 `null` | 是 | 本次记录能否覆盖最终输入；未结束或任一快照不可用时为 `null` |
| `input.stale_reason` | 字符串 | 未失效为 `null` | 失效原因 |

快照对象里 `staged`、`unstaged`、`untracked` 是三个路径数组，`counts` 是各自的条数。`unreadable` 列出无法读取内容的有效输入；只要该列表非空，快照的 `available` 就为 `false`，不能作为通过证据。`skipped_large` 保留以兼容已有 v1 读取者；新记录固定为空数组，不再因文件大小跳过内容哈希。数组里只有仓库相对路径，没有文件内容。

指纹的算法：`sha256(HEAD + 每个非忽略变更路径的内容摘要)`。每个文件都以流式 SHA-256 读取完整内容，包括大文件和未跟踪文件；同样大小的内容改动也会改变指纹。范围覆盖源码、测试、fixture、模板、词典和执行配置——只要有改动就进指纹；被 git 忽略的路径不是仓库输入，不参与；`.verify/` 永远排除，避免记录自身污染指纹。重命名时旧路径记 `absent`。

### 环境

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `environment.os` | 对象 | 是 | `system`、`release`、`machine` |
| `environment.python` | 对象 | 是 | `version`、`implementation`、`matches_wrapper`（子进程与入口同一解释器时为 `true`） |
| `environment.host` | 对象 | 是 | `cpu_count`、`timezone` |
| `environment.ci` | 对象 | 是 | `is_ci`、`provider`、`runner_os`；非 CI 时后两个为 `null` |
| `environment.config_summary` | 对象 | 是 | 只记 `ae_home_set`、`ae_locale`、`ae_verify_dir_set` 三个布尔或非敏感值 |
| `environment.isolation` | 对象 | 是 | 环境隔离方式（C 项起有值）：`mechanism`（`flock`）、`lock_dir_source`（`AE_LOCK_DIR` / `XDG_RUNTIME_DIR` / `tmpdir`，只记来源类别不记路径）、`resources`、`note`。协议详情见 [isolation.md](isolation.md) |

环境里不记环境变量原值、不记路径、不记凭据。浏览器组的 playwright 版本由 `uv` 在运行时解析，入口不导入它，因此记为声明依赖而不是实测版本；D、E 两项可以在组记录里补实测值。

### 时间

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timing.queued_at` | ISO 8601 | 是 | 入口被调用的时刻 |
| `timing.started_at` / `timing.ended_at` | ISO 8601 | `started_at` 必填 | 第一个组开始前 / 最后一个组结束后 |
| `timing.queue_seconds` | 浮点秒 | 是 | 排队与准备开销 |
| `timing.wall_seconds` | 浮点秒 | 是 | 从开始到结束的墙钟，用户等待的下界 |
| `timing.lock_wait_seconds` | 浮点秒 | 是 | 各组等待共享资源锁的时间之和（C 项起有值）；单列为排队时长，不计入 `wrapper_seconds`，也不摊进执行耗时 |
| `timing.wrapper_seconds` | 浮点秒 | 是 | 入口自身开销，等于墙钟减去各组子进程耗时与锁等待 |
| `timing.cleanup_seconds` | 浮点秒 | 是 | 中断或入口异常后回收登记子进程的累计耗时；正常完成为 0 |
| `timing.case_seconds_total` | 浮点秒 | 是 | 各用例耗时之和，不等于墙钟 |
| `timing.fixture_seconds_total` | 浮点秒 | 是 | 类 / 模块级准备与清理之和 |
| `timing.unattributed_seconds` | 浮点秒 | 是 | 同类相邻用例之间的空隙，明确标成未归属 |
| `timing.attribution_note` | 字符串 | 是 | 固定说明，提醒合计不是等待时间 |

计时归属规则：

- `cleanup_seconds` 是回收阶段的单独观测值，已经包含在对应组的 `child_seconds`
  或组外收尾的 `wrapper_seconds` 中，不与它们相加；重复进入收尾时累计实际回收耗时。
- 单个用例的耗时从 `startTest` 到 `stopTest`，含该用例自己的 `setUp` / `tearDown`。
- 类 / 模块级 `setUpClass`、`setUpModule`、`tearDownClass`、`tearDownModule` 发生在两个用例之间，计入两者之间的空隙，按 `run-start`、`class-boundary`、`run-end`、`same-class` 四类归入 `fixture_seconds_total`；不摊到任何单个用例，也不重复累计。
- `same-class` 空隙是同类相邻用例之间的时间，本应是零；非零时单独计入 `unattributed_seconds`，不冒充 fixture。
- 各组的空隙明细写在 `<组名>.cases.json` 的 `gaps` 数组里，每条带 `kind`、`from`、`to`、`seconds`；`seconds.gap_total` 与逐条之和一致。
- 不用并行累计值冒充用户等待：`case_seconds_total` 明确标为合计，`wall_seconds` 才是等待时间。

### 结果

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `result.outcome` | 字符串 | 是 | 结论，见「结论与优先级」 |
| `result.exit_status` | 整数 | 是 | 入口的退出码 |
| `result.counts` | 对象 | 是 | `groups`、`cases`、`checks` 与通过 / 失败 / 错误 / 跳过 / 中断 / 预期失败 / 意外成功；子测试和 fixture 计数单列，见下 |
| `result.summary` | 字符串 | 是 | 一行中文摘要 |
| `result.usable_as_evidence` | 布尔 | 是 | 能否作为通过证据，见「证据可用性」 |
| `result.notes` | 数组 | 是 | 需要人看的说明 |

组记录里 `counter` 标明计数方式：`unittest` 表示按用例计数，`log` 表示从日志末尾的「N/M 通过」摘要行计数，`composite` 表示展开组。`granularity` 标明计时粒度：`case`、`group` 或 `scenario`。浏览器组（`counter=log`）的子进程在「N/M 通过」摘要后输出一行 `AE-SCENARIOS: <JSON>` 场景执行清单；入口解析该行并填进 `groups[].scenarios`，每项含 `id`（注册表稳定场景 ID）、`status`（`passed` / `failed` / `error` / `interrupted` / `not_run`）、`checks`（`total` / `passed` / `failed`）、`started_at`、`ended_at`、`seconds`，场景出错时另带 `message`；`granularity` 相应从 `group` 细化为 `scenario`。`not_run` 表示因提前停止（如中断）没有执行的场景，不算通过。日志里没有该行时保持空列表与整组粒度，兼容不带场景清单的旧输出。场景注册表与选择见 `docs/verify/browser-scenarios.md`。

组记录另有两个隔离字段（C 项起有值，见 [isolation.md](isolation.md)）：`resources` 是组要求的共享资源名数组（空数组即纯逻辑组）；`lock` 记录本次锁获取——`resources`、`wait_seconds`（等待时长，锁等待不是执行耗时）与可选的 `timeout: true`（等待超时，组没有执行、结论为 `timeout`，note 写明资源与持有者 pid）。

用例记录在 `groups[].cases`，每项含 `id`、`name`、`status`、`seconds`；非通过时加 `message`，发生子测试失败、错误或跳过时，分别带 `subtest_failures`、`subtest_errors`、`subtest_skips`。`status` 取 `passed`、`failed`、`error`、`skipped`、`interrupted`、`expected_failure`、`unexpected_success`。

单元组的 `counts.cases` 统计已执行的主用例，`passed`、`failed`、`errors`、`skipped` 等计数严格按主用例状态统计。同一主用例有多个子测试失败或错误时，不重复增加主用例数。子测试失败、错误和跳过分别计入 `subtest_failures`、`subtest_errors` 和 `subtest_skips`，不额外计成独立用例。全部子测试跳过的主用例记为 `skipped`；子测试有成功和跳过、没有失败或错误时，主用例记为 `passed`。

类 / 模块级 fixture 错误和跳过分别计入 `fixture_errors`、`fixture_skips`，不计入 `cases` 或主用例的 `errors`、`skipped`；`fixture_errors` 非零仍使组结论为 `error`。汇总计数分别累加各组的主用例、子测试和 fixture 计数。

unittest 执行期间捕获中断时，已完成用例保留原结果，中断中的用例标为 `interrupted`。组计数和汇总计数中的 `interrupted` 分别记录中断的主用例数。显式 `unit-related` 选择器的模块加载会传播 `KeyboardInterrupt`，入口将组结论记为 `interrupted`，退出码为 130；尚未执行的用例不计入 `cases`。

完整 `unit` 使用标准库的 `discover`：模块导入期间的 `KeyboardInterrupt` 会被包装成 `_FailedTest` 导入错误，记录为 `error`、退出码为 1。入口保留直接执行 `unittest discover` 的既有行为，不改写加载器。

### 证据

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `evidence.integrity` | 字符串 | 是 | `ok` 或 `degraded` |
| `evidence.record_path` | 字符串 | 是 | 固定 `record.json` |
| `evidence.logs` | 数组 | 是 | 每项含 `group`、`path`、`bytes`、`sha256`；文件缺失时为 `missing: true` |
| `evidence.cases` | 数组 | 是 | 用例明细的定位与条数，仅 unittest 计数的组 |
| `evidence.notes` | 数组 | 是 | 证据侧的说明 |

`children.json` 是子进程登记簿，每项含 `group`、`pid`、`pgid`、`started_at`、`reaped`、`exit_status`、`signal`。它只登记本次入口自己拉起的进程；入口不去盘点、不去结束任何未登记进程。登记或元数据写入失败时，入口退出前仍回收本次已登记且尚未结束的子进程。

## 结论与优先级

组结论 `outcome` 的取值和触发条件：

| 取值 | 触发条件 |
| --- | --- |
| `passed` | 子进程退出码 0，且有用例或检查被执行，且没有全部跳过 |
| `failed` | 有用例失败或意外成功；`log` 计数组退出码非 0 |
| `error` | 有用例错误、fixture 错误、加载失败，或计时证据缺失 |
| `skipped` | 全部用例被跳过 |
| `empty` | 用例或检查数量为 0 |
| `timeout` | 超过 `--timeout` 被入口终止；或共享资源锁等待超过上限（C 项起，组没有启动子进程，note 写明等待的资源与持有者） |
| `interrupted` | 入口在等锁或组执行期间收到终止信号；子进程被信号终止；或 unittest / 浏览器入口捕获 `SIGINT` / `KeyboardInterrupt` 后以 130 退出。保留已执行用例与场景记录，尚未开始的场景记为 `not_run` |
| `unavailable` | 命令无法启动，例如找不到 `uv` |
| `wrapper_error` | 入口自身没能完成登记，例如记录写不进去 |

整次运行的结论按下面的顺序取第一个出现的：`wrapper_error`、`unavailable`、`error`、`failed`、`timeout`、`interrupted`、`skipped`、`empty`、`passed`。计数分开表示，结论只是汇总；一次运行同时有失败和错误时结论是 `error`，两个数都在 `result.counts` 里。

## 状态机

- `running`：入口已启动并写下记录，但还没有结束记录。父进程被 `SIGKILL` 或写记录中途被打断时，记录停在这个状态。
- `complete`：正常跑完，无论结论是通过还是失败。
- `aborted`：入口收到 `SIGINT` / `SIGTERM` / `SIGHUP`，已回收登记的子进程后写下记录。

只有 `complete` 且结论 `passed` 且覆盖最终输入且证据完整的记录才算可用的通过证据。`running` 与 `aborted` 都不是通过，也不会被后来的运行改写成通过。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部组完整成功 |
| 1 | 有失败、错误、空集合、全部跳过，或证据不可用 |
| 124 | 组超时（与 `timeout(1)` 一致） |
| 2 | 入口自身失败：参数用错、依赖缺失、记录写不进去 |
| 128 + 信号值 | 入口被信号中断 |

完整执行的成功 / 失败退出码语义与直接跑等价命令一致：`python3 -m unittest discover tests` 成功返回 0、有用例失败返回 1。例外是空集合与全跳过：直接跑 `unittest` 返回 0，入口返回 1，因为两者都不能显示为完整成功。

## 证据可用性

`report` 只读记录，不启动产品测试，也不改写落盘的 `record.json`。文本与 JSON 输出都反映本次读取时的证据校验结果，保留原运行的 `result.outcome` 和 `result.exit_status`。发现完整性问题时，输出中的 `result.usable_as_evidence` 为 `false`、`evidence.integrity` 为 `degraded`，并列出校验问题；不能仅凭原记录的通过标记判断证据可用。

JSON 报告在记录副本上追加 `report_validation.problems` 字符串数组，列出本次读取发现的问题；没有问题时为空数组。该字段只存在于报告输出，不写回原记录。

下面几种情况都判为证据不可用，`report` 退出码为 1，不静默成功：

- 运行目录或 `record.json` 不存在，说明这次运行没有留下结束记录；
- `record.json` 是半写或损坏的 JSON；
- `schema` 不是本入口认识的版本；
- `state` 不是 `complete`；
- 日志文件缺失，或内容校验和与记录里不一致。

元数据写失败的优先级：子进程的退出码优先。测试已经失败时，记录写不进去不会把退出码改成 0；测试通过但记录写不进去，入口返回 2 并明确报告。

## 扩展点

B–H 在同一个 schema 上追加，不改已有字段：

- 全局词典契约（B，已交付）：无参数全词典检查的唯一权威位置是 `tests/test_i18n_catalog_contract.py`；`unit-related` 组固定附带它（`verify.py` 的 `RELATED_CONTRACT_SELECTORS`），契约模块缺失时局部组加载失败报 `error`，不会静默失去保护。F 的组映射直接引用这一模块与常量。
- 组级隔离与锁（C，已交付）：`groups[].resources`、`groups[].lock`、`timing.lock_wait_seconds`、`timing.cleanup_seconds`、`environment.isolation`；`children.json` 的所有权登记保持不变。
- 浏览器场景拆分（D，已交付）：`groups[].scenarios` 由浏览器入口的场景执行清单（日志里的 `AE-SCENARIOS:` 行）填充，`granularity` 细化到 `scenario`；场景注册表、选择与旧参数别名见 `docs/verify/browser-scenarios.md`。
- 阶段选测与影子对照（F）：在 `context` 与 `groups[]` 加策略版本与选择理由；`input.fingerprint` 与 `changed_since_baseline` 已经够了，不需要新字段。
- 证据复用（G）：按 `input.fingerprint`、`environment`、`groups[].name` 与 `context.phase` 匹配已有记录；通过判定使用当前校验后的 `usable_as_evidence`，不能跳过证据完整性校验。
- 持续报告（H）：读 `index.jsonl` 汇总，不新增记录字段。

## 已知限制

- 自检组按整组计时；浏览器组已细化到场景级（D），单个场景内部的检查仍按整场景合并计时。
- playwright 版本记为声明依赖，没有实测值。
- 指纹只覆盖 git 认识的路径与被跟踪内容；被忽略的目录（例如 `.venv/`）属于环境，不算输入。
- `wrapper_seconds` 是入口自身开销的实测值，不含子进程启动时操作系统层面的抖动。
