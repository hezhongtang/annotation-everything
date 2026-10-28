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
      "status": "resolved",                 // open / resolved
      "at": "2026-10-27T10:19:14.129Z",
      "updatedAt": "…",                     // 编辑过才有
      "resolvedAt": "…", "resolvedBy": "claude", "resolution": "标题与 KPI 已改",
      "resolvedBatch": 3,                   // 代理标已处理时正在处理的批次（handoff 的 seq），处理记录据此统计
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
- 页面新建和「重新标注」时 `pin`、`region` 一起发（另一个为 `null`），所以新批注只有一种标注；拖动只发被拖的那一个，另一个不动。改动动了标注时，风格范围跟着标注走（只拖动、原范围是整页的保持整页）。
- 页面轮询的 `GET /api/notes` 只带最新一批（`handoff.latest`、计数、`total`）；完整处理记录 `history` 由 `GET /api/handoff` 和 `notes.py batches` 提供。服务响应里带 `version` 与 `features`（含 `marks`、`batches`），页面据此识别旧版服务。

`notes.jsonl` 每行一条操作：`{"ts","rev","op":"add|update|resolve|reopen|delete|import|handoff|handoff-update|handoff-claim|handoff-done","by":…,"id"|"seq",…}`。

## handoff.json（「交给 Agent」的提交）

```json
{
  "version": 1, "seq": 3, "updatedAt": "…",
  "items": [
    {"seq": 3, "status": "pending",            // pending 等待接收 → claimed 处理中 → done 已处理
     "at": "…", "by": "page", "message": "先改第 7–12 页",
     "openCount": 5, "openIds": ["…"], "pages": [7, 9, 12], "updates": 1,
     "claimedAt": "…", "claimedBy": "claude", "doneAt": "…", "doneBy": "claude", "summary": "改了 4 条，1 条等你定",
     // 以下由 done 记下（处理记录）：这批里代理标为已处理的批注、涉及页、提交时的批注还剩几条未处理
     "resolvedIds": ["…"], "resolvedCount": 4, "resolvedPages": [7, 9], "leftOpen": 1}
  ]
}
```

不用 `notes.py wait` 的代理：定时运行 `notes.py claim 审阅目录`（有等待中的提交就接收并输出 JSON，退出码 0；没有返回 3），处理完用 `notes.py done` 收尾。可以读这个文件看状态，但**不要直接改它**：写入要经过 notes.py / 服务的文件锁，否则可能覆盖用户刚合并进来的附言。保留最近 200 批。

处理记录（页面「处理记录」/ `notes.py batches`）：每次提交算一批。服务返回的状态里 `history` 是全部批次（新的在前，不含 `openIds`）；处理中的批次按当前进度实时统计（`live`），0.3 留下没记 `resolvedIds` 的旧批次按「接收到收尾之间代理标的」推算（`estimated`）。

## skills.json（技能目录，可选）

`{"version": 1, "skills": [{"name": "humanizer-zh", "desc": "中文润色"}]}`。由代理用 `notes.py skills` 维护，页面「技能」输入栏把它和用过的技能列为候选；生成对照页时也会嵌进页面，本地文件模式同样可用。

## 审阅目录里的文件

| 文件 | 谁写 | 用途 |
|---|---|---|
| `index.html` | build_viewer.py | 对照页 |
| `review.json` | build_viewer.py | 审阅标识 `reviewId`（页面据此认服务，重建不变；目录被复制后重建会换新）、`reviewDir`、页码/键/标题、源文件路径（`sources` 相对审阅目录，`sourcesAbs` 绝对路径兜底）、构建参数（`--rebuild` 用） |
| `notes.json` | 服务 / notes.py | 批注当前状态（以它为准） |
| `notes.jsonl` | 服务 / notes.py | 操作日志 |
| `handoff.json` | 服务 / notes.py | 「交给 Agent」的提交与处理状态 |
| `skills.json` | notes.py | 技能目录（可选） |
| `server.log` | 服务 | 启停、改动、拒绝原因代码、错误（请求字段已过滤，但仍属不可信数据） |
| `.annotation-server.json` | 服务 | 运行中的 pid、端口、地址；停止时删除 |
| `.notes.lock` | 服务 / notes.py | 文件锁 |

这些文件都可能含被审阅材料的内容，跟材料一样对待：不进 git、不外发。工具新建的文件权限为 0600。
