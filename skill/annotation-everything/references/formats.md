# 输入与输出格式

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

不是这种结构的材料（PPT、PDF、普通网页）：先转成逐页 section 结构再用本 skill（例如每页一张图片放进 section，也能在上面标注）。

PPTX 转图前，运行 `python3 $S/check_pptx_fonts.py 原始.pptx`。检查器读取幻灯片、母版、布局和主题中的字体声明，并用本机 `fc-list` 枚举字体；不会解压或修改原文件。转出 PDF 后，运行 `python3 $S/check_pptx_fonts.py 原始.pptx --pdf 转换后.pdf`，用 `pdffonts` 列出字体名称及 `emb`（是否嵌入）、`sub`（是否子集）等证据。退出码 0 表示本次检查没有发现差异，1 表示有待核查或未知项，2 表示输入或所需工具出错。未检出仅表示这次枚举没找到，主题字体可能未被使用；主题里按文字脚本列出的备选字体（`<a:font script=…>`）单独汇总，不计入未检出，中日韩文字或阿拉伯、希伯来、南亚等文字没有显式指定对应的东亚或复杂文种字体时记为未知；别名、样式名、混合回退、轮廓化文字或图片可能无法按名称对应，名称一致也不能证明逐字形渲染正确。没有 `fc-list` 或 `pdffonts` 时分别安装 fontconfig 或 poppler，或人工核对，不要把未知当成通过。补齐字体并重新转换后，逐页检查换行、间距和遮挡。

## 讲稿 Markdown

```markdown
# 讲稿：示例报告          ← 一级标题（报告没有 <title> 时作为页面标题）

## 使用说明                ← 第一个「### 第」之前的内容进「说明」浮层（支持标题、列表、表格）
- 每页先讲结论。

### 第 3 页｜结论：收入增长 12%
**时长**：1.5 分钟         ← 支持「N 分钟」「N 秒」「1 分 30 秒」，用于累计用时
**讲法**：
逐行写，每行一段。
**过渡**：收入结构上……
**可能被问**：
- 问：新店贡献多少？答：约三分之二。
```

- 页头的分隔符全角「｜」或半角「|」都可以，字段冒号全角半角都可以。
- 没有任何字段的页，整页正文按「讲法」显示。
- 讲稿页码须与报告页序一一对应；不一致时生成器列出差异并退出，`--lenient` 以报告为准。

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
      "status": "resolved",                 // open 未处理 / drafted 已初改·待复核（两段流程）/ resolved 已处理
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
      "orphan": true                             // 重建后原页面已不存在（待确认），确认后去掉
    }
  ]
}
```

- 快捷修改的 `text` 是固定说法（「删除引用的这段文字」「改为：…」），`quote` 等于 `edit.from`，旧工具照样能看懂。
- 生成对照页时若审阅目录里已有批注：没有 `key` 的按上一版 review.json 的页码补上，再按 `key` 把 `page` 改成新版的页码；`key` 在新版里找不到的标 `orphan`。所以 `data-key` 要稳定、唯一（重复的键不做校正）。
- 风格优化的 `target` 固定为报告；`text` 是用户写的方向，没写时为「风格优化：整页 / 框选处 / 标点处」。
- 0.3 及更早只有风格优化能框选，框存在 `style.region` 里；读入时自动挪到顶层 `region`（下次写盘落成新格式），原来的风格范围和点都保留——所以旧批注可能点和框并存、或「整页」风格优化带一个点。0.3 页面发来的 `{style:{region}}` 改动按顶层 `region` 处理。
- 批注归哪一批：`resolve` / `review` 不带 `--seq` 时按批注归属找（待复核的初改 → 它的初改批次；提交时包含它的在途批次，复核中优先、同级取最早），不再取「最新一批」；同时有几批处理中时 `done` 必须带 `--seq`。别的批次还在等复核的初改，不能在另一批里处理。
- 页面新建和「重新标注」时 `pin`、`region` 一起发（另一个为 `null`），所以新批注只有一种标注；拖动只发被拖的那一个，另一个不动。改动动了标注时，风格范围跟着标注走（只拖动、原范围是整页的保持整页）。
- 页面轮询的 `GET /api/notes` 只带最新一批（`handoff.latest`、计数、`total`）；完整处理记录 `history` 由 `GET /api/handoff` 和 `notes.py batches` 提供。服务响应里带 `version` 与 `features`（含 `marks`、`batches`），页面据此识别旧版服务。

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

源文件快照：代理接收初改时把 review.json 里的报告与讲稿源文件存到审阅目录 `.snapshots/batch-N/base/`，初改收尾时存 `draft/`（目录 0700、文件 0600，只留最近 30 批），`notes.py diff` 据此按页对比。审阅目录含经营数据时，快照同样不要外传。

处理记录（页面「处理记录」/ `notes.py batches`）：每次提交算一批。服务返回的状态里 `history` 是全部批次（新的在前，不含 `openIds`）；处理中的批次按当前进度实时统计（`live`），0.3 留下没记 `resolvedIds` 的旧批次按「接收到收尾之间代理标的」推算（`estimated`）。

## skills.json（技能目录，可选）

`{"version": 1, "skills": [{"name": "humanizer-zh", "desc": "中文润色"}], "always": [{"name": "humanizer-zh", "scope": "讲稿"}]}`。

- `skills`：技能目录。生成和重建时从常见技能目录及插件缓存读取名称、说明；插件技能使用 `插件:技能` 调用名，插件缓存不代表当前已启用。可用 `--skills-from` 指定来源或 `--no-scan-skills` 禁用扫描，也可用 `notes.py skills` 手工维护。页面「技能」输入栏把目录和用过的技能列为候选。
- `.skill-sources.json`：私有来源记录，保存扫描结果、手工覆盖和排除项；来源路径只保存哈希。目录更新与常驻技能使用同一把锁。手工移除不会在重建时恢复；扫描不完整时保留该来源旧记录并警告。`skills.json` 结构不变，但旧版 `notes.py` 写回超过 200 项的目录可能截断，应先升级。
- `always`：常驻技能，范围内的每条批注处理前都要先调用；`scope` 为 `all`（全部批注）/ `报告` / `讲稿`（对象含该侧，「报告和讲稿」两侧都算）/ `style`（风格优化），最多 20 个。页面「常驻技能」（`POST /api/skills/always`）或 `notes.py skills --always …` 设置；`show` / `wait` 把适用的常驻技能列进每条批注的「技能」；每批提交时把当时的名单记进 handoff 的 `alwaysSkills`。
- 两者都在生成对照页时嵌进页面，本地文件模式同样可见。文件模式下改常驻技能只存本浏览器（之后 Agent 改了名单并重建，以新名单为准）；「复制全部 / 下载 Markdown」的开头一行、「下载 JSON」的 `alwaysSkills` 都会带上，`notes.py import` 或页面「导入…」时随之写入（审阅目录里已有不同的名单时，命令行只提示、页面先询问）。
- 读 skills.json 时坏项跳过、好项保留（字符串项当「全部批注」）；页面和命令行写入时严格校验：一项里写了几个名字会拆开，超过 20 个报错。命令行同一条命令里先删（`--always-remove`）后加（`--always-add`），改范围就是删了再加。

## 审阅目录里的文件

| 文件 | 谁写 | 用途 |
|---|---|---|
| `index.html` | build_viewer.py | 对照页 |
| `review.json` | build_viewer.py | 审阅标识 `reviewId`（页面据此认服务，重建不变；目录被复制后重建会换新）、审阅名 `name`（`--name`，默认取标题）、导出标签 `namespace`（审阅名 + 标识前 4 位）、`reviewDir`、页码/键/标题、源文件路径（`sources` 相对审阅目录，`sourcesAbs` 绝对路径兜底）、构建参数（`--rebuild` 用） |
| `notes.json` | 服务 / notes.py | 批注当前状态（以它为准） |
| `notes.jsonl` | 服务 / notes.py | 操作日志 |
| `handoff.json` | 服务 / notes.py | 「交给 Agent」的提交与处理状态 |
| `skills.json` | 生成器 / 服务 / notes.py | 技能目录与常驻技能（可选） |
| `.skill-sources.json` | 生成器 / notes.py | 技能扫描来源、手工覆盖与排除项（可选） |
| `.snapshots/batch-N/{base,draft}/` | notes.py | 初改前后的源文件快照，`notes.py diff` 用（含源文件内容，按经营数据对待） |
| `server.log` | 服务 | 启停、改动、拒绝原因代码、错误（请求字段已过滤，但仍属不可信数据） |
| `.annotation-server.json` | 服务 | 运行中的 pid、端口、地址、是否配了钩子；停止时删除 |
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

## 导出与救回的 JSON

页面「下载 JSON」：`{"version": 1, "reviewId": …, "name": 审阅名, "namespace": 导出标签, "notes": [...], "alwaysSkills": [...]}`。「未能保存」与「其他审阅的未保存改动」的下载另带 `ops`（原始改动队列，供核对）。`reviewId` 与目标审阅不同时，`notes.py import` 拒绝（`--force` 强制），页面「导入…」先确认。
