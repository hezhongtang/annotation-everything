---
type: llm
focus: artifact
weight: 1
---

若生成了 report-record.json：schema 为 ae-report-record/1，sources 对三份材料全部登记 unreadable 且各有 reason（说明无 OCR / 扫描页未识别），verdict.draftComplete 不是 true；若运行了 report_check.py：输出显示初稿未完成（读取登记判为全部不可读 / record.reading-blocked）
