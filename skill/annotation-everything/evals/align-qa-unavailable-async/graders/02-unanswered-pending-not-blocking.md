---
type: llm
focus: artifact
weight: 1
---

report-record.json 里 request.qa 写明 available=false 及非空 unavailableReason，未确认的 density/visual/navigation 维度标为 source="default"、status="pending"（非 "user"），verdict.draftComplete=true 而 verdict.deliverable!=true；report_check.py 返回 0 且摘要与审阅输出明确列出「待确认」项。
