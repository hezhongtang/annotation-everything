---
type: llm
focus: artifact
weight: 1
---

在用户未确认放弃任一约束前，report-record.json 对冲突维度标为 status="conflict"（若有偏离另在 deviations 如实说明），content.requirements 未标为 pass，verdict.deliverable 不为 true；report_check.py 返回的结果中 coverage.content 非 covered、摘要明确显示未解决冲突或偏离。
