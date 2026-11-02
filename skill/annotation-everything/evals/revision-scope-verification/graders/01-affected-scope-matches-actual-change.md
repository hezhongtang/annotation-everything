---
type: llm
focus: artifact
weight: 1
---

materials/ 旁的核验记录（report-record.json）在改稿后维护了受影响的核验信息，影响范围与实际改动对得上：收入增速改稿后，收收入的 finding 更新为增速 15%（calculation 的 inputs 是 100 与 115、expected 15，或等价声明），refs.quote 更新为材料原文「三季度收入 115 万元，上年同期 100 万元」并如实标 found；links.sha256 的 report 与 script 都更新为改后文件的真实哈希（freshness 不再 stale）。revision 声明存在且 basis 写明影响范围怎么判定；revision.rechecked 列出实际重新核验的项，至少覆盖 content.claims、content.numbers-charts、content.cross-page、content.script-consistency 中实际受影响的项，且每项在 checks[] 里有 pass/fail 的实际结果；没有把与改动无关的项（如 structure.headings、structure.dup-gaps）塞进 rechecked 凑数，也没有把全部 14 项都重做一遍（普通改稿不做全量勾稽）。report_check.py 的运行结果显示 record.revision 通过（或如实登记的失败）。
