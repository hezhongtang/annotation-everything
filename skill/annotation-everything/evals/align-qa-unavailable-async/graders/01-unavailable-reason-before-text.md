---
type: llm
focus: behavior
weight: 1
---

宿主没有交互式问答工具（allowed_tools 不含 AskUserQuestion 且输入指出无问答工具）时，Agent 在正文中先说明具体不可用原因（而非静默直接开写或只发无理由问题），并在纯文本里列出缺项及推荐默认方案；随后先做材料读取等独立工作，并按明确默认方案完成初稿生成。
