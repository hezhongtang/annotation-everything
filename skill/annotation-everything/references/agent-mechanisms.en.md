# Agent mechanisms

## Codex desktop: review-scoped event delivery

When the user requests automatic handling after web submission, the agent configures the skill; the user does not edit Codex configuration.

1. Read `python3 $S/wakeup.py status REVIEW_DIR`. Preserve an existing binding instead of silently switching its receiving chat.
2. Run `python3 $S/wakeup.py bind REVIEW_DIR`. It uses the current host's `CODEX_THREAD_ID`; if unavailable, pass the verified current chat identity with `--thread UUID`. The command checks the local desktop socket and thread owner, then saves private metadata beside the review. `--by Codex` is only an attribution label.
3. Start `python3 $S/serve.py REVIEW_DIR` normally. If an older service exists, verify its identity and avoid interrupting another person's work before stopping and restarting it. Every subsequent normal restart restores the binding. A custom `--on-handoff` conflicts with a saved binding and is rejected; changing delivery method requires an explicit decision, `wakeup.py unbind REVIEW_DIR`, and restart.
4. On receipt, run the supplied `wakeup.py ack ...` command, then `notes.py claim --seq N --by Codex`. Apply the accepted requirements and original completion protocol. Do not start a claiming waiter or scheduler in binding mode. The updated CLI prevents a different chat from claiming the bound review.
5. Verify an actually idle chat: after the previous turn completes, submit through a synthetic review page and confirm a new chat turn, acknowledgement, annotation resolution, and done. Restart the same service normally and repeat. Busy-chat delivery, owner discovery, and process exit 0 are not idle-wakeup proof.

`wakeup.py status` and the page distinguish `accepted` (desktop accepted delivery) from `received` (the chat executed acknowledgement). Failed deliveries can retry on service restart or another user submission. Interrupted sending or a lost transport acknowledgement stays `unconfirmed`; inspect chat and batch state before any replay. Startup recovery handles only pending submissions/review requests, never work already claimed by another agent.

This adapter uses the desktop's existing local IPC, without changing Codex configuration, permissions, models, or application files or starting a second CLI agent. IPC is not a stable public API; the verified environment is macOS desktop 26.1027.51404. An unavailable desktop, missing thread owner, or incompatible protocol produces a visible error and preserves annotations. Binding applies only to its original review identity and directory; verify and rebind moved/copied reviews. Unbinding preserves annotations and delivery history.

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
