---
type: llm
focus: artifact
weight: 1
---

材料旁存在 report-record.json，schema 为 ae-report-record/1，links 的 sha256 与当前报告文件一致，verdict.draftComplete 为 true 且 deliverable 不是 true；Agent 实际运行了技能里的 report_check.py 公开核验入口，程序检查全部通过，且回复如实说明内容核验与视觉核验尚未执行，没有声称「报告可交付」
