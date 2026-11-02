# 报告生成：默认范式（中文）

[English](generation.en.md) · [中文操作流程](workflow.zh-CN.md) · [输入与输出格式](formats.md) · [共用安全与协议约束](protocol.md)

只安装 AE 时，从这里开始把原始材料变成可审阅的初稿。本指引是默认范式；用户指定了其他生成技能时按其流程执行，产物仍然走同一套检查（第 5 步）。生成推理、材料读取与内容判断由你（Agent）执行；AE 提供流程、基础样式、产物约定与确定规则检查。

## 生成方法：内置范式与外部生成技能

- **没有指定、也没有扩展需要**：用本指引（内置范式），记录 `method` 写 `{"kind": "builtin", "name": "annotation-everything 内置范式", "version": "<AE 版本>", "unavailable": []}`。
- **用户明确指定了生成技能，且它可用**：**实际调用该技能**（按[各 Agent 的对应机制](agent-mechanisms.zh-CN.md)的技能调用方式找到并执行它），按它的流程生成；产物仍须遵守第 2–4 步的约定（逐页 `<section class="page">`、稳定 `data-key`、可选讲稿、`ae-report-record/1` 记录），并走第 5 步同一核验入口、按同一清单验收。**扫描到候选或名称出现不等于已调用**：宣称采用了指定技能，就必须真的调用过，记录 `method.invocation` 写清怎么调用的、产物从哪来；核验入口会检查这一声明（`record.method`），缺依据按程序失败暴露。
- **指定的技能不可用、未启用或调用失败**：明确说明，不能假称已使用该技能或已满足其能力要求。改用内置范式继续时，记录 `method.requested` 写用户指定的方法、`note` 写原因；拿不准是否继续就先问用户。
- **能力缺口**：外部技能缺读取、版式或核验能力时，把缺口写进 `method.unavailable`；产物缺来源、口径或检查证据就按缺口披露（`gaps`），**不放宽验收**——外部产物与内置产物在同一缺陷下的判定完全一致。
- **额外交付形式**（PPTX / PDF / Word 等）：由外部能力提供，AE 核心不内置通用导出器。实际得到的产物写进记录（可用 `links.extras`），未满足的部分写进 `gaps`；核心可审阅 HTML 仍按第 6 步进 AE，额外格式不取代共同验收依据。
- **不自动搜索或安装远程技能**：生成技能由用户安装在受控位置；不用联网方式寻找、下载或安装生成技能，也不为接入外部技能放宽服务边界。

## 0. 明确需求：只补问影响结果的缺项

需要明确六件事：**用途、读者、核心问题、篇幅或时长、材料范围、要不要讲稿**。

- 对话里已经说过的直接沿用，不要重复问。
- 只补问缺失且影响结果的项；把缺项凑成一次提问，只问一次。
- **讲稿选择没说时先问清**（要 / 不要），这决定第 3 步做不做。
- 需求明确后连续生成，**不设提纲确认或视觉样稿审批**；缺项 unanswered 时在记录的 `gaps` 里披露，不要停在路上等确认。
- 六项都有合理解释时（例如材料本身就是一份结构完整的汇报稿），可以全部沿用并在记录里写明来源，一条都不问。

## 1. 读取材料并登记范围

- 逐个文件登记：`read`（已读全文）、`partial`（部分读取，说明哪部分）、`unreadable`（读不了，说明原因）。
- **格式名称不代表读取成功**：PDF 打开了但扫描页没 OCR、表格没读到隐藏列，都按实际读到的范围写。
- 读取工具缺失或能力不足时如实披露，不要用缺失内容补造事实。

### 读取编排与能力边界

- 优先用宿主实际可用的读取能力，不为读取引入第三方依赖或联网转换服务。常见材料成功路径需要宿主具备：**PDF** 提取文本层并保留页码；**Word**（.docx 等）读正文段落与表格；**表格**（.xlsx / .csv）读工作表与单元格——公式读到的要么是计算结果、要么是公式文本，注明是哪种。
- 定位信息沿流程保留：提取事实时记下它来自 **PDF 第几页、Word 第几段、哪个工作表哪些单元格**，写进报告的 `.ae-source` 标注和记录的 `sources[].locations` 与 `findings[].locations`，审阅人才能核对原文。
- 表格里的**空白、缺失、数值零、单位与期间**是四件事：空白单元格不写成 0，缺失值不当作不存在，0 不当作缺失，单位与统计期间跟数字一起保留。未读取的公式、图表、隐藏行列或受保护区域如影响结论，列入 `gaps`。

### 能力不足时的登记

- 读取工具缺失：`unreadable`，`reason` 写明缺什么能力（如「宿主无 PDF 文本提取工具」）。
- 部分提取失败：`partial`，`coverage` 同时写明已读与未读范围（如「第 1–8 页已读；第 9–12 页为扫描页，未识别」），`reason` 写原因；只写「文件已打开」不算登记。
- **全部实质材料都不可读**：停下来说明材料缺口和可继续的条件（换可读格式、安装读取工具或提供文字版），不要生成只有空结构或虚构内容的报告并宣布初稿完成——核验入口会判 `record.reading-blocked`，初稿不算完成。

## 2. 组织报告并生成逐页 HTML

- 章节按用途组织，报告必须回应用户的核心问题；回应不了的列入缺口。
- 逐页结构与对照页约定一致：`<section class="page" data-page="N" data-key="键">`，页码从 1 连续，**`data-key` 用稳定唯一的语义键**（如 `sum`、`rev`），不要用 `p3` 这类页码键——重建时批注靠它找回页面。
- 版面按 1280×720 设计。**基础样式随技能发布**：把技能副本 `references/report-base.css` 的内容内联进报告的 `<style>`（对照页按 Shadow DOM 收样式，`$S` 指技能副本的 `scripts/`，样式在它的上一级 `references/` 里）。可以在变量范围内调整颜色与字号，不要删掉 `.ae-source`、`.ae-gap`、`.ae-todo` 这些语义类。
- 关键结论、数值和影响判断在页面上保留依据标注（`.ae-source` 写来源位置）；未核实的内容用 `.ae-todo` 标记；读取缺口写成 `.ae-gap` 块或单独的缺口页。
- 数字保留日期、单位、口径和计算依据；计算结果要能从材料复算，口径四要素（单位、期间、口径、输入位置）写进核验记录的 `calculation`（见第 4 步）。
- 报告 `<title>` 写一个看得懂的标题。

## 3. 讲稿（用户要才生成）

- 中文写 `### 第 N 页｜标题` 加**时长 / 讲法 / 过渡 / 可能被问**，英文写 `### Page N｜Title`；字段细节见[输入与输出格式](formats.md)。
- 页码与报告页序一一对应；不要讲稿的场合不要补造空讲稿来应付检查。

## 4. 写核验记录（`ae-report-record/1`）

报告旁边保存 `report-record.json`（与报告同目录，即材料旁），内容是版本化、语言无关的结构化记录：

```json
{
  "schema": "ae-report-record/1",
  "generatedAt": "2026-11-02T09:00:00+08:00",
  "request": {"purpose": "季度经营汇报", "audience": "管理层",
              "coreQuestions": ["收入为什么增长"], "length": "6 页", "script": "yes"},
  "sources": [{"path": "materials/q3.md", "status": "read", "coverage": "全文",
               "sha256": "<材料文件 sha256>",
               "locations": ["q3.md 第 1–3 段"], "note": ""},
              {"path": "materials/q3-scan.pdf", "status": "partial",
               "coverage": "第 1–3 页文本层已读；第 4–5 页为扫描页，未识别",
               "locations": ["q3-scan.pdf 第 1–3 页"], "reason": "宿主无 OCR 能力",
               "sha256": "<材料文件 sha256>"}],
  "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
             "version": "0.9.0", "unavailable": []},
  "links": {"report": "report.html", "script": "script.md",
            "sha256": {"report": "<报告文件 sha256>", "script": "<讲稿文件 sha256>"},
            "sourceNote": "材料位置说明"},
  "findings": [{"claim": "收入同比增长 12%", "type": "calculation",
                "sources": ["materials/q3.md"], "locations": ["q3.md 第 2 段"], "pages": ["sum"],
                "refs": [{"path": "materials/q3.md", "locator": "§1 收入",
                          "quote": "去年同期为 100 万元", "found": true}],
                "support": "supported",
                "basis": "对比 §1 两期收入原文并复算，未见口径冲突",
                "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                "expected": 12,
                                "unit": "万元", "period": "2025Q3 对 2024Q3",
                                "definition": "对上年同期收入的增长百分比",
                                "inputLocations": {"a": "materials/q3.md §1 上年同期 100 万元",
                                                   "b": "materials/q3.md §1 三季度收入 112 万元"}},
                "note": "口径：对上年同期"}],
  "gaps": ["十月周报缺失"],
  "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
              "scope": "全部需求", "note": ""},
             {"id": "content.claims", "kind": "content", "status": "not_run",
              "scope": "全部关键结论", "note": ""},
             {"id": "content.fact-vs-inference", "kind": "content", "status": "not_run",
              "scope": "全部结论页", "note": ""},
             {"id": "content.units-dates", "kind": "content", "status": "not_run",
              "scope": "全部数字", "note": ""},
             {"id": "content.numbers-charts", "kind": "content", "status": "not_run",
              "scope": "数字与图表", "note": ""},
             {"id": "content.cross-page", "kind": "content", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "content.script-consistency", "kind": "content", "status": "not_run",
              "scope": "报告与讲稿", "note": ""},
             {"id": "content.open-questions", "kind": "content", "status": "not_run",
              "scope": "openItems", "note": ""},
             {"id": "structure.flow", "kind": "structure", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.headings", "kind": "structure", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.claim-support", "kind": "structure", "status": "not_run",
              "scope": "全部结论页", "note": ""},
             {"id": "structure.dup-gaps", "kind": "structure", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.length", "kind": "structure", "status": "not_run",
              "scope": "篇幅与讲稿时长", "note": ""},
             {"id": "visual.pages", "kind": "visual", "status": "not_run",
              "scope": "全部 6 页", "note": ""}],
  "openItems": [{"item": "提价幅度二选一", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

字段语义：

- `request`：第 0 步的需求；问过的与沿用的都写。
- `sources`：第 1 步的读取登记；`status` 用 `read` / `partial` / `unreadable`；`coverage` 写实际读到的范围，`partial` 必须同时写明已读与未读部分；`partial` / `unreadable` 必须写 `reason`；`locations` 列定位线索（PDF 页码、Word 段落、工作表与单元格）；`sha256` 用 `sha256sum` 或等价方式算材料内容哈希——核验入口用它发现材料事后变化，材料改了旧核验结果就不再算数。**全部实质材料都 `unreadable` 时初稿不能算完成**，核验入口会判 `record.reading` 未通过。
- `method`：实际生成方法；`kind` 用 `builtin` / `external`（封闭枚举），`name` 写实际采用的方法名，`version` 写可得的版本信息。外部技能必须写 `invocation`（怎么调用的、产物从哪来——扫描到候选或名称出现不算采用，缺它核验入口会判 `record.method` 失败）；用户指定但未实际采用的技能写 `requested` 与 `note`（原因）；能力缺口写 `unavailable`。核验入口核对这些声明并把方法信息带进摘要与结构化结果。外部技能示例：`{"kind": "external", "name": "synthetic-brief-kit", "version": "1.4.2", "invocation": "运行其 scripts/make_report.py 生成", "unavailable": []}`。
- `links`：产物关联与 sha256（`sha256sum` 或等价方式计算）；这是核验入口判断记录是否对应当前产物的依据。
- `findings`：关键结论（含数值与影响判断）与来源、报告页的对应；`locations` 可列材料内定位（如 `q3.pdf 第 2 页`、`Sheet1!B3`），让审阅人能核对原文；`refs` 写结构化定位：`quote` 保留材料原文（不翻译、不改写），`locator` 写位置（章节、行、表名），`found` 如实标是否定位到；`support` 写材料是否支持表述——`supported`（支持）/ `partial`（部分支持，表述强于材料）/ `unsupported`（不受支持）/ `unverified`（未核实），**引用存在不等于受支持，核对后不支持就必须标 `unsupported`**；`basis` 写检查依据：怎么核对的、看了材料哪几处。能声明输入与口径的计算写 `calculation`（运算支持 `sum`、`difference`、`product`、`ratio`、`percent-change`），并带齐四要素：`unit` 单位、`period` 期间、`definition` 口径、`inputLocations` 每个输入的位置；缺要素的计算核验入口会标为**待核实**，复算不一致会定位到结论与报告页。
- `gaps`：材料与需求缺口。`openItems`：待用户拍板的事项，影响核心结论的 `blocking: true`。
- `checks`：内容（`content.*`，八项清单见下）、结构（`structure.*`，五项清单见下）与视觉（`visual.pages`）核验的逐项声明，状态 `pass` / `fail` / `not_run` / `unavailable`。**没执行的写 `not_run`，不能写 pass。** 要判可交付时，三类清单必须逐项 `pass`，视觉项还要带覆盖全部报告页的逐页证据（见下面「视觉核验」）；无讲稿时讲稿关联的两项（`content.script-consistency`、`structure.length`）不适用、不必声明，核验入口也不把它们计入覆盖总数——声明了 pass 属于虚假的已检查，会按程序检查失败暴露。有关键结论缺支持、口径不明的计算或材料没哈希时，核验入口会把内容与结构覆盖降级，不让它显示为通过。
- `verdict.draftComplete`：结构完整、能进入审阅即为 `true`；`verdict.deliverable` 只有在内容、结构与视觉核验都通过且无阻塞事项时才 `true`，否则留 `null` 或 `false`。**初稿生成完成 ≠ 报告可交付。**

### 内容核验清单（`content.*` 八项：逐项核对，逐项写 scope / note）

| id | 查什么 | note 写什么 |
|---|---|---|
| `content.requirements` | 需求与核心问题是否逐条被回应，或明确列入缺口 | 哪些需求查了、缺哪些 |
| `content.claims` | 关键结论的依据与支持程度：逐条 findings 有 refs、support、basis | 结论数、标 unsupported / partial 的及原因 |
| `content.fact-vs-inference` | 事实与推断分开：推断、预期不能写成已确认事实 | 改写过的表述 |
| `content.units-dates` | 日期、单位、口径、期间逐个核对（八月不能写成十月，万元不能写成元） | 发现的混淆与改法 |
| `content.numbers-charts` | 数字与图表一致，图表数据能在材料中找到 | 图表清单与数据来源 |
| `content.cross-page` | 跨页数字与结论一致，无互相矛盾的口径 | 冲突的页与裁决 |
| `content.script-consistency` | 报告与讲稿逐页的说法、数字一致 | 两边同步改动处 |
| `content.open-questions` | 未解决问题对结论的影响已记录（openItems、blocking 判定） | 判为 blocking 的理由 |

- 每项都要有实际核对动作，`note` 写查了什么、发现了什么；一个笼统的「已检查」不算数。
- 典型反例：**引用位置存在但结论不受支持**（材料只说华东增长，报告写成全国增长）必须标 `support: "unsupported"`，内容核验不通过；漏记依据（结论没有 refs / basis）按待核实处理；日期或单位混淆、过度推断都是 fail——先改稿，再重跑核验入口。
- 需要判断的项目保留依据与检查说明；这八项由你执行，核验入口只核对记录声明与确定规则，不能替你证明提炼准确。

### 结构核验清单（`structure.*` 五项：逐项核对，逐项写 scope / note）

| id | 查什么 | note 写什么 |
|---|---|---|
| `structure.flow` | 叙述顺序与章节回应核心问题：先答核心问题的内容在前，铺垫不过量，读起来是同一条线 | 页序安排及理由 |
| `structure.headings` | 每页标题与该页内容匹配，不夸大、不跑题 | 改过的标题及原因 |
| `structure.claim-support` | 结论页有相邻的依据（`.ae-source`、数据或图表），结论不被孤立摆放 | 缺支撑的页与补法 |
| `structure.dup-gaps` | 影响理解的重复与缺口：重复的数字或段落合并，关键信息不缺页 | 合并了什么、补了什么 |
| `structure.length` | 篇幅或时长符合 `request.length`；有讲稿时逐页核对时长 | 实际页数 / 时长与约束的关系 |

- 适用性规则：**无讲稿时**，`content.script-consistency` 与 `structure.length` 的讲稿部分不适用——两项不必声明，核验入口也不把它们计入覆盖总数；声明了 pass 就是虚假的已检查，程序检查会判否。篇幅约束本身仍由核验入口按 `request.length` 里的页数直接核对（`record.length`），可解析为页数而页数对不上会明确报错。

### 视觉核验（`visual.pages`：逐页实际渲染，证据随声明交付）

- 用**实际渲染**逐页查看（浏览器打开报告，或按 1280×720 逐页截图），每页检查五类问题：**溢出**（内容超出画布）、**遮挡**（元素互相压盖）、**文字与图表可读性**（字号、对比度）、**字体**（按基础样式渲染）、**内容缺失**（空白页、丢段、图表无数据）。
- 检查结果写进 `visual.pages` 声明的 `pages` 证据，每页一条：

```json
{"id": "visual.pages", "kind": "visual", "status": "pass",
 "scope": "全部 3 页（1280×720 实际渲染）", "note": "逐页渲染检查",
 "pages": [{"page": 1, "checked": true, "issues": [], "viewport": "1280x720"},
           {"page": 2, "checked": true, "issues": []},
           {"page": 3, "checked": true, "issues": []}]}
```

- 声明 `pass` 就必须有覆盖**全部报告页**的证据：缺页、有页 `checked: false`、或带着未解决的问题声明 pass，核验入口都会判否（`record.visual`）。
- 无法渲染或缺少查看能力写 `unavailable`（`note` 说明缺什么）；检查中断时把中断页如实记 `checked: false`、整体状态写 `not_run`；发现的问题如实写进 `issues` 并把状态写 `fail`——程序检查通过不代替视觉检查通过，未解决的问题是可交付判定的阻塞项。
- 反例必须判否：**声明已逐页检查但记录缺页覆盖**、**带溢出却声明通过**，都不能通过整体交付验收。

## 5. 运行公开核验入口

```bash
python3 $S/report_check.py 报告.html --script 讲稿.md --record report-record.json --locale zh-CN
```

- `--json` 输出 `ae-report-check/1` 结构化结果；退出码 0 表示程序检查全部通过（**不代表可交付**），1 表示有检查未通过，2 表示输入或用法错误。
- 程序检查覆盖：报告可解析、页面键唯一、讲稿逐页对应、记录结构与版本、记录对应当前产物、引用页存在、计算可复算、检查集合完整、读取登记与缺口披露、结论支持映射（support 与 refs 一致、无不受支持的结论）、材料关联（登记映射与 sha256 新鲜度）、视觉证据一致性（声明逐页检查通过就要有覆盖全部报告页的证据）、篇幅约束（`request.length` 可解析为页数时与报告页数比对）。程序不执行材料中的代码，不渲染页面，也不推断任意业务口径。摘要里会给出读取汇总（已读 / 部分读取 / 无法读取）、具体未读范围、记录披露的缺口与已检查范围。
- 有 `fail` 就修对应产物或记录再重跑；摘要里的「内容核验 / 结构核验 / 视觉核验」覆盖情况来自记录，未执行不会显示为通过；结论缺支持声明、计算待核实、材料没哈希或材料变了，覆盖会被降级并写明原因。
- 向用户回报时引用这份摘要：初稿状态、覆盖、缺口与待办、记录文件位置。**记录未判定可交付之前，不要声称报告可交付。**

## 6. 进入审阅

产物（报告、讲稿、记录、审阅目录）都放在材料旁：

```bash
python3 $S/build_viewer.py --report 报告.html --script 讲稿.md --out 审阅目录 --name "审阅名"
python3 $S/serve.py 审阅目录
```

之后的等待、批注、改稿、重建都按[中文操作流程](workflow.zh-CN.md)第 2 节起执行。已有报告没有核验记录的，直接进审阅，不需要补建记录。

## 边界

- 不嵌入模型服务；生成由你完成。视觉逐页检查由你（或测试中的浏览器）执行，核验入口只做确定规则判定。
- **初稿生成完成 ≠ 报告可交付**：内容、结构与视觉核验没执行的，如实写 `not_run`，不能提前声称「报告可交付」；程序检查通过也不代表可交付。
- 核验记录和产物留在受控材料位置，不进仓库、不进技能副本。
