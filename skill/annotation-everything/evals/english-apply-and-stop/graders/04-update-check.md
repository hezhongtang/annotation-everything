---
type: llm
focus: trace
weight: 1
---

Before the review workflow, the agent runs `update.py auto` from the loaded skill copy. If it reports an update, the agent reloads the skill instructions and tells the user; an offline check does not prevent the review.
