---
name: synthetic-brief-lite
description: 合成外部生成技能的精简变体（仅用于 AE 的测试与评测，issue #74）：生成不带来源标注与计算口径的两页简报，用于验证「外部产物缺依据时列为缺口、不放宽验收」。Synthetic limited external generation skill for AE tests and evals; produces a two-page brief without source annotations or calculation basis, used to prove missing evidence is disclosed as gaps under the same acceptance.
---

# synthetic-brief-lite（合成外部技能·精简变体）

这是受控合成的外部生成技能变体（issue #74），代表「能力有限的外部方法」：只出版式，不提供来源标注、计算口径与材料哈希。它如实声明自己的局限，产物仍须走 annotation-everything 的同一核验入口，缺口由核验如实呈现。

## 用法

```bash
python3 <本技能目录>/scripts/make_report.py --materials 材料.md --out 输出目录
```

## 已知局限（如实披露，不是可以隐瞒的缺陷）

- 报告不带 `.ae-source` 来源标注；核验记录的 findings 不带 `refs` / `support` / `basis` / `calculation`。
- 材料只登记读取状态，不带 `sha256`（无法发现材料事后变化）。
- 以上局限写进记录的 `gaps`；宣称采用了本技能仍须真的运行过脚本（`method.invocation`）。
