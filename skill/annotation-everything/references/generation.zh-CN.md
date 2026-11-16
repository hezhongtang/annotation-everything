# 报告生成：默认范式（中文）

[English](generation.en.md) · [中文操作流程](workflow.zh-CN.md) · [输入与输出格式](formats.md) · [共用安全与协议约束](protocol.md)

只安装 AE 时，从这里开始把原始材料变成可审阅的初稿。本指引是默认范式；用户指定了其他生成技能时按其流程执行，产物仍然走同一套检查（第 5 步）。生成推理、材料读取与内容判断由你（Agent）执行；AE 提供流程、基础样式、产物约定与确定规则检查。

## 生成方法：内置范式与外部生成技能

- **没有指定、也没有扩展需要**：用本指引（内置范式），记录 `method` 写 `{"kind": "builtin", "name": "annotation-everything 内置范式", "version": "<AE 版本>", "unavailable": []}`。
- **用户明确指定了生成技能，且它可用**：**实际调用该技能**（按[各 Agent 的对应机制](agent-mechanisms.zh-CN.md)的技能调用方式找到并执行它），按它的流程生成；外部生成技能**同样受本指引第 0 步对齐契约与第 2–5 步产物约定约束**——生成前同样完成九维度对齐与问答，遵循信息密度、视觉规范、任务导向导航与补充页规则，产物仍须遵守逐页 `<section class="page">`、稳定 `data-key`、可选讲稿、`ae-report-record/1` 记录并走第 5 步同一核验入口、按同一清单验收。**扫描到候选或名称出现不等于已调用**：宣称采用了指定技能，就必须真的调用过，记录 `method.invocation` 写清怎么调用的、产物从哪来；核验入口会检查这一声明（`record.method`），缺依据按程序失败暴露。
- **指定的技能不可用、未启用或调用失败**：明确说明，不能假称已使用该技能或已满足其能力要求。改用内置范式继续时，记录 `method.requested` 写用户指定的方法、`note` 写原因；拿不准是否继续就先问用户。
- **能力缺口**：外部技能缺读取、版式、对齐或核验能力时，把缺口写进 `method.unavailable`；产物缺来源、口径、偏离披露或检查证据就写进 `gaps` 与 `deviations`，**不放宽验收**——外部产物与内置产物在同一缺陷下的判定完全一致。
- **额外交付形式**（PPTX / PDF / Word 等）：由外部能力提供，AE 核心不内置通用导出器。实际得到的产物写进记录（可用 `links.extras`），未满足的部分写进 `gaps`；核心可审阅 HTML 仍按第 6 步进 AE，额外格式不取代共同验收依据。
- **不自动搜索或安装远程技能**：生成技能由用户安装在受控位置；不用联网方式寻找、下载或安装生成技能，也不为接入外部技能放宽服务边界。

## 0. 明确需求：九维度检查与生成前问答对齐

在生成前检查并明确下面九个维度（缺项时只补问影响结果的项）：

| 维度 | 要明确什么 | 记录与默认处理线索 |
|---|---|---|
| 1. **用途与预期行动** | 帮读者做什么判断、决策或后续行动 | 写入 `request.purpose` |
| 2. **读者与阅读方式** | 谁来读；演讲配合、独立细读还是逐条审核 | 决定叙述口径与第 5/7 项 |
| 3. **核心问题与内容优先级** | 优先回答哪些问题，哪些细节退到补充页 | 写入 `request.coreQuestions` |
| 4. **材料范围与证据口径** | 纳入哪些材料；数字、统计期间与引用定位口径 | 在第 1 步逐文件登记 |
| 5. **信息密度与篇幅约束** | `演示为主` / `独立阅读` / `深入审阅`；页数约束属**硬上限**还是**参考目标**、补充页是否计入 | 写入 `request.length`、`request.lengthSpec` 与 `request.alignment.density` |
| 6. **视觉风格与参考依据** | 模板/品牌规范/参考样张；色板、字号层级、留白与图表风格的具体规则 | 写入 `request.alignment.visual`（抽象要求须先拆成可核对约定） |
| 7. **导航与查找任务** | 如何支撑「概览把握」「板块定位」与「结论 ↔ 证据/补充页双向跳转」 | 写入 `request.alignment.navigation` |
| 8. **使用环境与交付形式** | 独立 HTML 与 AE 对照页（`1280×720` Shadow DOM 缩放）阅读环境、附加交付格式 | 写入 `request.alignment.environment` |
| 9. **讲稿与首次审阅重点** | 是否需要讲稿（`yes` / `no`）；首次进入对照页想先核对什么 | 决定第 3 步做不做，并写入 `request.script` / `alignment.script` |

### 问答对齐与来源记录规则

- **复用与委派优先**：用户已明确说出、材料/参考模板可追溯提供、或用户明确委派（如「由 Agent 决定」）的维度直接沿用并写明依据（`reused` / `delegated` + `basis`），不重复追问。**全部九项均有明确要求、可追溯依据或委派时，可以一题不问。**
- **必须调用问答工具**：只要**信息密度、视觉风格、导航设计、讲稿选择或其他影响结果的缺项**没有明确可复用依据或委派，生成前**必须实际调用当前宿主可用的问答工具**（按[各 Agent 的对应机制](agent-mechanisms.zh-CN.md)选择工具），将缺项整合成**一轮提问**（按宿主每批问题数上限合理分组）；每题提供用户易懂的选项、**推荐项与简短理由**，并保留自由补充空间。
- **分别记录来源与状态**：信息密度（`density`）、视觉风格（`visual`）、导航设计（`navigation`）在 `request.alignment` 里**分别记录**值（`value`）、来源（`source`：`user` / `reused` / `delegated` / `default` / `inferred`）与状态（`status`：`confirmed` / `pending` / `conflict` / `deviated`）。**从既有表述推断的偏好不得标成 `user`**（用户明确选择），必须标为 `inferred` 或 `default` 并写明简短 `reason`；`delegated` 必须写明采用的方案与 `basis`。
- **问答工具不可用**：先向用户说明具体不可用原因，再用纯文本提问，并在 `request.qa` 中记 `available: false` 与非空 `unavailableReason`。
- **异步问答等待与超时兜底**：发出问答后可先执行不依赖答案的独立准备（如材料读取与读取范围登记），并给用户合理的等待窗口回复；若仍未回复，偏好项**不阻塞生成**——由 Agent 按可用材料与任务目标选定明确默认方案，标为 `source: "default"`、`status: "pending"`（「待确认」）后继续连续生成，并在摘要和后续审阅清单中显式标出 `pending` 项。
- **内部冲突提示与未解决留痕**：同一组要求内部存在矛盾时（如「每页极简大留白/高密度完整表」与「不超过 N 页硬上限」同时成立），必须提示冲突并请用户取舍；**尚未解决的冲突**把对应项记为 `status: "conflict"`，并在 `content.requirements` 等对应验收项保持未完成状态（`not_run` / `fail`），**不得标为 `pass` 或全部满足**。
- **连续生成与可选内页校准**：需求对齐后连续生成初稿与核验记录，**不设提纲确认或视觉样稿审批**。仅当用户**明确提出**要确认版式或做样张校准时，才触发可选的**代表性内页校准**（先出正文典型页与图表典型页各 1 张供确认，再展开全本）。

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
- **按对齐的信息密度组织内容**：
  - `演示为主`：每页 1 个核心判断配少量大字号要点，细节依据进讲稿或补充页；
  - `独立阅读`：不依赖讲稿，每页自带完整主谓结论、必要上下文、口径与 `.ae-source`，单页可读且不超一屏负荷；
  - `深入审阅`：系统化证据对照、边界条件与口径推导按分区卡片与明确层级排布（`report-base.css` 提供 `.ae-cols-2` / `.ae-card` / `.ae-supplement` 等类），确保可扫读且不溢出。
  - **无论有无讲稿**，正文都保持独立理解完整；补充页在 `<section class="page">` 上标注 **`data-kind="supplement"`**，并与 `request.lengthSpec` 的约束性质（`hard` 硬上限 / `reference` 参考目标）与 `includeSupplements` 保持一致。
- **落实具象视觉约定**：从用户模板、品牌规范或参考材料中**提取可核对规则**（色板变量、字号层级、网格列数、留白基线、图表与配图风格），把「专业 / 简洁」等抽象词拆成字号、边距、对比度与卡片规则；委派 Agent 决定时写明采用的基础样式或默认规则。
- **面向三类任务设计导航**：① 目录页（`.toc-row` 带 `data-range-start`/`data-range-end`、`toc-label`/`toc-title`）与摘要共同支撑**全貌把握**；② 可识别的板块名与页标题支撑**按主题定位**；③ **结论 → 证据或补充页 → 对应分析**的双向跳转：结论处链接 `href="#page-<key>"`，证据/补充页提供明确返回原分析页的 `href="#page-<key>"` 链接（对照页同时支持 `data-page-ref` 兜底与顶栏「返回上一浏览页」），所有锚点与稳定 `data-key` 绑定，页序重排与 `--rebuild` 后仍准确对应。
- 逐页结构与对照页约定一致：`<section class="page" data-page="N" data-key="键">`，页码从 1 连续，**`data-key` 用稳定唯一的语义键**（如 `sum`、`rev`、`sup-method`），不要用 `p3` 这类页码键——重建时批注与跨页跳转都靠它找回页面。
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
  "summary": "三季度收入增长主要来自新店，外卖占比上升与原料涨价挤压毛利。四季度需优先改善老店坪效。",
  "request": {"purpose": "季度经营汇报", "audience": "管理层",
              "coreQuestions": ["收入为什么增长"], "length": "6 页", "script": "yes",
              "lengthSpec": {"nature": "hard", "includeSupplements": false},
              "qa": {"used": true, "tool": "ask_question",
                     "asked": ["density", "visual", "navigation"], "unanswered": []},
              "alignment": {
                "density": {"value": "standalone", "source": "user", "status": "confirmed"},
                "visual": {"value": "report-base 默认中性可读版式（正文 22px，边距 64×72px）",
                           "source": "delegated", "status": "confirmed",
                           "basis": "用户委派 Agent 决定，采用内置可读性变量与双列网格"},
                "navigation": {"value": "summary-toc + conclusion-to-evidence",
                               "source": "reused", "status": "confirmed",
                               "reason": "复用管理层季度汇报标准三步查找任务"}}},
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
  "checks": [{"id": "content.requirements", "status": "not_run",
              "scope": "全部需求", "note": ""},
             {"id": "content.claims", "status": "not_run",
              "scope": "全部关键结论", "note": ""},
             {"id": "content.fact-vs-inference", "status": "not_run",
              "scope": "全部结论页", "note": ""},
             {"id": "content.units-dates", "status": "not_run",
              "scope": "全部数字", "note": ""},
             {"id": "content.numbers-charts", "status": "not_run",
              "scope": "数字与图表", "note": ""},
             {"id": "content.cross-page", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "content.script-consistency", "status": "not_run",
              "scope": "报告与讲稿", "note": ""},
             {"id": "content.open-questions", "status": "not_run",
              "scope": "openItems", "note": ""},
             {"id": "structure.flow", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.headings", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.claim-support", "status": "not_run",
              "scope": "全部结论页", "note": ""},
             {"id": "structure.dup-gaps", "status": "not_run",
              "scope": "全部页", "note": ""},
             {"id": "structure.length", "status": "not_run",
              "scope": "篇幅与讲稿时长", "note": ""},
             {"id": "visual.pages", "status": "not_run",
              "scope": "全部 6 页", "note": ""}],
  "openItems": [{"item": "提价幅度二选一", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

字段语义：

- `summary`：新生成的报告必须写一两句摘要，概括报告内容和要解决的问题，供审阅总览识别报告。值必须是字符串；兼容旧记录时可省略，schema 仍为 `ae-report-record/1`。总览同时显示 `request.purpose`、`request.audience` 和 `request.coreQuestions`；没有摘要时显示「（无摘要）」和封面页标题。
- `request`：第 0 步的需求；问过的与沿用的都写。可选扩展字段（保留 `ae-report-record/1` 向后兼容，未包含这些字段的旧记录判为未记录偏好并继续有效）：
  - `request.alignment`：分别记录 `density`、`visual`、`navigation` 等维度的 `value`、`source`（`user` / `reused` / `delegated` / `default` / `inferred`）与 `status`（`confirmed` / `pending` / `conflict` / `deviated`），以及 `basis` / `reason`。
  - `request.lengthSpec`：`{"nature": "hard" | "reference", "includeSupplements": true | false, "pages"?: "..."}`，指明约束性质与 `data-kind="supplement"` 补充页是否计入。
  - `request.qa`：`{"used": bool, "available"?: bool, "tool"?: str, "unavailableReason"?: str, "asked": [...], "unanswered": [...]}`。
- `deviations`（可选）：`[{"item": "...", "reason": "...", "impact"?: "..."}]`，如实披露未能完全按对齐约束执行的项与原因，不写成全部满足。
- `sources`：第 1 步的读取登记；`status` 用 `read` / `partial` / `unreadable`；`coverage` 写实际读到的范围，`partial` 必须同时写明已读与未读部分；`partial` / `unreadable` 必须写 `reason`；**`path` 相对报告所在目录解析**——核验入口按报告所在目录找材料文件核对哈希；`locations` 列定位线索（PDF 页码、Word 段落、工作表与单元格）；`sha256` 用 `sha256sum` 或等价方式算材料内容哈希——核验入口用它发现材料事后变化，材料改了旧核验结果就不再算数。**全部实质材料都 `unreadable` 时初稿不能算完成**，核验入口会判 `record.reading` 未通过。
- `method`：实际生成方法；`kind` 用 `builtin` / `external`（封闭枚举），`name` 写实际采用的方法名，`version` 写可得的版本信息。外部技能必须写 `invocation`（怎么调用的、产物从哪来——扫描到候选或名称出现不算采用，缺它核验入口会判 `record.method` 失败）；用户指定但未实际采用的技能写 `requested` 与 `note`（原因）；能力缺口写 `unavailable`。核验入口核对这些声明并把方法信息带进摘要与结构化结果。外部技能示例：`{"kind": "external", "name": "synthetic-brief-kit", "version": "1.4.2", "invocation": "运行其 scripts/make_report.py 生成", "unavailable": []}`。
- `links`：产物关联与 sha256（`sha256sum` 或等价方式计算）；这是核验入口判断记录是否对应当前产物的依据。
- `findings`：关键结论（含数值与影响判断）与来源、报告页的对应；`locations` 可列材料内定位（如 `q3.pdf 第 2 页`、`Sheet1!B3`），让审阅人能核对原文；`refs` 写结构化定位：`path` 与 `sources[].path` 同基准（相对报告所在目录，供审阅人定位材料原文），`quote` 保留材料原文（不翻译、不改写），`locator` 写位置（章节、行、表名），`found` 如实标是否定位到；`support` 写材料是否支持表述——`supported`（支持）/ `partial`（部分支持，表述强于材料）/ `unsupported`（不受支持）/ `unverified`（未核实），**引用存在不等于受支持，核对后不支持就必须标 `unsupported`**；`basis` 写检查依据：怎么核对的、看了材料哪几处。能声明输入与口径的计算写 `calculation`（运算支持 `sum`、`difference`、`product`、`ratio`、`percent-change`），并带齐四要素：`unit` 单位、`period` 期间、`definition` 口径、`inputLocations` 每个输入的位置；缺要素的计算核验入口会标为**待核实**，复算不一致会定位到结论与报告页。
- `gaps`：材料与需求缺口。`openItems`：待用户拍板的事项，影响核心结论的 `blocking: true`。
- `checks`：内容（`content.*`，八项清单见下）、结构（`structure.*`，五项清单见下）与视觉（`visual.pages`）核验的逐项声明，状态 `pass` / `fail` / `not_run` / `unavailable`；条目写 `id` 与 `status`（加 `scope` / `note`），内容 / 结构 / 视觉的类别由核验入口按内置清单 id 归类，条目自带的类别字段不参与判定。**没执行的写 `not_run`，不能写 pass。** 要判可交付时，三类清单必须逐项 `pass`，视觉项还要带覆盖全部报告页的逐页证据（见下面「视觉核验」）；无讲稿时讲稿关联的两项（`content.script-consistency`、`structure.length`）不适用、不必声明，核验入口也不把它们计入覆盖总数——声明了 pass 属于虚假的已检查，会按程序检查失败暴露。有关键结论缺支持、口径不明的计算或材料没哈希时，核验入口会把内容与结构覆盖降级，不让它显示为通过。
- `verdict.draftComplete`：结构完整、能进入审阅即为 `true`；`verdict.deliverable` 只有在内容、结构与视觉核验都通过且无阻塞事项时才 `true`，否则留 `null` 或 `false`。**初稿生成完成 ≠ 报告可交付。**
- `revision`（可选，改稿维护时写）：本次改稿核验信息的声明。`basis` 写影响范围怎么判定的（改动落在哪些页、哪些来源、是否涉及讲稿与跨页引用）；`rechecked` 列出本次重新核验过的检查项 id，每项都必须在 `checks` 里有实际结果（`pass` / `fail`）；`pending` 列出无法确认影响范围的待核验项，每项带 `scope`（范围）与 `reason`（原因）——核验入口会让有 `pending` 的记录到不了可交付，并在摘要里如实列出待核验范围。没有 `revision` 的记录（初次生成、旧报告）不受影响。字段语义见[中文操作流程](workflow.zh-CN.md)「改稿后的核验信息维护」。

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

- 用**实际渲染**逐页查看（同时覆盖独立报告页及 AE 对照页 `1280×720` Shadow DOM 缩放展示环境），每页检查五类问题：**溢出**（内容超出画布）、**遮挡**（元素互相压盖）、**文字与图表可读性**（字号、对比度，以及长文本/表格/图例真实可读性）、**字体**（按基础样式渲染）、**内容缺失**（空白页、丢段、图表无数据）。
- 检查结果写进 `visual.pages` 声明的 `pages` 证据，每页一条：

```json
{"id": "visual.pages", "status": "pass",
 "scope": "全部 3 页（报告与对照页 1280×720 Shadow DOM 实际渲染）", "note": "逐页渲染检查",
 "pages": [{"page": 1, "checked": true, "issues": [], "viewport": "1280x720"},
           {"page": 2, "checked": true, "issues": []},
           {"page": 3, "checked": true, "issues": []}]}
```

- 声明 `pass` 就必须有覆盖**全部报告页**的证据：缺页、有页 `checked: false`、或带着未解决的问题声明 pass，核验入口都会判否（`record.visual`）。
- 无法渲染或缺少查看能力写 `unavailable`（`note` 说明缺什么）；检查中断时把中断页如实记 `checked: false`、整体状态写 `not_run`（标明未检查受影响页）；发现的问题如实写进 `issues` 并把状态写 `fail`——程序检查通过不代替视觉检查通过，未解决的问题是可交付判定的阻塞项。
- 反例必须判否：**声明已逐页检查但记录缺页覆盖**、**带溢出却声明通过**，都不能通过整体交付验收。

## 5. 运行公开核验入口

```bash
python3 $S/report_check.py 报告.html --script 讲稿.md --record report-record.json --locale zh-CN
```

- `--json` 输出 `ae-report-check/1` 结构化结果；退出码 0 表示程序检查全部通过（**不代表可交付**），1 表示有检查未通过，2 表示输入或用法错误。
- 程序检查覆盖：报告可解析、页面键唯一、讲稿逐页对应、记录结构与版本、记录对应当前产物、引用页存在、计算可复算、检查集合完整、读取登记与缺口披露、结论支持映射（support 与 refs 一致、无不受支持的结论）、材料关联（登记映射与 sha256 新鲜度）、视觉证据一致性（声明逐页检查通过就要有覆盖全部报告页的证据）、篇幅约束（`record.length`，支持 `request.lengthSpec` 的硬上限/参考目标与 `data-kind="supplement"` 补充页计数）、改稿维护声明（`revision.rechecked` 与记录检查结果一致，`pending` 披露完整）、对齐偏好与偏离披露（`record.alignment`，校验来源、委派依据、问答工具不可用原因、`pending`/内部冲突与偏离项）、跨页导航与目录范围（`report.navigation`，校验 `#page-<key>`、`data-page-ref` 与 `data-range-start`/`data-range-end` 有效）。程序不执行材料中的代码，不渲染页面，也不推断任意业务口径。摘要里会给出读取汇总（已读 / 部分读取 / 无法读取）、具体未读范围、记录披露的缺口、对齐待确认项/冲突/偏离项（旧记录标未记录偏好）与已检查范围；改稿维护过的记录还会列出本次重新核验的范围与待核验范围。
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
