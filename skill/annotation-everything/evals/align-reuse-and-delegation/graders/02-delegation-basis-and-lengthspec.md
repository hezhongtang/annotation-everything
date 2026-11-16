---
type: llm
focus: artifact
weight: 1
---

report-record.json 满足：1) request.alignment.navigation 记为 delegated 且 basis 写明具体采用方案（如目录+结论/补充页双向跳转）；2) request.lengthSpec 记录 nature="hard" 且 includeSupplements=false；3) 若生成补充页，标有 data-kind="supplement"，且 report_check.py 下 record.length、record.alignment、report.navigation 均为 pass。
