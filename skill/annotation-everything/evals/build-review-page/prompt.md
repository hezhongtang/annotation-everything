---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

我手上有一份季度经营汇报：report.html 是逐页的报告（每页一个 section），script.md 是配套的逐页讲稿。我想在浏览器里一边看报告一边看讲稿，逐页写批注，批注最好能自动存到本地文件里，回头直接交给你改，不要让我复制粘贴。帮我把审阅页生成到 review/ 目录里。服务你先别帮我挂着不关，告诉我怎么启动、批注会存在哪个文件就行。

Input files: report.html, script.md
