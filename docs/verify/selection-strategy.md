# 阶段选测与影子对照策略（issue #55）

`verify.py` 的选测策略层回答一个问题：**当前改动应该跑哪些验证、为什么、什么时候必须回退完整检查**。策略本体在仓库根 `verify_selection.py`（纯标准库，与 `host_isolation.py` 同级），本入口默认工作在**影子模式**：建议范围只记录不裁剪，实际执行仍是请求的完整门禁。本项不降低当前仓库的任何要求。

## 用法

```bash
# 看当前改动建议跑什么、为什么（不启动任何测试）
python3 verify.py plan --phase development
python3 verify.py plan --phase development --baseline <SHA> --json   # 结构化计划带 selection 字段

# 影子模式 run：实际执行仍是请求的组；记录里带建议范围与对照结果
python3 verify.py run --group unit --group serve-check --phase development --issue 55

# 读对照结果（record.json 的 shadow 字段，或独立文件）
python3 verify.py report --latest
cat .verify/runs/<run ID>/shadow.json
```

`--phase` 接受 `issue`、`development`、`review`、`pr`、`ci`、`manual`（与 run 的既有语义一致）；`--baseline` 显式指定基线提交，缺省用 merge-base(origin/main)。

## 阶段策略

| 阶段 | 建议范围 | 说明 |
| --- | --- | --- |
| `issue` | 不自动启动测试（`runs_tests=false`） | 需求 / 分诊阶段写 issue 不触发测试；缺陷复现显式请求 `run unit-related <选择器>`。这不是「不用测」。 |
| `development` / `review` / `manual` | 命中映射规则的 `unit-related`（自动附带全局词典契约）+ 命中服务边界时加 `serve-check` + 浏览器场景（计划层） | 有可信映射才给局部建议。 |
| `pr` | `all`（unit → serve-check → browser） | PR 收敛完成完整验收；双 Python 全量与 CI 责任由 CI 承担。 |
| `ci` | `unit` + `serve-check` | 与现有 CI 矩阵一致；浏览器不在 CI 矩阵。 |

任何阶段下，映射给不出可信建议时都回退完整检查（见下）。回退是「建议=完整检查」，不是「不测」；回退同时保留已命中规则的信息——命中服务边界时建议仍带 `serve-check`，场景计划（计划层）也一并保留，回退解释的是「为什么要完整检查」，不是「已命中的信息可以丢弃」。

## 映射表（版本化显式声明）

映射是 `verify_selection.py` 里的受版本控制声明，不用 CodeGraph / AST 索引做判定依据。每条规则带 `why`，计划里逐条展示命中：

- `SELECTION_RULES`：路径前缀 → 建议选择器 / 浏览器场景 / 是否带 `serve-check` / 命中即回退的类别。初始覆盖：`ae.py`（CLI）、`notes.py` / `notes_store.py` / `ledger.py`(审阅协议与存储)、`serve.py` / `viewer_template.html` / `build_viewer.py`（服务与页面）、`match.py` / `skill_sources.py`（页面匹配与技能发现）、`i18n.py` 与两处 `locales/`（词典，显式含全局契约模块）、`export_format.py` / `check_pptx_fonts.py`（导出与字体）；`skill/annotation-everything/SKILL.md`、`references/`、`evals/`（技能资源，回退）、`fixtures/`（回退）、`verify.py` 等执行配置（回退）、`docs/` 等说明文档（回退）。
- `DOC_ALLOWLIST`（狭窄允许列表）：`README.md`、`README.en.md`、`docs/assets/`。命中后产品测试标记「不适用」——影子模式实际执行不因此减少任何检查，不会增加通过数量。
- 浏览器场景 `*` 仅在该规则命中时展开为注册场景清单，未命中的规则不会扩大浏览器建议。
- `STRATEGY_VERSION`：映射或判定逻辑变化时递增；记录里的 `context.selection.strategy_version` 标明计划用的版本。修改映射走普通 PR（受审查的策略变更）。
- `AE_SELECTION_MAP` 环境变量可指向同结构的 JSON 覆盖映射（测试与实验用）；覆盖映射损坏按 `broken_rules` 回退。路径不进记录。

`tests/` 的处理内建，不进映射表：修改已有 `tests/test_*.py` 建议运行它自身；新增 / 删除 / 重命名测试触发清单覆盖检查并回退（见下）。

## 保守回退（全部显式解释原因）

| kind | 触发 |
| --- | --- |
| `input_unavailable` | git 快照或有效输入读不出来 |
| `bad_baseline` | 基线缺失或解析不了（含显式 `--baseline` 写错） |
| `broken_rules` | 映射结构损坏、场景 ID 不在注册表、覆盖映射解析失败 |
| `incomplete_manifest` | 映射选择器指向不存在的测试文件、场景注册表缺失、全局契约模块缺失、新增 / 删除 / 重命名的测试未纳入映射 |
| `unknown_path` | 路径不在任何规则或允许列表（含改名伪装进允许列表、未提交的新文件） |
| `strategy_change` | `verify.py` / `verify_selection.py` / `host_isolation.py` / `.github/` / `tools/` 自身变化：映射边界不可信 |
| `infrastructure_change` | `tests/` 下非测试文件的新增、删除、重命名或修改 |
| `shared_resource` | fixture、技能指引与评估资源：影响面不可静态判定，不能按扩展名放行 |
| `guarded_doc` | 不在允许列表内的说明文档 |
| `no_changes` | 相对基线没有差异：不解释成「不用测」，保守按完整检查 |

空差异、解析失败、未知路径一律不产生「零用例成功」。

## 影子对照

每次 `run` 开始时计算建议（`context.selection`），结束时把建议与实际执行对照（`record.json` 的 `shadow` 字段 + 运行目录的 `shadow.json`，schema `ae-verify-shadow/1`）：

- **可比条件**：输入快照可用、运行期间输入指纹未变、运行正常结束且整次结论不是超时 / 入口错误 / 依赖缺失、实际执行含完整单元且有可对应的真实单元执行用例（允许断言失败；超时 / 空集合 / 全跳过 / 加载错误不可比较）。不满足时 `comparable=false` 并写明「不可比较」原因——输入不匹配、未运行完整单元或执行证据损坏时，不会错误产生选择遗漏；整次结论与证据可用性同时记录在 `shadow.run_outcome` / `shadow.usable_as_evidence`。
- **遗漏判断**：可比且未回退时，实际失败（用例失败 / 意外成功 / 服务自检失败 / 浏览器场景失败）落在建议范围外才成为候选遗漏（`candidate_omissions`），每条带归因：`selector-gap`（建议范围没覆盖）或 `infrastructure`（加载 / fixture 错误）。`all`、`unit` 和 `browser` 按完整组覆盖判断，不能把这些组已覆盖的失败误报为遗漏。回退完整检查或不可比较时不产出遗漏判断，实际失败清单仍保留。
- **候选遗漏不会被丢弃**：完整检查的失败原样保留在组结果与 `shadow.failures` 里；`rule_candidates` 给出修正映射规则的候选。确认遗漏后扩大相关范围，缩小门禁必须走受审查的策略变更。
- **浏览器范围**：浏览器组未执行时（例如并行工单占用浏览器资源，run 只跑 `unit + serve-check`），`coverage.browser=false` 明示，建议的场景清单仅在计划层对照（`browser_planned_scenarios`），不构成遗漏证据，也不构成通过。

## 对照样本

`docs/verify/shadow-samples.json` 记录已完成的对照样本（真实与合成分别标注，schema `ae-verify-shadow-samples/1`），供后续 G / H 读取。真实样本在真实改动上运行完整门禁；合成样本在合成仓库生成，补齐类别覆盖——合成样本不冒充生产漏测率，漏测率统计属 H 项。
