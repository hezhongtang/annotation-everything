---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

materials/ 里有已交付过的三季度回顾：报告（report.html）、讲稿（script.md）、核验记录（report-record.json）和经营笔记（q3-notes.md）。用户看完对照页提交了两条批注：

1. 三季度收入最新口径是 115 万元（笔记已更新），把结论页和讲稿里的增速都改过来。
2. 第 2 页「外卖渠道占比 38%，同比继续上升」的同比口径帮我核对一下。

请按批注改稿：改源文件、维护核验记录（更新产物哈希、受影响的结论引用与计算声明、检查结果和 revision 声明），跑一遍核验入口。然后告诉我：改了哪些、本次重新核验了什么、哪些没法确认、现在能不能交付。

Input files: materials/q3-notes.md, materials/brief.md, materials/report.html, materials/script.md, materials/report-record.json
