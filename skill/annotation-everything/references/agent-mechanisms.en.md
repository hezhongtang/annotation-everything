# Agent mechanisms

[简体中文](agent-mechanisms.zh-CN.md)

Commands, files, and exit codes are the same for every agent. Use this table when configuring a waiter, invoking annotation skills, delegating work, or choosing a worker name. The [shared protocol](protocol.md) governs permissions, stage boundaries, and safety. Follow the [English workflow](workflow.en.md) for the complete sequence.

| Step | Claude Code | Other agents |
|---|---|---|
| Waiting and wake-up | Run `serve.py` and `notes.py wait` as background Bash tasks. The host's background-task completion notification wakes the session when `wait` ends. Read its exit code and output before handling the accepted batch. | Use a background waiter if the host supplies completion notifications. Otherwise, repeat `notes.py wait "$R" --timeout 30` (exit 3 on timeout), or poll `notes.py claim "$R"` (JSON and exit 0 for an accepted submission; exit 3 when none is waiting). The host or an active session must schedule each poll. A host that accepts external events can use `serve.py "$R" --on-handoff "command"`. |
| Skill invocation | Use the Skill tool to invoke the named annotation skills and applicable standing skills. | Use the host's skill-loading mechanism. Find the named `SKILL.md` and follow it. If it is unavailable, report that and continue under the shared protocol. |
| Delegation | When the accepted batch allows subagents, use the Agent tool. If supported, its `model` parameter can select a lower-cost initial editor and a stronger reviewer. | When allowed, use the host's subagent or separate-session mechanism. If neither is available, the main session handles the work personally. |
| Worker name (`--by`) | `notes.py` uses `claude` by default when the `CLAUDECODE` environment variable is nonempty. | Without that environment variable, the default is `agent`. |

Set an explicit name such as `--by initial-editor` or `--by reviewer`. The page and processing trail show it, so the roles remain distinguishable.

See [workflow section 4](workflow.en.md#4-start-the-service-and-wait) for hook inputs, the working directory, and snapshot handling. Follow [section 6](workflow.en.md#6-initial-edit-review-and-delegation) when passing the accepted requirements, sequence, and skills to workers. Keep its main-session completion duties and user-controlled stage boundaries.
