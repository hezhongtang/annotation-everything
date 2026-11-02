---
type: llm
focus: trace
weight: 1
---

执行审阅前，从已加载技能副本的 scripts 运行 `update.py auto`；若命令报告完成更新，则重新读取技能说明，并向用户说明更新结果。离线或检查失败时继续审阅。
