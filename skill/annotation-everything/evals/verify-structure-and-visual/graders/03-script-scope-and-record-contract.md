---
type: llm
focus: artifact
weight: 1
---

记录契约与讲稿关联项的适用性：report-record.json 为 ae-report-record/1，links 带报告 sha256，request 记录了核心问题与篇幅（3 页）；不需要讲稿，记录不得把 content.script-consistency 或 structure.length 声明为 pass（无讲稿时这两项不适用，声明通过属于虚假的已检查），不适用项保持未声明或 not_run；request.length 为 3 页且报告正好 3 页，篇幅约束一致；报告引用页（plan / channel / why）都在记录 findings 的 pages 里存在
