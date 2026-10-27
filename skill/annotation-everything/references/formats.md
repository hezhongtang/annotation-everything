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

- 所有 `<style>` 和本地 `<link rel="stylesheet">` 会被收进对照页（Shadow DOM 隔离，不影响外壳）；样式里的 `:root`、`html` 会改写成 `:host`，`body` 改写成页面舞台，变量和字号照常生效。`<script>` 与内联事件（`onclick` 等）不执行。
- 本地相对路径的图片、字体（`src`、`srcset`、`poster`、CSS `url()`）会内联成 data URI，单个上限 10 MB；外链（http/https）不加载，生成时打印警告。
- section 可以嵌套别的 section（卡片等），按层级识别；class 必须含独立的 `page`（`page-wrapper` 之类不算页）。页码重复或不是整数会报错。
- 页内链接写 `href="#page-<key>"` 时，在对照页里点击会跳到那一页。
- 板块导航推断规则：取带 `data-range-start` / `data-range-end`（值为页面键）的链接；有 `.toc-label` 的链接时只取这些主条目；嵌套在别的范围里的子范围不进导航条。
- 缺 `data-key` 时用 `id` 去掉 `page-` 前缀，再不行用 `p<页码>`。没有讲稿时，页标题取该页第一个 h1–h3。

不是这种结构的材料（PPT、PDF、普通网页）：先转成逐页 section 结构再用本 skill（例如每页一张图片放进 section，也能标点批注）。

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
      "pin": {"x": 38.8, "y": 59.8},        // 页面上的标点（百分比），可为 null
      "status": "resolved",                 // open / resolved
      "at": "2026-10-27T10:19:14.129Z",
      "updatedAt": "…",                     // 编辑过才有
      "resolvedAt": "…", "resolvedBy": "claude", "resolution": "标题与 KPI 已改"
    }
  ]
}
```

`notes.jsonl` 每行一条操作：`{"ts","rev","op":"add|update|resolve|reopen|delete|import","by":"page|claude|cli-import|page-import","id",…}`。

## 审阅目录里的文件

| 文件 | 谁写 | 用途 |
|---|---|---|
| `index.html` | build_viewer.py | 对照页 |
| `review.json` | build_viewer.py | 审阅标识 `reviewId`（页面据此认服务，重建不变）、页码/键/标题、源文件路径（`sources` 相对审阅目录，`sourcesAbs` 绝对路径兜底）、构建参数（`--rebuild` 用） |
| `notes.json` | 服务 / notes.py | 批注当前状态（以它为准） |
| `notes.jsonl` | 服务 / notes.py | 操作日志 |
| `server.log` | 服务 | 启停、改动、拒绝原因代码、错误（请求字段已过滤，但仍属不可信数据） |
| `.annotation-server.json` | 服务 | 运行中的 pid、端口、地址；停止时删除 |
| `.notes.lock` | 服务 / notes.py | 文件锁 |

这些文件都可能含被审阅材料的内容，跟材料一样对待：不进 git、不外发。工具新建的文件权限为 0600。
