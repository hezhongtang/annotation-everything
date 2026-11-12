# 输入与输出格式

[English format reference](formats.en.md) · [中文操作流程](workflow.zh-CN.md) · [共用安全与协议约束](protocol.md)

本页讲已有报告、讲稿与审阅数据的格式。**从原始材料生成新报告**时，先读[报告生成：默认范式](generation.zh-CN.md)；新报告随附的核验记录（schema `ae-report-record/1`，存为报告旁的 `report-record.json`）与公开核验入口 `scripts/report_check.py` 的字段契约以那份指引为准，已有报告没有记录时直接审阅、不需要补建。

## 报告 HTML

逐页一个 `section`，`class` 含 `page`，带页码与唯一键；版面按 1280×720 设计（对照页按这个尺寸缩放）：

```html
<!doctype html><html><head><title>示例报告</title>
<style>.page{width:1280px;height:720px;padding:56px}</style></head><body>
<section class="page" data-page="1" data-key="cover" id="page-cover"><h1>封面</h1></section>
<section class="page" data-page="2" data-key="toc" id="page-toc">
  <a class="toc-row" href="#page-sum" data-range-start="sum" data-range-end="rev">
    <span class="toc-label">开篇</span><span class="toc-title">结论与收入</span></a>
</section>
<section class="page" data-page="3" data-key="sum" id="page-sum"><h2>结论</h2>…</section>
<section class="page" data-page="4" data-key="rev" id="page-rev"><h2>收入</h2>…</section>
</body></html>
```

- 所有 `<style>` 和本地 `<link rel="stylesheet">` 按出现顺序收进对照页（Shadow DOM 隔离，不影响外壳；注释掉的不算）；样式里的 `:root`、`html` 改写成 `:host`，`body` 改写成页面舞台：变量、文字、颜色、背景照常生效，`body` 上的布局属性（padding、flex、grid、min-height 等）会被复位，免得幻灯片被挤偏；`html` 上的字号会搬到外层，`rem` 照常。`vw`/`vh` 与 `@media` 按浏览器窗口而不是幻灯片计算，依赖它们的版式可能和单独打开时不同。`<script>` 与内联事件（`onclick` 等）不执行。
- 本地相对路径的图片、字体、音视频（`src`、`srcset`、`poster`、CSS `url()`）会内联成 data URI，单个上限 10 MB；只收报告所在目录之内的文件（`--asset-root` 可放宽），其他类型或目录外的文件跳过并警告；外链（http/https）不加载，生成时打印警告。
- section 可以嵌套别的 section（卡片等），按层级识别；class 必须含独立的 `page`（`page-wrapper` 之类不算页）。页码重复或不是整数会报错。
- 页内链接写 `href="#page-<key>"` 时，在对照页里点击会跳到那一页。
- 板块导航推断规则：取带 `data-range-start` / `data-range-end`（值为页面键）的链接；有 `.toc-label` 的链接时只取这些主条目；嵌套在别的范围里的子范围不进导航条。
- 缺 `data-key` 时用 `id` 去掉 `page-` 前缀，再不行用 `p<页码>`。没有讲稿时，页标题取该页第一个 h1–h3。

不是这种结构的材料（PPT、PDF、普通网页；Word 文档见下一节）：先转成逐页 section 结构再用本 skill（例如每页一张图片放进 section，也能在上面标注）。

PPTX 转图前，运行 `python3 $S/check_pptx_fonts.py 原始.pptx`。检查器读取幻灯片、母版、布局和主题中的字体声明，并用本机 `fc-list` 枚举字体；不会解压或修改原文件。转出 PDF 后，运行 `python3 $S/check_pptx_fonts.py 原始.pptx --pdf 转换后.pdf`，用 `pdffonts` 列出字体名称及 `emb`（是否嵌入）、`sub`（是否子集）等证据。退出码 0 表示本次检查没有发现差异，1 表示有待核查或未知项，2 表示输入或所需工具出错。未检出仅表示这次枚举没找到，主题字体可能未被使用；主题里按文字脚本列出的备选字体（`<a:font script=…>`）单独汇总，不计入未检出，中日韩文字或阿拉伯、希伯来、南亚等文字没有显式指定对应的东亚或复杂文种字体时记为未知；别名、样式名、混合回退、轮廓化文字或图片可能无法按名称对应，名称一致也不能证明逐字形渲染正确。没有 `fc-list` 或 `pdffonts` 时分别安装 fontconfig 或 poppler，或人工核对，不要把未知当成通过。补齐字体并重新转换后，逐页检查换行、间距和遮挡。

## Word 文档（DOCX）

`--report` 也可以直接给 `.docx`，不用先转成逐页 HTML：

```bash
python3 $S/build_viewer.py --report 合同.docx --out 合同-review --name "采购合同"
```

- **分页**：按 Word 保存时写下的渲染分页（`w:lastRenderedPageBreak`）、硬分页符、段前分页和分节符切页。页面尺寸、方向和页边距取自各节设置，A4 纵向就按 794×1123 像素显示，不改成横向舞台；内容比页高时页面向下延伸，不截断。对照页按页宽适配，可纵向滚动。
- **分页可信度**：生成器、`review.json`（`docx.pagination`）和对照页横幅都会标明分页可信度。

  | 值 | 含义 | 处理 |
  |---|---|---|
  | `verified` | 有渲染分页，页数与 `docProps/app.xml` 记录的一致 | 可视为 Word 原分页 |
  | `unverified` | 有渲染分页，但页数对不上或没有记录 | 分页可能已过期；用 Word 打开另存一次后重建 |
  | `estimated` | 文档没有渲染分页（python-docx、部分在线工具导出） | 只按显式分页符切，页码不是 Word 页码；用 Word 或 WPS 另存一次后重建 |

  不提供「原页图像」只读模式。
- **定位**：正文段落带 `data-docx-p`（`document.xml` 里 `w:p` 的文档顺序编号）和 `data-docx-pid`（`w14:paraId`），文字运行带 `data-docx-r`。页面键优先取页首段落的 paraId（从段落中间开始的页加 `~c`），没有 paraId 时取页首正文的摘要；正文段落另带 `data-docx-d`（段落文本摘要：保留空白的完整正文 SHA-1 前 12 位），跨页段落的续片带 `data-docx-o`（这一片在段落里的起始字符偏移）。页眉页脚、文本框和续表重复的表头不带定位。
- **批注定位**：在报告上引用文字、放点或框选时，批注写入带版本号的 `docx` 字段（见下文 `notes.json`），记录所在段落的 paraId、段落序号、文本摘要和选区在段落内的字符偏移。浏览器拿不到 Shadow DOM 里的选区时（非 Chromium），页面在当前页按文本反查：只有一个段落含这段文字才写定位，并把来源记为 `text`。重建时先按段落找回批注现在所在的页，同步 `page`、`key`、`title`：paraId 在文档里唯一时用它；没有 paraId、paraId 重复或已不存在时，用段落序号与文本摘要互证（摘要唯一即认定，摘要有多处相同时要求序号也一致），并在生成器输出里说明。跨段引用和框选会同时核对起点与终点，并同步两端段落序号。任一端找不到、无法确认或顺序倒置时标 `orphan`，页码与旧指纹不动；用户在页面上确认归属或把批注挪到别的页后，这条批注的 `docx` 定位随之去掉，之后按页面键处理。文本摘要保留空白，纯分页重排不影响摘要；文字或空白被编辑后，只有唯一 paraId 仍能确认段落身份。单页段落保留 `changed` 和旧指纹；改动段落跨页时无法证明旧偏移所在的片段，标 `orphan`，不按旧偏移挪页。缺少唯一 paraId 时，文本编辑导致摘要不匹配也保留待确认状态。页面键只用于没有段落定位的批注。`review.json` 的 `docx.fingerprint` 记录源文件的 SHA-256；批注的 `docx.fp` 与它相同，表示定位已按当前版本核对。
- **回写**：`docx_edit.py apply 审阅目录 --batch N` 或重复 `--id ID` 回写未处理的报告侧快捷删除／替换；自由批注用 `replace 审阅目录 --paragraph N --from 原文 --to 新文`，段落序号从 0 开始。paraId、序号与文本摘要相互核对，原文先精确匹配，再忽略空白差异；重复文字要求选区偏移能唯一确认。跨运行修改保留运行格式；跨段、制表符、符号及换行结构不自动回写。选中只读结构、无可靠定位或跨段范围时，快捷修改仍记录为批注并说明限制。
  - 只重写目标段落中实际变化的 `w:t`；保留其他 XML 字节、ZIP 其他成员的本地头／压缩流／描述符，中央目录仅调整必需的偏移。支持 UTF-8、BOM、命名空间前缀变化、实体、CDATA 和空文本元素；UTF-16、ZIP64／分卷等不支持的包结构明确拒绝。包大小上限 256 MiB，XML 部件上限 64 MiB，解压后成员合计上限 512 MiB。
  - 默认原地写入：先保存 0600 的二进制备份，再将新包写入同目录临时文件并 fsync；重开检查 XML、段落数、预期文本及所有成员 CRC，提交前再次核对源指纹与 Word 锁文件，通过后才 `os.replace`。失败不替换原件，已生成备份保留。备份位置为 `.snapshots/batch-N/docx-writeback/`；未指定批次时 N 为 0。
  - 写前拒绝 Word 锁文件（`~$`）、与 `review.json` 不同的源指纹、数字签名、编辑保护及修订模式。只有修订模式可用 `--allow-untracked` 显式放行，并提示「AE 写入的是不留痕改动」；原生修订段落仍只读。其余拒绝原因按提示在 Word 中处理并重建，不提供强制覆盖。
  - `--dry-run` 只预演；`--output 新路径` 写到尚不存在的新文件并保留源文件；`--locale zh-CN|en` 选择命令行语言。回写不会改变批注状态或刷新指纹，成功后须重建、核对结果并记录处理状态，详见[改稿流程](workflow.zh-CN.md)。`notes.py` 的二进制快照与按页文本 diff 同样适用。
- **不支持**：旧版二进制 `.doc`、加密文档、Strict Open XML 会直接报错并说明处理办法，不按文本读取。DOCX 按不可信输入处理：颜色、字体名写进样式前会校验，XML 部件限制大小并拒绝 DTD。

支持矩阵（生成器只列出「近似」「占位」「不显示」或段落只读的结构，并给出所在页；对照页「查看限制清单」同样列出）：

| 结构 | 显示 | 所在段落能否回写 |
|---|---|---|
| 正文、高亮、删除线、加粗倾斜下划线、字号颜色 | 原样 | 能 |
| 表格（列宽、合并单元格、底纹、边框、重复表头） | 近似：表样式的条件格式不还原；跨页行按分页标记切开 | 能 |
| 列表编号 | 近似：按 Word 规则计算，不可选中 | 能（编号本身不回写） |
| 页眉页脚 | 近似：按节、首页和奇偶页显示；PAGE / NUMPAGES / SECTIONPAGES 按实际页码 | 不适用 |
| 图片 | 近似：按原尺寸内联，同一张图只内联一次；超过 10 MB 显示占位 | 不适用 |
| 浮动图片、形状 | 近似：显示在锚点处，位置不保真 | 不适用 |
| Word 原生修订（插入、删除、移动、表格行列修订） | 近似：插入绿色下划线、删除红色删除线，不接受也不拒绝 | 不能 |
| 格式修订 | 近似：只显示修改后的格式 | 不能 |
| Word 批注 | 占位：`[W编号]` 标记加悬停文字，范围加底色；不导入为 AE 批注 | 能 |
| 文本框 | 近似：在锚点处以虚线框内联显示 | 不能（也不能定位） |
| 域（目录、日期、交叉引用等） | 近似：显示 Word 保存的结果 | 不能 |
| 脚注、尾注 | 占位：只显示引用标记 | 不适用 |
| 公式 | 近似：按纯文字显示 | 不能 |
| 内容控件 | 近似：显示其中内容 | 不能 |
| 隐藏文字 | 不显示（与 Word 默认一致） | 不能 |
| 嵌入对象（OLE）、外链图片、不支持的图片格式 | 占位 | 不能 |
| 分栏 | 近似：按单栏显示 | 不适用 |
| 未识别结构 | 不显示，诊断里列出元素名 | 不能 |

`review.json` 里 `format` 为 `{"report": "docx", "version": 1}`。版本高于当前安装能处理的版本时，`--rebuild`、`serve.py` 和 `notes.py` 都会明确报错，不按普通文本读取。旧审阅没有 `format` 字段，仍按逐页 HTML 处理。

## 讲稿 Markdown

中文与英文两种页头、字段写法都认，同一份讲稿里还能混用。认哪种写法与界面语言无关：中文界面可以审阅英文讲稿，英文界面可以审阅中文讲稿；标题和正文按原文进页面，不翻译。合成样例见 `fixtures/sample/script.md`（中文）与 `fixtures/sample/script.en.md`（英文），两份结构与内容一一对应。

```markdown
# 讲稿：示例报告          ← 一级标题（报告没有 <title> 时作为页面标题）

## 使用说明                ← 第一个页头之前的内容进「说明」浮层（支持标题、列表、表格）
- 每页先讲结论。

### 第 3 页｜结论：收入增长 12%        ← 中文页头
**时长**：1.5 分钟         ← 用于累计用时
**讲法**：                  ← 逐行写，每行一段（下面那行就是正文）
逐行写，每行一段。
**过渡**：收入结构上……
**可能被问**：
- 问：新店贡献多少？答：约三分之二。

### Page 3｜Conclusion: revenue up 12%   ← 英文页头，与上面等价
**Duration**: 1.5 min
**Talking points**:          ← 逐行写，每行一段（下面那行就是正文）
One line per paragraph.
**Transition**: In the revenue mix…
**Likely questions**:
- Q: How much from new stores? A: About two thirds.
```

- 页头：`###` 之后写页码，中文写「第 N 页」，英文写「Page N」（大小写、`Page` 后有没有空格都不限），再用全角「｜」或半角「|」与标题分开。
- 字段名中英文都认，冒号全角半角都可以；英文字段名大小写、词间空格不限。

| 字段 | 中文 | 英文（规范写法） | 英文别名 |
|---|---|---|---|
| 时长 | `**时长**` | `**Duration**` | `**Time**` |
| 讲法 | `**讲法**` | `**Talking points**` | `**Talking point**`、`**Talk**` |
| 过渡 | `**过渡**` | `**Transition**` | — |
| 问答 | `**可能被问**` | `**Likely questions**` | `**Likely question**`、`**Q&A**`、`**Questions**` |

- 问答逐行写「问：……答：……」或「Q: … A: …」；答的标记取行内先出现的那个，问的标记只在行首才去掉；行首的 `-`、`*`、`•` 会去掉，不写项目符号也行。英文 `A:` 前面必须是空白、标点或行首——「套餐A：」「plan A:」里的 `A:` 是正文，不当成答标记。
- 时长写「N 分钟」「N 秒」「1 分 30 秒」或「N min」「N minutes」「N seconds」「N s」；一段里有几个数就累加几段，认不出的单位和数字不计入累计用时（有时长字段却一段都读不出来时生成器会警告，不静默按 0 秒算）。
- 没有任何字段的页，整页正文按「讲法」显示；第一个字段之前的内容也按「讲法」显示，不会凭空消失。
- 讲稿页码须与报告页序一一对应，且同一份讲稿里不能重复；不一致时生成器列出差异并退出，`--lenient` 以报告页序为准（缺讲稿的页显示为空）。
- 认不出的页头（`### Page 3 结论` 少了分隔符、`####` 用错层级、`### 第 3 页 结论` 等）和重复的页码是格式错误：生成器给出行号、原文和期望写法后退出，不把错输入当成空讲稿交付，`--lenient` 也不放过这一类（宽容只针对页码不一致）。正文里不要写以 `### 第 N 页` 或 `### Page N` 开头的行，那会被当成页头。
- 字段的处理规则：
  - 重复字段：采用最后一次的值，并警告前面几次出现在哪几行。
  - 认不出的字段名（英文写错，或用了 `**备注**` 这类自有字段）：不当成字段，内容按正文保留，并警告。旧讲稿里写错的字段不会让文字消失，只是不计入时长、问答和过渡。
- 生成器按界面的当前语言给出上面这些诊断（`--locale auto`、`zh-CN`、`en`，与讲稿本身用哪种语言无关）。

## notes.json

```json
{
  "version": 1,
  "rev": 7,
  "updatedAt": "2026-10-27T18:19:25+08:00",
  "notes": [
    {
      "id": "mvqj9tn44u4o8a",
      "page": 3,
      "title": "结论：收入增长 12%",
      "target": "报告",                     // 报告 / 讲稿 / 报告和讲稿
      "text": "把 +12% 改成「同比 +12%」",
      "quote": "+12%",                      // 用户选中的原文，可为空
      "quoteFrom": "报告",                  // 引用来自哪一侧
      "pin": {"x": 38.8, "y": 59.8},        // 报告上的标注之一：点（百分比），可为 null
      "region": {"x": 5, "y": 22, "w": 40, "h": 25},  // 报告上的标注之二：框（左上角 + 宽高，百分比）；一条批注只有点或框之一
      "status": "resolved",                 // open 未处理 / drafted 已初改·待复核（两段流程）/ resolved 已处理 / held 搁住
      "at": "2026-10-27T10:19:14.129Z",
      "updatedAt": "…",                     // 编辑过才有
      "resolvedAt": "…", "resolvedBy": "claude", "resolution": "标题与 KPI 已改",
      "resolvedBatch": 3,                   // 定为已处理时所在的批次（handoff 的 seq），处理记录据此统计
      "draftedAt": "…", "draftedBy": "sonnet-初改", "draftBatch": 3,   // 两段流程的初改（drafted 时 resolution 是初改说明）
      "trail": [                            // 处理痕迹：只追加（最多 50 条），谁、哪个阶段、第几批、做了什么、理由
        {"at": "…", "by": "sonnet-初改", "stage": "draft", "batch": 3, "action": "drafted", "note": "标题改短"},
        {"at": "…", "by": "opus-复核", "stage": "review", "batch": 3, "action": "revised", "note": "再收短，讲稿同步"}
      ],   // action：drafted 初改 / resolved 处理 / finalized 定稿 / approved 复核通过 / revised 复核再改 / returned 退回给用户 / reopened / accepted 用户定稿
      // 以下字段都可选
      "edit": {"op": "replace", "from": "+12%", "to": "同比 +12%"},   // 快捷修改：delete（无 to）/ replace
      "pair": {"side": "讲稿", "text": "收入同比增长 12%", "match": "similar"},  // 另一侧对应句，用户勾选了同时改；match: exact / similar
      "style": {"scope": "region"},        // 风格优化；scope 跟着标注走：有框 region、有点 pin、都没有 page
      "anchor": "table「渠道 二季度 三季度」",   // 位置线索：点下或框里的元素（拖动标注后重新计算）
      "skills": ["humanizer-zh"],                // 处理这条前要调用的技能
      "key": "sum",                              // 页面键（报告 section 的 data-key），重建时据此校正 page
      "orphan": true,                            // 重建后原页面（DOCX：原段落）已找不到（待确认），确认后去掉
      "docx": {                                  // DOCX 审阅的段落定位（HTML 报告的批注没有）；v 是字段版本
        "v": 1,
        "kind": "quote",                         // quote 引用选区 / pin 点 / region 框
        "via": "selection",                      // 定位来源：selection 浏览器选区 / text 按文本唯一匹配反查 / point 标注处的段落
        "fp": "<源文件 SHA-256>",                 // 定位按哪个版本的源文件核对过；与 review.json 的 docx.fingerprint 相同即当前版本
        "para": {"id": "1A000007", "n": 6, "digest": "54f68cf0074a"},   // paraId（没有为 null）、段落序号、文本摘要
        "start": 12, "end": 30,                  // 选区在段落内的字符偏移（按 Unicode 码点，不含自动编号）；点和框只在跨页段落的续片上带 start
        "endPara": {"id": null, "n": 7, "digest": "…"},   // 选区或框跨段落时的最后一段；此时 end 是它里面的偏移
        "run": 1,                                // 文字运行序号，只作提示
        "changed": true                          // 重建后段落还在但文字已变：偏移与摘要对应旧版本，fp 不更新
      }
    }
  ]
}
```

- `held` 表示暂不交给 Agent 改稿，`hold` 记录「分析中」或「结论已变」，`heldBy` 记录关联分析。快照只收 `open`。`fromAnalysis`、`basis` 和源批注的 `analysisId` 记录分析关联，同 id 更新与导入保留认不出的扩展字段。
- 旧版工具导入未知 `held` 时退回 `open`，旧页面也显示为未处理；旧服务的快照仍只收 `open`。升级后重启服务，重新生成并刷新页面，避免旧页面手动改写状态。
- 快捷修改的 `text` 是固定说法（「删除引用的这段文字」「改为：…」），`quote` 等于 `edit.from`，旧工具照样能看懂。
- 生成对照页时若审阅目录里已有批注：没有 `key` 的按上一版 review.json 的页码补上，再按 `key` 把 `page` 改成新版的页码；`key` 在新版里找不到的标 `orphan`。所以 `data-key` 要稳定、唯一（重复的键不做校正）。DOCX 审阅里带 `docx` 定位的批注先按段落校正，不走页面键，规则见上文「批注定位」。
- `docx.v` 高于当前安装支持的版本时，字段原样保留（上限 4000 字符），不用于重建定位；`notes.py show` 会标明不可用于回写。没有 `docx` 字段的旧批注按原规则读取。
- 风格优化的 `target` 固定为报告；`text` 是用户写的方向，没写时为「风格优化：整页 / 框选处 / 标点处」。
- 0.3 及更早只有风格优化能框选，框存在 `style.region` 里；读入时自动挪到顶层 `region`（下次写盘落成新格式），原来的风格范围和点都保留——所以旧批注可能点和框并存、或「整页」风格优化带一个点。0.3 页面发来的 `{style:{region}}` 改动按顶层 `region` 处理。
- 批注归哪一批：`resolve` / `review` 不带 `--seq` 时按批注归属找（待复核的初改 → 它的初改批次；提交时包含它的在途批次，复核中优先、同级取最早），不再取「最新一批」；同时有几批处理中时 `done` 必须带 `--seq`。别的批次还在等复核的初改，不能在另一批里处理。
- 页面新建和「重新标注」时 `pin`、`region` 一起发（另一个为 `null`），所以新批注只有一种标注；拖动只发被拖的那一个，另一个不动。改动动了标注时，风格范围跟着标注走（只拖动、原范围是整页的保持整页）。
- 页面轮询的 `GET /api/notes` 只带最新一批（`handoff.latest`、计数、`total`）；完整处理记录 `history` 由 `GET /api/handoff` 和 `notes.py batches` 提供。服务响应里带 `version` 与 `features`（含 `marks`、`batches`），页面据此识别旧版服务。DOCX 审阅的页面要求服务声明 `docx` 特性：没有声明这项特性的旧版服务会丢弃 `docx` 字段，页面连上这样的服务时显示阻断提示并停止写入，重启新版服务并刷新后恢复。

`notes.jsonl` 每行一条操作：`{"ts","rev","op":"add|update|resolve|draft|reopen|review|finalize|delete|import|handoff|handoff-update|handoff-claim|handoff-done|handoff-review|handoff-finalize","by":…,"id"|"seq",…}`。

## handoff.json（「交给 Agent」的提交）

```json
{
  "version": 1, "seq": 3, "updatedAt": "…",
  "items": [
    {"seq": 3, "status": "pending",            // pending 等待接收 → claimed 处理中 →〔drafted 已初改·待复核 → review 等待复核 → reviewing 复核中〕→ done 已处理
     "target": "final",                       // 用户要求做到：final 定稿 / review 已初改·待复核
     "allowSubagents": false,                 // 用户是否允许代理派发子代理（不允许时由主代理亲自处理）
     "fullCheck": true,                       // 本批处理后是否全量勾稽报告和讲稿；旧批次缺省为 false
     "at": "…", "by": "page", "userMessage": "先改第 7–12 页",  // 用户原始附言
     "message": "先改第 7–12 页\n\n【本批提交选项】全量勾稽：…",  // 兼容旧版 wait；勾选时附加勾稽要求
     "openCount": 5, "openIds": ["…"], "pages": [7, 9, 12], "updates": 1,
     "claimedAt": "…", "claimedBy": "claude", "doneAt": "…", "doneBy": "claude", "summary": "改了 4 条，1 条等你定",
     // 以下由 done 记下（处理记录）：这批里代理标为已处理的批注、涉及页、提交时的批注还剩几条未处理
     "resolvedIds": ["…"], "resolvedCount": 4, "resolvedPages": [7, 9], "leftOpen": 1,
     "draftedIds": ["…"], "draftedCount": 2,  // 已初改·待复核的批注
     "stages": [                              // 各阶段：draft 初改 / review 复核 / accept 用户直接定稿
       {"stage": "draft", "by": "sonnet-初改", "claimedAt": "…", "doneAt": "…", "doneBy": "…", "summary": "…", "draftedIds": ["…"]},
       {"stage": "review", "by": "opus-复核", "claimedAt": "…", "doneAt": "…", "summary": "…",
        "approved": 5, "revised": 2, "returned": 1, "resolved": 1}],
     "reviewRequestedAt": "…", "reviewMessage": "重点看第 7 页",
     "alwaysSkills": [{"name": "humanizer-zh", "scope": "讲稿"}]}   // 提交时的常驻技能
  ]
}
```

不用 `notes.py wait` 的代理：定时运行 `notes.py claim 审阅目录`（有等待中的提交就接收并输出 JSON，退出码 0；没有返回 3），处理完用 `notes.py done` 收尾。勾选全量勾稽时，`message` 附加该要求，确保仍在等待的旧版 Agent 也能读到；`userMessage` 保留用户输入的原文，旧批次没有此字段。可以读这个文件看状态，但**不要直接改它**：写入要经过 notes.py / 服务的文件锁，否则可能覆盖用户刚合并进来的附言。保留最近 200 批。

页面接口：`POST /api/handoff` `{message, target, allowSubagents, fullCheck, userMessage}` 提交一批；新批次的 `fullCheck` 缺省为 false，合并待接收的批次时缺省表示保留原值，显式传入布尔值才更新；请求复核时保留原批次的要求。勾选时页面在 `message` 开头放勾稽要求，并用 `userMessage` 传原始附言；新服务保存原文并生成兼容旧 Agent 的 `message`。响应中的 `acceptedFullCheck` 确认该批次实际保存的选项。`POST /api/handoff/review` `{seq, message, allowSubagents}` 请求复核；`POST /api/handoff/finalize` `{seq}` 直接定稿。轮询状态里的 `inFlightIds` 是已交给 Agent、还在路上的批注（页面「批注」按钮据此显示「处理中」）。服务功能列表中的 `full-check` 表示支持这个选项；页面发送前检查功能，服务未确认保存时保留待补传提交并提醒核对批次状态。若服务在检查后切换到旧版，旧服务可能已经接收提交；其 4,000 字上限包含兼容提示，因此可能截短旧 Agent 首次收到的附言，恢复新版后重试会携带完整原文，也可能形成另一批。旧服务和旧页面共同更新待接收批次时也可能覆盖兼容提示，使用新选项前应升级服务并刷新页面。

`POST /api/server/stop` 对旧页面同样进入审阅级停止生命周期。先拒绝新提交和后台工作，完成保存保护、等待者退出与轮次取消确认，最后停止 HTTP；HTTP 只有在 `server_close()` 返回后才记为停止。`.annotation-stop.json` 私有原子记录停止标识、`stopping` / `stopped` / `incomplete`、各组件确认以及 hook 配置与进程号。独立 hook 在服务消失后仍须实际退出；`connecting` / `sending` 投递属于活动工作，停止与直接投递由同一分发锁协调，取消快照后不能再开始新轮次。普通命令行或总览的显式启动即使复用运行中的服务也会解除停止状态，批次、绑定和专属端口不变。`stop-review` 声明该能力，新页面连接旧服务时提示升级，不退回只停 HTTP。

全部定稿仅供审阅人在页面确认，不提供 Agent 或 CLI 入口。服务功能列表中的 `finalize-all` 声明支持；不支持时页面隐藏按钮并提示更新技能、重启服务和刷新。`GET /api/notes` 的 `handoff.finalizable` 与 `GET /api/handoff` 的顶层 `finalizable` 提供 `{count, batches: [{seq, ids}], skipped: [{id, batch, reason}]}`：只把 `drafted` 批注且批次为 `drafted` / `review` 的条目列为可定稿，`claimed` / `reviewing` 的初改列为跳过。

`POST /api/handoff/finalize-all` 的 JSON 请求为 `{batches: [{seq, ids}]}`，使用审阅人确认的快照；批次号为正整数，批注 ID 与批次不能重复，空列表不写入。服务在同一把 `.notes.lock` 内重读并核对批次、状态与归属，只定稿已确认的初改。确认后新出现的批次不顺带定稿；同批新增初改时整批跳过，避免完成的批次中遗留初改。响应包含 `finalizedCount`、`finalizedIds`、完成批次号列表 `batches`、`skippedCount` 和 `skipped`，另带更新后的 `doc` 与 `handoff`。跳过原因：`claimed` 正在初改、`reviewing` 正在复核、`unavailable` 批次不可定稿或不存在、`changed` 状态或批次归属变化、`unconfirmed` 不在本次确认范围。页面以实际结果显示部分成功；取消确认不发写请求，断线时不暂存或乐观定稿。

新增摘要和响应字段可被旧页面忽略，磁盘格式与批次状态机不变；批注沿用 `accepted` 痕迹，批次沿用 `accept` 阶段、`done` 状态及既有逐批日志。旧页面连接新服务仍可用原入口；新页面连接旧服务时先做功能协商。写接口与其他写操作共用 Host 校验、同源 Origin、JSON Content-Type、无 CORS 头的安全门。

源文件快照：代理接收初改时把 review.json 里的报告与讲稿源文件存到审阅目录 `.snapshots/batch-N/base/`，初改收尾时存 `draft/`（目录 0700、文件 0600，只留最近 30 批），`notes.py diff` 据此按页对比。审阅目录含经营数据时，快照同样不要外传。

处理记录（页面「处理记录」/ `notes.py batches`）：每次提交算一批。服务返回的状态里 `history` 是全部批次（新的在前，不含 `openIds`）；处理中的批次按当前进度实时统计（`live`），0.3 留下没记 `resolvedIds` 的旧批次按「接收到收尾之间代理标的」推算（`estimated`）。

## skills.json（技能目录，可选）

`{"version": 1, "skills": [{"name": "humanizer-zh", "desc": "中文润色"}], "always": [{"name": "humanizer-zh", "scope": "讲稿"}]}`。

- `skills`：技能目录。生成和重建时从常见技能目录及插件缓存读取名称、说明；插件技能使用 `插件:技能` 调用名，插件缓存不代表当前已启用。可用 `--skills-from` 指定来源或 `--no-scan-skills` 禁用扫描，也可用 `notes.py skills` 手工维护。页面「技能」输入栏把目录和用过的技能列为候选。
- `.skill-sources.json`：私有来源记录，保存扫描结果、手工覆盖和排除项；来源路径只保存哈希。目录更新与常驻技能使用同一把锁。手工移除不会在重建时恢复；扫描不完整时保留该来源旧记录并警告。`skills.json` 结构不变，但旧版 `notes.py` 写回超过 200 项的目录可能截断，应先升级。
- `always`：常驻技能，范围内的每条批注处理前都要先调用；`scope` 为 `all`（全部批注）/ `报告` / `讲稿`（对象含该侧，「报告和讲稿」两侧都算）/ `style`（风格优化），最多 20 个。页面「常驻技能」（`POST /api/skills/always`）或 `notes.py skills --always …` 设置；`show` / `wait` 把适用的常驻技能列进每条批注的「技能」；每批提交时把当时的名单记进 handoff 的 `alwaysSkills`。
- 两者都在生成对照页时嵌进页面，本地文件模式同样可见。文件模式下改常驻技能只存本浏览器（之后 Agent 改了名单并重建，以新名单为准）；「复制全部 / 下载 Markdown」的开头一行、「下载 JSON」的 `alwaysSkills` 都会带上，`notes.py import` 或页面「导入…」时随之写入（审阅目录里已有不同的名单时，命令行只提示、页面先询问）。
- 读 skills.json 时坏项跳过、好项保留（字符串项当「全部批注」）；页面和命令行写入时严格校验：一项里写了几个名字会拆开，超过 20 个报错。命令行同一条命令里先删（`--always-remove`）后加（`--always-add`），改范围就是删了再加。

## 分析数据

分析保存在 `analysis/<分析标识>/events.jsonl`：每行一个只追加事件，首个发起事件带 `schema`，当前状态由日志推导。问答不按批次或处理痕迹的保留上限截断。`analysis/counter.json` 分配审阅内唯一轮次号；`handoff.json` 的 `submitSeq` 为改稿、复核和分析分配共享交出序号 `order`，同秒提交也能区分先后。

分析、批注与批次共用 `.notes.lock`。从批注发起分析会同锁搁住源批注并移出待领取快照；空批次记为 `void`，不再领取。结论草稿只存分析事件，确认后才写入 `notes.json`，不新增批注草稿状态。页面引用保留稳定 `key`，转出和确认时重定位；页面删除或键有歧义时标为 `orphan`。

`GET /api/analysis` 返回摘要和队列计数，传 `?analysis=<标识>` 时追加完整状态、关联批注 `linkedNotes` 和当前线轮次上下文；再加 `&round=N` 可只读该分析任意轮次，不切线。审阅人写操作为 `/api/analysis/start|answer|submit|convert|confirm|release|activate|pause|redecide|update-note|keep-note|delete-note|seal|fork|switch-line|close|reopen|refute`，沿用 Host、Origin、JSON Content-Type、审阅身份及相关接收方校验。`start` 接受源 `noteId`，也接受没有源批注的 `focus` 和可选 `page`。服务用 `analysis` 能力标记声明支持，文件模式不提供分析。

轮次的 `line` 标明所属分析线。`assumptions` 保存带稳定标识的假设，`confirmed` 按线记录 `pending`、`confirmed` 或 `rejected`，`refuted` 与不认同分开；决策的 `assumptions` 记录直接依赖。`facts` 保存稳定标识、原文、出处及可选问题引用。`analysis-writeback --proposals` 接受旧问题列表，或含 `proposals`、`assumptions`、`facts` 的对象。新增、拆分、合并和作废均是待确认结构提议；确认前不改树，旧节点和决策保留。详细字段与处理约束见[分析操作指引](analysis.zh-CN.md)。

自由文本回答保留原话并生成稳定选项；`park` 将问题放入搁置区；`verify` 记录查证请求，`allowExternal` 缺省为布尔值 `false`。暂停和切换分析只改变焦点，不增加基线版本，也不取消已交出或处理中的轮次。`notes.py suggest-analysis` 仅写入批注的 `analysisSuggestion`，不会创建分析。

`protection` 给出本分析是否有领取中的轮次；节点的 `branch` 区分当前选择、断枝与作废。重裁或请求更新会作废本线未领取轮次，基线拒收也会作废原轮；有效原答带入同线重新出题，不重复创建已经确认的实体。已处理或作废轮次不能继续作答，领取中的合法作答不增加基线版本。

从分析转出的批注用 `tracking` 控制是否跟踪依据。依据失效只联动未处理或「结论已变」搁住的批注；已领取/复核批次中的批注不移出，已处理状态不回改。`updateRequests` 保存请求，`updates` 保存 Agent 新文本草稿，轮次的 `U:` 条目确认后才改普通批注，并把旧文和旧依据追加到 `analysisHistory`。写回对象可带 `updates: [{noteId, text, node?}]`；拒绝新文本不会覆盖原文。

`lines` 保存各线的最新节点投影，`nodes` 保持为当前线投影；决策、假设、事实与节点选项身份跨线共享。`checkpoints` 使用稳定 `cpN` 标识，包含 `line`、来源轮次、是否自动/封存及当时快照。快照含焦点、节点、决策、假设、事实、有效结论、未决与搁置问题，不递归嵌入旧检查点。封存已有检查点只改变 `sealed`，不重拍快照；从检查点分线不更换继承实体的身份。

快照的 `pendingProposals` 保留尚未确认的结构提议，分线不预建节点。推翻后节点的 `reopened` 保留原决策、原答案、假设和撤下原因，`needsReview` 包含自身与祖先的失效依赖；旧线的 `basedOnRefuted` 标记相应假设。假设不成立是全局事实，旧快照与冻结上下文仍保留当时判断。

写回对象可追加 `deadEnds`，使用稳定 `key` 提议选项或整线死局，记录完整 `reason` 及已有假设依赖。`state.deadEnds` 分配稳定 `eN` 身份，状态为 `pending`、`confirmed`、`rejected` 或 `review`；依赖推翻时同一条死局转为 `review`，不另建身份。轮次 `D:` 条目仍通过作答确认，不新增单独确认接口。下一轮按 D → U → A → S → Q 组成；死局拒绝只向页面提供理由和假设原文等安全参数，用当前语言呈现。

新轮次在 `done` / `void` 时冻结完整上下文；历史读取带 `historical: true`，保留原 `items`、`answeredItems`、回答、说明及 `writeback`。旧日志没有冻结上下文时仍可兼容推导。轮次所属线与页面当前线分开：后台领取、作答、写回及自动检查点归本轮所属线，批注有效性按页面当前线同步。`closed` 拒绝作答和状态修改，重新打开会排队新请求；没有分析删除操作。

JSON 与 Markdown 导出都不包含分析事件、问答、问题树或轮次队列。批注 JSON 可保留 `held` 及来源、依据引用，但不能据此恢复分析；JSON 仅备份批注与常驻技能，不是整份审阅归档，完整保留需备份审阅目录。Markdown 沿用旧文法，不保留分析关联和搁住原因，导入后按未处理批注解释。

## 审阅目录里的文件

| 文件 | 谁写 | 用途 |
|---|---|---|
| `index.html` | build_viewer.py | 对照页 |
| `review.json` | build_viewer.py | 审阅标识 `reviewId`（页面据此认服务，重建不变；目录被复制后重建会换新）、审阅名 `name`（`--name`，默认取标题）、导出标签 `namespace`（审阅名 + 标识前 4 位）、`reviewDir`、页码/键/标题、源文件路径（`sources` 相对审阅目录，`sourcesAbs` 绝对路径兜底）、构建参数（`--rebuild` 用） |
| `notes.json` | 服务 / notes.py | 批注当前状态（以它为准） |
| `notes.jsonl` | 服务 / notes.py | 操作日志 |
| `handoff.json` | 服务 / notes.py | 改稿提交、处理状态与共享交出序号 |
| `analysis/<分析标识>/events.jsonl` | 服务 / notes.py | 只追加的分析事件，推导问题树、轮次、决策与结论草稿 |
| `analysis/counter.json` | 服务 / notes.py | 审阅内唯一轮次号分配 |
| `skills.json` | 生成器 / 服务 / notes.py | 技能目录与常驻技能（可选） |
| `.skill-sources.json` | 生成器 / notes.py | 技能扫描来源、手工覆盖与排除项（可选） |
| `.snapshots/batch-N/{base,draft}/` | notes.py | 初改前后的源文件快照，`notes.py diff` 用（含源文件内容，按经营数据对待） |
| `server.log` | 服务 | 启停、改动、拒绝原因代码、错误（请求字段已过滤，但仍属不可信数据） |
| `.annotation-server.json` | 服务 | 运行中的 pid、端口、地址、是否配了钩子；停止时删除 |
| `.annotation-stop.json` | `serve.py stop` / 服务 | 审阅级停止标识、`stopping` / `stopped` / `incomplete` 状态与各组件确认；私有原子写入，显式启动才解除停止，不写入台账 |
| `.annotation-wait.<pid>.json` | notes.py wait | 有代理在等这份审阅（pid、阶段 draft / review / any），每个 `wait` 进程一个；结束（含 Ctrl+C、SIGTERM）时删除，进程已退出的视为没有 |
| `.notes.lock` | 服务 / notes.py | 文件锁 |

这些文件都可能含被审阅材料的内容，跟材料一样对待：不进 git、不外发。工具新建的文件权限为 0600。

## 审阅台账（审阅目录之外）

`~/.annotation-everything/reviews.json`（环境变量 `AE_HOME` 可改目录；目录 0700、文件 0600），按审阅标识记录：

```json
{"version": 1, "reviews": {"acd0afb38465ebe1": {"dir": "/…/v7-review", "port": 8765}}}
```

- 只存标识、审阅目录绝对路径、专属端口（`null` 表示还没启动过）。审阅名、运行与提交状态由 `serve.py list` 现场从审阅目录读。
- 生成对照页、启动服务、运行 notes.py 时登记；目录搬家后按标识更新路径。专属端口第一次 `serve` 时分配，只有 `serve.py forget` 才释放。
- 写入在 `~/.annotation-everything/.reviews.lock` 文件锁下读改写，原子替换；读不加锁。文件损坏时不会被覆盖，相关命令只警告、照常干活。**不要手改**，用 `list` / `forget` / `--claim-port`。
- 复制出来的审阅目录（原目录还在、标识相同）不会顶替原条目，`--rebuild` 换了新标识后才登记。

## 导出与导入（批注摘要）

页面「批注 → 复制全部 / 下载 Markdown / 下载 JSON」与 `notes.py export` 输出同一种文法；页面「导入…」与 `notes.py import` 读它。页面和命令行各有一份实现，遵守同一份规范（Python 实现在 `scripts/export_format.py`，页面在 `viewer_template.html` 的「导出 / 导入」一节）。

```markdown
<!-- annotation-everything export v1 lang=zh-CN -->      ← 第一行：格式版本与内容语言
# 批注 · 示例报告
导出时间：2026-10-31 09:34:00；共 5 条（未处理 3），涉及 2 页
报告上的标注：标点是一个点，框选是一块区域；坐标为相对页面左上角的百分比（横向 x、纵向 y，宽、高）。
常驻技能（每条批注按范围处理前都先调用）：humanizer-zh（讲稿）

## 第 3 页｜结论：收入增长 12%
1. 【报告·标点 1（x 38.8%，y 59.8%）】把 +12% 改成「同比 +12%」
   第二行也要改
   > 引用（报告）：+12%
2. 【讲稿·框选 2（x 5%，y 20%，宽 60%，高 30%）】【已初改·待复核】改为：删掉整段
   > 引用（讲稿）：原句甲乙
   初改说明：已缩短
   风格优化：框选处
   位置线索：table「渠道」
   技能：humanizer-zh、dataviz
   ↔ 另一侧（报告·相同）：对应句子
```

英文导出是同一份文法的对照翻译，只翻译系统包装文字；用户正文、引用、处理说明和技能名称保持原文：

```markdown
<!-- annotation-everything export v1 lang=en -->
# Annotations · Sample review
Exported 2026-10-31 09:34:00; 5 notes (3 open) across 2 pages
Marks on the report: a pin is a point and a region is a box; coordinates are percentages from the top-left corner of the page (x across, y down, width, height).
Persistent skills (run before every note within scope): humanizer-zh (Script)

## Page 3 | Conclusion: revenue up 12% YoY
1. [Report · Pin 1 (x 38.8%, y 59.8%)] Change +12% to "YoY +12%"
   > Quote (Report): +12%
2. [Script · Region 2 (x 5%, y 20%, w 60%, h 30%)] [Drafted] 改为：删掉整段
   > Quote (Script): 原句甲乙
   Draft note: 已缩短
   Style: Region
   Position hint: table「渠道」
   Skills: humanizer-zh, dataviz
   ↔ Other side (Report · identical): 对应句子
```

- 第一行的标记声明格式版本 `v1` 与内容语言（`zh-CN` / `en`）。 开头三行（标题、计数、标注说明）只给人看，不参与解析；其中「未处理 / open」与页面一致，把已初改·待复核也算作还没定稿。**解析按标记选文法，与导入界面的语言无关**：中文导出在英文界面、英文导出在中文界面都能导入，落库的协议值一致。
- 文法里的字面量（页头、`【】` / `[]`、`引用` / `Quote`、`风格优化` / `Style` 等）写死在实现里，**不从界面词典取**。改界面文案不会改变已发布的交换文法；要改文法就升版本号，旧工具会明确报「不支持 v2」而不是猜。
- 中文文法与 0.3 以来的旧格式逐字相同，只多一个标记行，所以旧工具读新的中文导出与读旧导出没有区别。**旧工具读不懂英文导出**：它找不到 `## 第 N 页｜` 页头，会报「没找到批注」。升级 `notes.py` 和对照页到同一版本即可。
- 没有标记的文字按旧中文格式读；没有标记又读不出批注的算「无法识别」，导入时给出明确提示，不猜、不静默丢弃。
- 对象为空时写固定字面量 `未指定`（两种语言都一样，它是文法的一部分，不是界面文案），导入时原样成为 `target`。

### Markdown 的往返范围

逐项声明（都有对得上的合成用例，见 `tests/test_export_format.py` 与 `tests/e2e_browser.py` 的 `export_import_checks`）：

| 字段 | 写法 | 往返 |
|---|---|---|
| 页码与页标题 | `## 第 3 页｜标题` / `## Page 3 \| Title` | 支持 |
| 对象 | `【报告】` / `[Report]`，`报告` / `讲稿` / `报告和讲稿` | 支持，导入还原成中文存储值 |
| 正文（含多行） | 条目的方括号之后；换行后缩进 3 空格 | 支持，但受下面「三条限制」约束 |
| 状态 | `【已处理】` / `[Resolved] `、`【已初改·待复核】` / `[Drafted] ` | 支持 |
| 标注 | `·标点 N（x ..%，y ..%）` / ` · Pin N (x ..%, y ..%)`；框选同理，带宽高 | 支持，坐标数字不改写 |
| 引用与来源 | `> 引用（报告）：` / `> Quote (Report): ` | 支持；引用里的换行折成一行 |
| 处理说明 | `处理说明：` / `初改说明：`，英文 `Resolution: ` / `Draft note: ` | 支持 |
| 风格优化 | `风格优化：整页 / 标点处 / 框选处` / `Style: Whole page / Pin / Region` | 只写范围标签；0.3 那种把坐标写在 `区域（x ..%）` 里的写法只读不写 |
| 位置线索 | `位置线索：` / `Position hint: ` | 支持 |
| DOCX 段落定位 | `DOCX 定位：v1 引用 · 段落 6（paraId 1A000007，摘要 54f68cf0074a） · 字符 12–30 · 运行 1 · 来源 选区 · 指纹 …` / `DOCX location: v1 quote · paragraph 6 (paraId 1A000007, digest 54f68cf0074a) · chars 12–30 · run 1 · source selection · fingerprint …` | 支持 v1；更高版本只写版本号，完整内容在 JSON 备份里，导入时略过这一行 |
| 本条指定的技能 | `技能：` / `Skills: ` | 支持；带 `（常驻·范围）` 来源标签的项不写进单条（常驻技能走开头那一行） |
| 另一侧对应句 | `↔ 另一侧（报告·相同）：` / `↔ Other side (Report · identical): ` | 支持 |
| 常驻技能 | 开头 `常驻技能（…）：` / `Persistent skills (…): ` | 支持，范围还原成 `all` / `报告` / `讲稿` / `style` |
| 痕迹、孤立页警告、页面键不一致警告、来源标签 | — | **不进交换格式**；它们在 `notes.py show` 的阅读输出里 |

Markdown 不是完整审阅归档：不保存 `id`、时间戳、处理痕迹和批次。JSON 保留完整批注结构，但不包含分析、提交记录或源文件快照；完整审阅需备份审阅目录。

#### 从 0.3 继承下来的三条限制（都有用例钉住，改动要先改测试）

批注正文是自由文本，而元信息行和正文共用同一套「3 空格缩进」，所以下面三种写法会被按元信息读。**这是 0.3 以来就有的取舍，不是本次引入的回归**；没有去改它，是因为改解析规则会动到已经发布的中文导入文法（旧工具读新导出必须和读旧导出一样），而本 issue 允许把兼容限制记录下来。三种情况页面和命令行行为完全一致。

1. **正文里某一行（去掉缩进后）读起来像元信息行**——`风格优化：…`、`处理说明：…` / `初改说明：…`、`技能：…`、`位置线索：…`、`↔ 另一侧（…）：…`、`> 引用…`——这一行不再算正文，并写进对应字段。导出的元信息行排在正文续行之后，所以批注真有的字段不会被同形的正文行改掉；但批注**本来没有**这个字段时，会凭正文造出一个来：引用被造出来会改掉重复判定，快捷修改的 `edit.from` 也会跟着变。「先一句结论、下面 `> 引用（…）：原句`」这种写法请改用 JSON 导出，或把引用行挪到批注之外。
2. **多行正文里的空行会被丢掉**（导出时空行也写 3 空格，导入时空白行不算续行）；正文末尾的换行同样不保留。
3. **正文以状态标记开头**（`【已处理】` / `【已初改·待复核】`，英文 `[Resolved] ` / `[Drafted] `）时，开头那段会被当成状态——一条未处理意见可能被导成已处理，之后默认导出就不再包含它。写完批注尽量避免以这几个字开头。

要原样保留这类内容，用 JSON 导出（`notes` 是原始对象，不做任何文本解析）。


### JSON

页面「下载 JSON」与 `notes.py export --format json`：

```json
{"version": 1, "reviewId": "…", "name": "审阅名", "namespace": "导出标签", "lang": "en",
 "notes": [ …整份 notes，协议值原样… ], "alwaysSkills": [ … ]}
```

- `notes` 是 `notes.json` 里的原始对象，`target`、`status`、`scope` 等协议值不改写；`lang` 记录导出时的内容语言，是新增的元信息，**旧消费者按 key 读取、直接忽略即可**。
- 页面下载的 JSON 始终是整份批注；命令行的 `--all` / `--page N` 对 Markdown 和 JSON 一样有效，默认只含未处理。
- 身份核对、去重、更新与冲突提示不变：`reviewId` 与目标审阅不同时 `notes.py import` 拒绝（`--force` 强制），页面「导入…」先问；同 id 而导入方更新得更晚的才覆盖，否则跳过；内容相同（页码、对象、正文、引用、标注、是否风格优化）算重复。
- 用户按已支持方式编辑导出后再导入，**以看得见的正文为准**：Markdown 的正文是唯一内容，没有隐藏副本；JSON 里只改 `text` 时，结构化的快捷修改（`edit`）从 `text` 重新推导，不会写回改之前的内容。
- 「未能保存」与「其他审阅的未保存改动」的下载另带 `ops`（原始改动队列，供核对）。

### 命令行

```bash
python3 $S/notes.py export 审阅目录 [--lang zh-CN|en|auto] [--format md|json] [--all] [--page N] [--out 文件]
python3 $S/notes.py import 审阅目录 批注文件.md|json [--force]
```

`--lang` 的优先级与 `show --locale` 相同：`--lang` > `AE_LOCALE` > 审阅里存的 `locale` > 中文。默认只导未处理，`--all` 含已处理。不带 `--out` 时打印到标准输出；`--out` 指定完整文件路径，不接受目录。页面下载的文件名保留审阅识别 `namespace` 和 12 位紧凑 UTC 时间，只有按语言换的前缀会变（`批注_` / `annotations_`）；导出时间写本地时间 `YYYY-MM-DD HH:MM:SS`，两种语言同一格式，时间戳、数值和坐标都不随地区格式改变。
