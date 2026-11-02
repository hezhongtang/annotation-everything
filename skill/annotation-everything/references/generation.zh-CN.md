# 报告生成：默认范式（中文）

[English](generation.en.md) · [中文操作流程](workflow.zh-CN.md) · [输入与输出格式](formats.md) · [共用安全与协议约束](protocol.md)

只安装 AE 时，从这里开始把原始材料变成可审阅的初稿。本指引是默认范式；用户指定了其他生成技能时按其流程执行，产物仍然走同一套检查（第 5 步）。生成推理、材料读取与内容判断由你（Agent）执行；AE 提供流程、基础样式、产物约定与确定规则检查。

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
- 数字保留日期、单位、口径和计算依据；计算结果要能从材料复算。
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
               "locations": ["q3.md 第 1–3 段"], "note": ""},
              {"path": "materials/q3-scan.pdf", "status": "partial",
               "coverage": "第 1–3 页文本层已读；第 4–5 页为扫描页，未识别",
               "locations": ["q3-scan.pdf 第 1–3 页"], "reason": "宿主无 OCR 能力"}],
  "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
             "version": "0.9.0", "unavailable": []},
  "links": {"report": "report.html", "script": "script.md",
            "sha256": {"report": "<报告文件 sha256>", "script": "<讲稿文件 sha256>"},
            "sourceNote": "材料位置说明"},
  "findings": [{"claim": "收入同比增长 12%", "type": "calculation",
                "sources": ["materials/q3.md"], "locations": ["q3.md 第 2 段"], "pages": ["sum"],
                "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                "expected": 12},
                "note": "口径：对上年同期"}],
  "gaps": ["十月周报缺失"],
  "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
              "scope": "全部需求", "note": ""},
             {"id": "visual.pages", "kind": "visual", "status": "not_run",
              "scope": "全部 6 页", "note": ""}],
  "openItems": [{"item": "提价幅度二选一", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

字段语义：

- `request`：第 0 步的需求；问过的与沿用的都写。
- `sources`：第 1 步的读取登记；`status` 用 `read` / `partial` / `unreadable`；`coverage` 写实际读到的范围，`partial` 必须同时写明已读与未读部分；`partial` / `unreadable` 必须写 `reason`；`locations` 列定位线索（PDF 页码、Word 段落、工作表与单元格）。**全部实质材料都 `unreadable` 时初稿不能算完成**，核验入口会判 `record.reading` 未通过。
- `method`：实际生成方法；外部技能写它的名称与可得版本信息，扫描到候选不算采用。
- `links`：产物关联与 sha256（`sha256sum` 或等价方式计算）；这是核验入口判断记录是否对应当前产物的依据。
- `findings`：关键结论与来源、页面的对应；`locations` 可列材料内定位（如 `q3.pdf 第 2 页`、`Sheet1!B3`），让审阅人能核对原文；能声明输入与口径的计算写 `calculation`（支持 `sum`、`difference`、`product`、`ratio`、`percent-change`），核验入口会复算。
- `gaps`：材料与需求缺口。`openItems`：待用户拍板的事项，影响核心结论的 `blocking: true`。
- `checks`：内容（`content.*`）与视觉（`visual.*`）核验的逐项声明，状态 `pass` / `fail` / `not_run` / `unavailable`。**没执行的写 `not_run`，不能写 pass。** 本切片至少声明 `content.requirements` 与 `visual.pages` 两项。
- `verdict.draftComplete`：结构完整、能进入审阅即为 `true`；`verdict.deliverable` 只有在内容与视觉核验都通过且无阻塞事项时才 `true`，否则留 `null` 或 `false`。**初稿生成完成 ≠ 报告可交付。**

## 5. 运行公开核验入口

```bash
python3 $S/report_check.py 报告.html --script 讲稿.md --record report-record.json --locale zh-CN
```

- `--json` 输出 `ae-report-check/1` 结构化结果；退出码 0 表示程序检查全部通过（**不代表可交付**），1 表示有检查未通过，2 表示输入或用法错误。
- 程序检查覆盖：报告可解析、页面键唯一、讲稿逐页对应、记录结构与版本、记录对应当前产物、引用页存在、计算可复算、检查集合完整、读取登记与缺口披露。摘要里会给出读取汇总（已读 / 部分读取 / 无法读取）与具体未读范围。
- 有 `fail` 就修对应产物或记录再重跑；摘要里的「内容核验 / 视觉核验」覆盖情况来自记录，未执行不会显示为通过。
- 向用户回报时引用这份摘要：初稿状态、缺口、未执行的核验。**记录未判定可交付之前，不要声称报告可交付。**

## 6. 进入审阅

产物（报告、讲稿、记录、审阅目录）都放在材料旁：

```bash
python3 $S/build_viewer.py --report 报告.html --script 讲稿.md --out 审阅目录 --name "审阅名"
python3 $S/serve.py 审阅目录
```

之后的等待、批注、改稿、重建都按[中文操作流程](workflow.zh-CN.md)第 2 节起执行。已有报告没有核验记录的，直接进审阅，不需要补建记录。

## 边界

- 不嵌入模型服务；生成由你完成。
- 本切片交付的是「初稿生成完成」：内容核验与视觉核验没执行的，如实写 `not_run`，不能提前声称「报告可交付」。
- 核验记录和产物留在受控材料位置，不进仓库、不进技能副本。
