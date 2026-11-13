# Agent mechanisms

Before configuring a receiver or switching sessions, read [Receiver binding and transfer](receiver-binding.en.md). Confirm the receiving session, then choose event delivery or a waiter below. `--by` does not establish session identity.

## Codex desktop: review-scoped event delivery

When the user requests automatic handling after web submission, the agent configures the skill; the user does not edit Codex configuration.

1. Read `python3 $S/wakeup.py status REVIEW_DIR`. Preserve an existing binding instead of silently switching its receiving chat.
2. Run `python3 $S/wakeup.py bind REVIEW_DIR`. It uses the current host's `CODEX_THREAD_ID`; if unavailable, pass the verified current chat identity with `--thread UUID`. The command checks the local desktop socket and thread owner, then saves private metadata beside the review. `--by Codex` is only an attribution label.
3. Start `python3 $S/serve.py REVIEW_DIR` normally. If an older service exists, verify its identity and avoid interrupting another person's work before stopping and restarting it. Every subsequent normal restart restores the binding. A custom `--on-handoff` conflicts with a saved binding and is rejected; changing delivery method requires an explicit decision, `wakeup.py unbind REVIEW_DIR`, and restart.
4. On receipt, run the supplied `wakeup.py ack ...` command, then the supplied claim command: `--seq N` for revision/review, `--events analysis --round N` for analysis. Follow the [analysis guide](analysis.en.md) for a round and the accepted completion protocol for a revision batch. If an exact claim exits 3, handle the actual queue head first. Do not start a claiming waiter or scheduler in binding mode. The updated CLI prevents a different chat from claiming the bound review.
5. Verify an actually idle chat: after the previous turn completes, submit through a synthetic review page and confirm a new chat turn, acknowledgement, annotation resolution, and done. Restart the same service normally and repeat. Busy-chat delivery, owner discovery, and process exit 0 are not idle-wakeup proof.

`wakeup.py status` and the page distinguish `accepted` (desktop accepted delivery) from `received` (the chat executed acknowledgement). Failed deliveries can retry on service restart or another user submission. Interrupted sending or a lost transport acknowledgement stays `unconfirmed`; inspect chat and batch state before any replay. Startup recovery handles pending revision/review batches and queued analysis rounds, never work already claimed.

This adapter uses the desktop's existing local IPC, without changing Codex configuration, permissions, models, or application files or starting a second CLI agent. IPC is not a stable public API; the verified environment is macOS desktop 26.1027.51404. An unavailable desktop, missing thread owner, or incompatible protocol produces a visible error and preserves annotations. Binding applies only to its original review identity and directory; verify and rebind moved/copied reviews. Unbinding preserves annotations and delivery history.

[简体中文](agent-mechanisms.zh-CN.md)

Commands, files, and exit codes are the same for every agent. Use this table when configuring a waiter, invoking annotation skills, delegating work, or choosing a worker name. The [shared protocol](protocol.md) governs permissions, stage boundaries, and safety. Follow the [English workflow](workflow.en.md) for the complete sequence.

| Step | Claude Code | Other agents |
|---|---|---|
| Waiting and wake-up | Run `serve.py` and `notes.py wait --events analysis` as background Bash tasks. The host's background-task completion notification wakes the session when `wait` ends. Read its exit code and output before handling the accepted batch. | Use a background waiter if the host supplies completion notifications. Otherwise, repeat `notes.py wait "$R" --events analysis --timeout 30` (exit 3 on timeout), or poll `notes.py claim "$R" --events analysis` (JSON and exit 0 for an accepted submission; exit 3 when none is waiting). The host or an active session must schedule each poll. A host that accepts external events can use `serve.py "$R" --on-handoff "command"`. |
| Skill invocation | Use the Skill tool to invoke the named annotation skills and applicable standing skills. | Use the host's skill-loading mechanism. Find the named `SKILL.md` and follow it. If it is unavailable, report that and continue under the shared protocol. |
| Delegation | When the accepted batch allows subagents, use the Agent tool. If supported, its `model` parameter can select a lower-cost initial editor and a stronger reviewer. | When allowed, use the host's subagent or separate-session mechanism. If neither is available, the main session handles the work personally. |
| Worker name (`--by`) | `notes.py` uses `claude` by default when the `CLAUDECODE` environment variable is nonempty. | Without that environment variable, the default is `agent`. |

Set an explicit name such as `--by initial-editor` or `--by reviewer`. The page and processing trail show it, so the roles remain distinguishable.

## Overview-home delegated starts

`python3 $S/serve.py overview` (optionally `--under DIR`) opens the standalone overview home; choosing "Start and open" for a stopped review delegates the start to it. Agent-relevant behavior:

- The handoff hook comes only from the overview's command line, `serve.py overview --on-handoff "command arguments"`—never from a web page. The newly started review service inherits the parsed arguments and runs the hook in the overview's starting directory (no shell; relative paths in the arguments resolve there).
- A saved receiver binding conflicts with a custom hook and blocks the start (reason code `overview-hook-conflict`); the binding stays untouched. To keep the binding, start the overview without `--on-handoff`; to switch to a hook, unbind first per [receiver binding and handoff](receiver-binding.en.md).
- Without a custom hook, the start loads a saved binding under the normal rules: the start only reads the binding—it never creates, rewrites or removes one—and does not suspend a waiter on the agent's behalf.
- The start result and review details show the receiver and binding state. A wait-type binding shows its harness, session and waiting state, plus a copyable `notes.py wait <short-id>`—run it from the bound session, whose arguments or environment carry the identity. A desktop event binding shows its delivery state and session and omits the claim-style wait command. A custom hook shows "configured".
- When an already-running review service is reused, the result shows what that process actually loaded; if this start's hook was not applied, it says so explicitly—no need to replace the running process.
- Desktop backfill and dedup on start, and the trigger timing of an inherited hook, follow the existing rules: batches pending before the start stay queued for `notes.py wait` or `notes.py claim` and are not replayed through the hook.

## Stopping a review and cancellation boundaries

`python3 $S/serve.py stop REVIEW_ID --timeout N` returns persistent JSON; `stop REVIEW_ID --status` only queries it. Exit 0 means the service, waiters and this review's background work have all confirmed stopping; 1 means incomplete; 2 means an invalid or ambiguous target. Retries keep the same `stopId` and handle only unfinished components. Only an explicit `serve.py REVIEW_ID` start accepts work again; stopping never archives, unbinds, finalizes or releases the port.

Before replacing or removing the binding, confirm cancellation of delivered desktop turns that are still awaiting claim. An explicit start redelivers work that is still awaiting claim and whose cancellation was confirmed, including when it reuses a running service.

A `notes.py wait` stopped by this lifecycle exits 4 and removes its marker; never re-suspend it as though it had timed out. Event cancellation is routed by review id, receiver binding and turn id. Desktop acceptance, owner discovery and signal delivery are not turn-stop confirmations. Rejection, timeout or a missing terminal turn state keeps `incomplete` with a capability gap; never end the shared session or kill a process whose ownership is unverified. Synthetic IPC covers the cancellation protocol; actual desktop-host cancellation has not been verified automatically.

See [workflow section 4](workflow.en.md#4-start-the-service-and-wait) for hook inputs, the working directory, and snapshot handling. Follow [section 6](workflow.en.md#6-initial-edit-review-and-delegation) when passing the accepted requirements, sequence, and skills to workers. Keep its main-session completion duties and user-controlled stage boundaries.
