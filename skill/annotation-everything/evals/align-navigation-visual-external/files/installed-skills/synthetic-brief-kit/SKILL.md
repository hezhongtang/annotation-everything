---
name: synthetic-brief-kit
description: 合成外部生成技能（仅用于 AE 的测试与评测，issue #74）：把含「三季度收入 / 上年同期收入」数字的材料生成两页简报式逐页 HTML、讲稿与核验记录。Synthetic external generation skill for AE tests and evals; turns material with quarterly revenue figures into a two-page page-per-section brief with a speaker script and a verification record. Not a real remote skill.
---

# synthetic-brief-kit（合成外部技能）

这是一个受控合成的外部生成技能，用来验证 annotation-everything 的外部方法接入约定（issue #74）。它不是真实远程技能，不联网、不安装任何依赖。

## 用法

1. 实际运行本技能脚本生成产物（宣称采用了本技能，就必须真的运行过）：

```bash
python3 <本技能目录>/scripts/make_report.py --materials 材料.md --out 输出目录
```

2. 产物遵守 annotation-everything 的产物约定：逐页 `<section class="page" data-page="N" data-key="稳定键">`、可选讲稿逐页对应、`ae-report-record/1` 核验记录（`method` 写明本技能名称、版本与实际调用方式，外部方法必须带 `invocation`）。
3. 用 annotation-everything 的 `report_check.py` 对产物做同一验收；未执行的核验如实写 `not_run`，不能因为用了外部技能就放宽验收。
4. 版式是本技能自己的简报风格（`xs-banner` 标记），但保留 `.ae-source` / `.ae-gap` / `.ae-todo` 语义类。
