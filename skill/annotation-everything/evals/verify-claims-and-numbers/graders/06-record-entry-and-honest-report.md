---
type: llm
focus: trace
weight: 1
---

材料旁存在 report-record.json（schema ae-report-record/1）：报告与讲稿的 links.sha256 与当前文件一致，结论引用页都在报告里存在，引用的材料都在 sources 登记且带 sha256，八项 content.* 检查逐项声明（未执行的如实写 not_run）；Agent 实际运行了技能里的 report_check.py 公开核验入口（命令行证据），并如实回报：哪些结论有材料支撑、哪些是推断或待核实、内容与视觉核验的覆盖与缺口、记录文件位置；没有声称「报告可交付」
