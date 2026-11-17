# PPTX 额外交付：内置制作与核验（中文）

[English](generation-pptx.en.md) · [报告生成默认范式](generation.zh-CN.md) · [中文操作流程](workflow.zh-CN.md)

使用者在生成阶段明确要求演示文稿时，AE 随技能提供制作流程、辅助工具、产物约定与确定规则检查；你（宿主 Agent）执行生成推理、内容判断与逐页看图。成功路径不以安装其他演示文稿技能为前提；网页版报告仍按[默认范式](generation.zh-CN.md)生成、核验并进入审阅，PPTX 只是登记在案的额外交付，不代替网页验收。没有要求演示文稿时，沿用现有流程，不新增依赖。

生成前的用途、读者、篇幅、讲稿选择与视觉对齐仍按默认范式第 0 步执行；**已确认的品牌色、字体与版式优先**，不被本文示例配色覆盖。

## 责任边界

- AE 提供：`scripts/pptx_build.py`（制作器，纯标准库）、`scripts/pptx_check.py`（文件层检查）、`scripts/pptx_render.py`（渲染辅助）、deck 规格约定、核验记录字段与公开判定（`report_check.py` 的 `record.deliverables`）。
- 你提供：内容组织（章节、页序、密度）、deck 规格编写、逐页看图、内容一致性核对、记录与摘要。
- 审阅边界不变：新建文稿不扩展审阅中的按批注回写白名单；图片、图表、SmartArt、组合内部与媒体仍不能自动回写。

## 可选工具

| 工具 | 用途 | 平台 | 已验证版本 | 探测 | 安装 |
|---|---|---|---|---|---|
| Python 3（标准库） | 制作与检查 | macOS / Linux / Windows | 随宿主 | `python3 --version` | — |
| LibreOffice（`soffice`） | 渲染逐页图片 | macOS / Linux / Windows | 26.2（macOS arm64）实测；其余未验证 | `pptx_render.py` 自动探测 PATH 与 macOS 安装位置；可用 `AE_SOFFICE` 指定 | `brew install --cask libreoffice`（macOS）、`apt install libreoffice`（Debian/Ubuntu） |
| poppler（`pdftoppm`） | PDF 转逐页 PNG | macOS / Linux / Windows | 26.07（macOS arm64）实测；其余未验证 | `pptx_render.py` 自动探测；可用 `AE_PDFTOPPM` 指定 | `brew install poppler`、`apt install poppler-utils` |
| Microsoft PowerPoint | 桌面兼容性与原生图表编辑验收 | macOS / Windows | 16.115（macOS arm64）：合成文稿无修复提示，修改图表数据后正确更新；Windows 未验证 | 手动打开桌面应用并记录版本 | Microsoft 官方安装包 |

制作不依赖第三方库；渲染工具缺失或转换失败时，`pptx_render.py` 输出 `unavailable` / `failed` 与具体诊断，按下面「状态与失败处理」登记，基础 HTML 审阅保持可用。渲染工具的首选验证环境是 macOS 与 Linux；Windows 未做实机验证，如在实际任务中验证过，请把版本写进记录的 `render` 字段。

## deck 规格（`ae-pptx-deck/1`）

制作器的输入是一个 JSON 文件。逐页内容来自已确认的内容组织结果；`data-key` 语义键与 HTML 报告共用同一套页面键。

```json
{
  "schema": "ae-pptx-deck/1",
  "title": "演示文稿标题",
  "theme": {
    "colors": {"dk1": "1F2933", "lt1": "FFFFFF", "dk2": "3E4C59", "lt2": "F5F7FA",
               "accent1": "2B6CB0", "accent2": "...", "accent3": "...", "accent4": "...",
               "accent5": "...", "accent6": "...", "hlink": "...", "folHlink": "..."},
    "fonts": {"major": {"latin": "标题西文字体", "ea": "标题中文字体"},
              "minor": {"latin": "正文西文字体", "ea": "正文中文字体"}}
  },
  "size": {"width": 12192000, "height": 6858000},
  "master": {"footer": true, "footerText": "固定页脚文字", "pageNumber": true,
             "logo": {"path": "logo.png", "name": "标志"}},
  "sections": [{"name": "开场", "keys": ["cover", "sum"]}],
  "slides": [
    {"key": "cover", "layout": "title", "title": "封面标题", "subtitle": "副标题",
     "hideMasterShapes": false},
    {"key": "sum", "layout": "titleAndContent", "title": "要点页",
     "body": ["一级要点", {"text": "二级要点", "level": 1, "bold": false}],
     "shapes": [{"shape": "rect", "x": 800000, "y": 5600000, "cx": 3000000, "cy": 600000,
                 "fill": "accent2", "text": "结论一句话"}],
     "charts": [{"chart": "column", "name": "收入", "x": 600000, "y": 1800000,
                 "cx": 6000000, "cy": 3600000,
                 "categories": ["一季度", "二季度"],
                 "series": [{"name": "收入", "values": [100, 112]}],
                 "legend": "bottom", "title": "收入对比"}],
     "notes": "这一页的讲稿"}
  ]
}
```

字段约定：

- **主题**：12 个色槽全部必填（先确定主题色再动手）；`major` 是标题字体、`minor` 是正文字体，各需西文（latin）与中日韩（ea）两个名字。标题、正文、普通形状与图表的受控元素一律引用主题槽位，制作器不写固定色；品牌标志等固定色素材除外。制作器直接写出正确主题，不需要事后补写；换主题验证时只应改动 theme 部件。
- **版式**（`layout`，命名版式）：`title`（封面，标题+副标题）、`sectionHeader`（节标题）、`titleAndContent`（标题+正文）、`titleOnly`（仅标题，放图表与自由形状的常用版式）、`blank`（空白）。使用页脚、页码与标志时由母版承载，逐页自动出现；封面等页面可用 `hideMasterShapes: true` 隐藏母版公共元素。
- **图表**（`charts`）：首版支持 `column`（柱形）、`bar`（条形）、`line`（折线）、`pie`（饼图），全部是 PowerPoint 原生图表并内嵌可编辑数据工作簿。类目与每个系列的数值必须等长；`labels` 只有饼图支持 `percent`。组合图、双轴图与三维图不在首版范围；不支持的类型明确记入缺口，**不能用图片冒充原生图表**。
- **讲稿**（`notes`）：写入对应幻灯片的备注部件；没有讲稿的页面省略该字段，不为验收补造空讲稿。备注对应实际幻灯片——分页与 HTML 不同时，不能直接套用 HTML 的讲稿页码。
- **章节**（`sections`）：每张幻灯片归入恰好一个章节；构建前校验漏页与重复归组。
- **几何**：`x / y / cx / cy` 用 EMU（1 英寸 = 914400）；形状与图表必须落在画布内。图表页优先用 `titleOnly` 版式，不要让图表与正文占位符重叠。

## 生成与核验步骤

1. **对齐沿用默认范式第 0 步**：用途、读者、篇幅、讲稿、视觉对齐确认后再动手；没有演示文稿要求就不触发本流程。
2. **写 deck 规格**：按内容组织结果逐页落规格；主题用已确认的品牌色与字体。数字、单位、期间、依据标注与 HTML 报告同源，不允许两边各写一套。
3. **构建**：`python3 $S/pptx_build.py --deck deck.json --out deck.pptx --json`。规格校验全部通过才落盘；失败按退出码 2 的报错修规格。构建是确定性的：同规格重跑得到同字节哈希；`--json` 输出 `ae-pptx-build/1` 构建摘要（文件名、sha256、页数、标题）。
4. **文件层检查**：`python3 $S/pptx_check.py deck.pptx --json`。覆盖必要部件与关系目标、内容类型、版式引用与命名、占位符对应、四类图表的关键结构（轴引用、标签位置、数据缓存、内嵌工作簿、主题引用配色）、主题部件与章节覆盖。**ZIP 能打开、XML 能解析不算通过**；失败输出检查项、部件路径、可定位幻灯片与原因；结果结构为 `ae-pptx-check/1`。
5. **渲染与逐页看图**：`python3 $S/pptx_render.py deck.pptx --out-dir pages/ --json` 得到逐页 PNG 与 `ae-pptx-render/1` 渲染清单（工具版本、页图哈希、源文件哈希）。然后**你实际查看全部页面**，逐页核对：文字溢出或截断、非预期遮挡、元素超出画布、图表或正文缺失、字体与可读性。有意叠放与影响阅读的遮挡分开判断；几何检测和缩略图只能辅助，生成图片或跑完检测不等于已经逐页看图。
6. **修复与重查**：发现问题先修规格或样式，重新构建、重新跑文件检查并查看**受影响页**；主题、字体或公共版式变化时核对全部受影响页，无法确定影响范围时核对全稿。复用未变化页面的旧渲染证据时，必须能说明该页内容与渲染未受影响，且文件哈希未变。
7. **内容一致性**：逐条核对关键结论、数字、单位、期间、引用依据与未核实事项在 HTML 与 PPTX 一致；允许分页与详略不同，但每条结论（`findings`）都要映射到承载它的幻灯片页码。核验入口会按映射页提取幻灯片文本与图表数据，**声明里的关键数字在映射页找不到就判失败**——合成的跨格式数字不一致反例不能通过。
8. **写记录**：把证据写进 `report-record.json` 的 `deliverables[]`（见下节），再跑第 5 步的公开核验入口（`report_check.py`），HTML 与 PPTX 分别给出结果。

## 记录字段（`ae-report-record/1` 的 `deliverables[]`）

向后兼容的可选扩展；没有该字段的旧记录按原规则处理，不会被误读为已检查 PPTX。

```json
{"kind": "pptx", "required": true, "file": "deck.pptx", "sha256": "<最终文件 sha256>",
 "pages": 12,
 "build": {"tool": "annotation-everything 内置 pptx_build.py", "version": "<AE 版本>",
           "invocation": "python3 $S/pptx_build.py --deck deck.json --out deck.pptx",
           "deckSpec": "deck.json", "deckSpecSha256": "<deck 规格 sha256>"},
 "render": {"tool": "LibreOffice soffice", "version": "26.2", "notes": "pdftoppm 26.07",
            "pptxSha256": "<渲染清单中的源文件 sha256>"},
 "checks": {
   "file": {"id": "pptx.file", "status": "pass", "note": "pptx_check 全部通过", "issues": []},
   "visual": {"id": "pptx.visual", "status": "pass", "note": "逐页看图完成",
              "pages": [{"page": 1, "image": "pages/page-01.png", "sha256": "<页图 sha256>",
                         "checked": true, "issues": []}]},
   "consistency": {"id": "pptx.consistency", "status": "pass", "note": "逐条核对数字与结论",
                   "findings": [{"claim": "收入同比增长 12%", "pages": [2]}]}},
 "powerpoint": {"status": "not_run", "note": "本机无 PowerPoint，待 PR review 实测"},
 "openIssues": [], "note": ""}
```

- `required`：用户明确要求的额外交付写 `true`。必需而未完成时，摘要保留整项请求缺口，但不改变 HTML 自己的可交付判定；「HTML 已可交付、PPTX 未完成」可以同时成立。
- `build`：HTML 用内置范式、PPTX 用另一种制作工具时分别记清，不能一个笼统的方法名覆盖两种产物。缺 `tool` 或 `invocation`，核验入口判失败——只出现技能名称或一段自述不算调用证据。
- `render.pptxSha256`：从本次渲染清单复制源 PPTX 的哈希；视觉检查声明 `pass` 时，该值必须与当前产物哈希一致。重建改变文件后，先重新渲染、复核，再更新证据；只更新产物的 `sha256` 不能沿用旧截图。
- `checks.visual.pages[]`：逐页证据绑定到被检查的文件版本——每页带图片相对路径与 sha256；缺页、有页 `checked: false`、图片缺失或哈希不匹配、带问题却声明 pass，都判失败。
- `checks.consistency.findings[]`：每条记录 `findings` 的结论映射到幻灯片页码；声明里的关键数字（声明文本 + 计算期望值）必须在映射页的幻灯片文本或图表数据中出现。
- `powerpoint`：`pass`（附 `os`、`version`，有则附 `build`）/ `fail` / `not_run` / `unavailable`。未实测的文件必须如实披露该项未执行，不能宣称已在 PowerPoint 实测或兼容所有 Office/WPS 版本。
- `openIssues`：非空却声明 pass，核验入口判失败。
- 每份 PPTX 独立记录；主题补写、图表修复等后续改动后更新 `sha256` 并重新核验受影响范围。
- 记录、渲染图与产物保存在材料旁的受控位置，不进仓库或技能副本。

## 状态与失败处理

| 情况 | PPTX 的结果与交付说明 |
|---|---|
| 制作工具缺失、生成失败或没有有效产物 | 未完成；`deliverables[]` 写 `{"file": null, "status": "unavailable", "note": "具体原因", "resume": "可继续的条件"}`，用户明确要求时整项请求不宣称完成 |
| 有有效文件，但无法渲染、缺看图能力或部分页面尚未查看 | 可保留草稿；对应检查记 `unavailable` 或 `not_run`，不能声明 PPTX 可交付 |
| 文件、视觉或内容一致性检查失败 | 记 `fail` 与问题位置；修复并重新核验后才能通过 |
| 所有适用检查通过且无阻塞缺口 | PPTX 可交付；同时披露实际检查范围与 PowerPoint 实测状态 |

## PowerPoint 实机兼容性验收

首版不把本机 PowerPoint 设为每次生成的必装依赖；交付按文件检查、逐页视觉与内容一致性判定，并披露实测状态。**功能 PR 的必要验收**是：在至少一套 Microsoft PowerPoint 桌面环境打开合成样例，记录操作系统、PowerPoint 版本与构建号，确认无修复提示、无正文或图表丢失、修改原生图表数据后图形正确更新；结构检查与 LibreOffice 渲染成功不能代替这项实测，环境不可用时如实移交「待 PR review 验证」。用户明确要求逐份实测时，该项未完成会阻止该 PPTX 可交付。

## 边界

- 不把参考技能中的任意内部标记修改能力接入按批注回写，也不要求每条批注先调用演示文稿技能。
- 不整份拷入官方技能及其结构定义目录，不自动搜索或安装远程技能。
- 不做既有模板填充、审阅中的增删页、图表数据修改或整份换风格（属审阅回写与后续能力）。
- 不覆盖全部 OOXML 错误、全部图表类型或全部 Office/WPS 版本；文件检查是本票生成范围的最低要求。
- 不新增模型服务；生成与看图推理不进本机审阅服务。
