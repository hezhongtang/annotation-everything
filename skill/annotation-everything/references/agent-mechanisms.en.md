# Agent mechanisms

Before configuring a receiver or switching sessions, read [Receiver binding and transfer](receiver-binding.en.md). Confirm the receiving session, then choose event delivery, background waiting, or polling based on the host's actual capabilities. `--by` is an attribution label and does not establish session identity.

[简体中文](agent-mechanisms.zh-CN.md)

Commands, files, and exit codes are the same for every agent. **Do not rely on a hardcoded brand whitelist, and do not treat loading this skill or a zero start exit code as proof of receiving or wake-up capability.** Inspect the five capability areas below before configuring a waiter, invoking annotation skills, delegating work, or choosing a worker name. The [shared protocol](protocol.md) governs permissions, stage boundaries, and safety; follow the [English workflow](workflow.en.md) for the complete sequence.

## Capability probe before choosing a mechanism

| Capability | What to check | What to do |
|---|---|---|
| **Shell and CLI syntax** | Is the current shell POSIX `bash`/`zsh` or Windows PowerShell? | Use `python3 "$S/..."` and POSIX quoting on POSIX; use `python "$S\..."` and double/PowerShell quoting on Windows PowerShell. Quote paths with spaces or non-ASCII characters so backslashes are preserved and quotes match the shell. |
| **Service and waiter process survival** | Do background `serve.py` / `notes.py wait` processes survive after the current turn ends or a sandbox exits? | If the host keeps background processes alive, start them in the bound session and verify the port/status file; if the host reclaims background processes when a turn or sandbox ends, **do not judge success from exit code 0 alone** — run `serve.py` in a persistent user terminal and state clearly how to keep it running or restart it. |
| **Waiting, notifications, and event delivery** | Does the host provide (1) verified review-scoped event IPC, (2) background task completion notifications, or (3) external event hooks? | Pick the strongest verified capability:<br>1. **Verified desktop event IPC** (only on verified platforms; unavailable in Windows Phase 1, where `DesktopIPC` returns `desktop-ipc-unavailable`): use the event binding flow below.<br>2. **Persistent background completion notifications**: run `notes.py wait REVIEW_DIR --events analysis --harness ... --session ...` in the background, wake on notification, and check its exit code and output.<br>3. **External event trigger without background wait**: configure a trusted `serve.py REVIEW_DIR --on-handoff "command arguments"` at startup (runs without a shell; Windows waits synchronously on the child and preserves its exit code).<br>4. **No notifications or events**: poll with bounded `notes.py wait REVIEW_DIR --events analysis --timeout 30` (exit 3 on timeout) or `notes.py claim REVIEW_DIR --events analysis` (JSON and exit 0 when work is waiting; exit 3 when none). **Tell the user explicitly when they must keep the session/terminal running or return to chat after submitting** — never promise automatic wake-up of an arbitrary idle or closed chat, and never fake wake-up via unauthorized config edits, scheduled tasks, or substitute sessions. |
| **Cancellation and stop handling** | Can stop or user interruption settle cleanly? | `notes.py wait` cleans `.annotation-wait.<pid>.json` and exits `130` on `SIGTERM` / `SIGHUP` / Windows `SIGBREAK` / `SIGINT`, and exits `4` when the review is stopped (never re-suspend as a timeout). Never kill a shared session of unverified ownership. |
| **Skills, delegation, Q&A, and `--by`** | Which tools does this host expose? | **Skill invocation**: use the host's skill-loading tool for named and standing skills, or read the matching `SKILL.md` directly; if missing, report that and continue under the shared protocol.<br>**Delegation**: delegate only when the batch allows `allowSubagents` and the host supports subagents or separate sessions (selecting models by stage if supported); otherwise handle personally.<br>**Q&A tool**: use the host's interactive Q&A tool for pre-draft alignment when available; otherwise state the specific reason first, ask in plain text, and record `request.qa.unavailableReason`.<br>**Worker name (`--by`)**: name the role explicitly (for example `--by initial-editor` or `--by reviewer`); without `--by`, `notes.py` defaults to `claude` only when `CLAUDECODE` is nonempty, otherwise `agent`. |

## Distinct status semantics (UI, CLI, and delivery)

Service health, delivery acceptance, actual chat receipt, and processing completion are separate states:

- **Service online (`server`)**: means only that the `127.0.0.1` local service can save and read annotations; it does not prove the receiving chat is online or can be woken.
- **Delivery accepted (`accepted`)**: means only that the transport accepted the request; **never show or claim "received" without an acknowledgement receipt**.
- **Actual chat receipt (`received` / `ack`)**: established only after the bound chat executes `wakeup.py ack ...` or claims the work (`notes.py claim`).
- **Processing completed (`done` / `resolved`)**: annotations and batches are finalized on disk via `notes.py`.
- **Unknown host or protocol**: keep status as unconfirmed/unknown; never pretend offline or success.

## Event delivery flow (verified platforms only)

When the user requests automatic handling after web submission and the current host provides a verified review-scoped event IPC, follow these steps without editing host global configuration. **Windows Phase 1 does not provide or verify Codex desktop private IPC** (`bind` fails with `desktop-ipc-unavailable`); use `--adapter wait` with `notes.py wait` / `claim` or `--on-handoff` instead.

1. Read `wakeup.py status REVIEW_DIR`. Preserve an existing binding instead of silently switching its receiving chat.
2. Run `wakeup.py bind REVIEW_DIR` (on POSIX, using `CODEX_THREAD_ID` or `--thread UUID`). The command checks the local desktop socket and thread owner, then saves private metadata beside the review. `--by` is only an attribution label.
3. Start `serve.py REVIEW_DIR` normally. If an older service exists, verify its identity and avoid interrupting another person's work before stopping and restarting it. Every subsequent normal restart restores the binding. A custom `--on-handoff` conflicts with a saved binding and is rejected; changing delivery method requires an explicit decision, `wakeup.py unbind REVIEW_DIR`, and restart.
4. On receipt, run the supplied `wakeup.py ack ...` command, then the supplied claim command: `--seq N` for revision/review, `--events analysis --round N` for analysis. Follow the [analysis guide](analysis.en.md) for a round and the accepted completion protocol for a revision batch. If an exact claim exits 3, handle the actual queue head first. Do not start a claiming waiter or scheduler in binding mode. The updated CLI prevents a different chat from claiming the bound review.
5. Verify an actually idle chat: after the previous turn completes, submit through a synthetic review page and confirm a new chat turn, acknowledgement, annotation resolution, and done. Restart the same service normally and repeat. Busy-chat delivery, owner discovery, and process exit 0 are not idle-wakeup proof.

`wakeup.py status` and the page distinguish `accepted` (desktop accepted delivery) from `received` (the chat executed acknowledgement). Failed deliveries can retry on service restart or another user submission. Interrupted sending or a lost transport acknowledgement stays `unconfirmed`; inspect chat and batch state before any replay. Startup recovery handles pending revision/review batches and queued analysis rounds, never work already claimed.

This adapter uses the desktop's existing local IPC, without changing host configuration, permissions, models, or application files or starting a second CLI agent. IPC is not a stable public API; the only verified environment is macOS desktop 26.1027.51404 (Windows desktop remains unverified pending #177). An unavailable desktop, missing thread owner, or incompatible protocol produces a visible error and preserves annotations. Binding applies only to its original review identity and directory; verify and rebind moved/copied reviews. Unbinding preserves annotations and delivery history.

## Overview-home delegated starts

`serve.py overview` (optionally `--under DIR`) opens the standalone overview home; choosing "Start and open" for a stopped review delegates the start to it. Before opening the home for the first time, run the capability probe in [Start the overview home](workflow.en.md#start-the-overview-home). Agent-relevant behavior:

- The handoff hook comes only from the overview's command line, `serve.py overview --on-handoff "command arguments"`—never from a web page. The newly started review service inherits the parsed arguments and runs the hook in the overview's starting directory (no shell; relative paths in the arguments resolve there).
- A saved receiver binding conflicts with a custom hook and blocks the start (reason code `overview-hook-conflict`); the binding stays untouched. To keep the binding, start the overview without `--on-handoff`; to switch to a hook, unbind first per [receiver binding and handoff](receiver-binding.en.md).
- Without a custom hook, the start loads a saved binding under the normal rules: the start only reads the binding—it never creates, rewrites or removes one—and does not suspend a waiter on the agent's behalf.
- The start result and review details show the receiver and binding state. A wait-type binding shows its harness, session and waiting state, plus a copyable `notes.py wait <short-id>`—run it from the bound session, whose arguments or environment carry the identity. A desktop event binding shows its delivery state and session and omits the claim-style wait command. A custom hook shows "configured".
- When an already-running review service is reused, the result shows what that process actually loaded; if this start's hook was not applied, it says so explicitly—no need to replace the running process.
- Desktop backfill and dedup on start, and the trigger timing of an inherited hook, follow the existing rules: batches pending before the start stay queued for `notes.py wait` or `notes.py claim` and are not replayed through the hook.

## Stopping a review and cancellation boundaries

`serve.py stop REVIEW_ID --timeout N` returns persistent JSON; `stop REVIEW_ID --status` only queries it. Exit 0 means the service, waiters and this review's background work have all confirmed stopping; 1 means incomplete; 2 means an invalid or ambiguous target. Retries keep the same `stopId` and handle only unfinished components. Only an explicit `serve.py REVIEW_ID` start accepts work again; stopping never archives, unbinds, finalizes or releases the port.

Before replacing or removing the binding, confirm cancellation of delivered desktop turns that are still awaiting claim. An explicit start redelivers work that is still awaiting claim and whose cancellation was confirmed, including when it reuses a running service.

A `notes.py wait` stopped by this lifecycle exits 4 and removes its marker; never re-suspend it as though it had timed out. Event cancellation is routed by review id, receiver binding and turn id. Desktop acceptance, owner discovery and signal delivery are not turn-stop confirmations. Rejection, timeout or a missing terminal turn state keeps `incomplete` with a capability gap; never end the shared session or kill a process whose ownership is unverified. Synthetic IPC covers the cancellation protocol; actual desktop-host cancellation has not been verified automatically.

See [workflow section 4](workflow.en.md#4-start-the-service-and-wait) for hook inputs, the working directory, and snapshot handling. Follow [section 6](workflow.en.md#6-initial-edit-review-and-delegation) when passing the accepted requirements, sequence, and skills to workers. Keep its main-session completion duties and user-controlled stage boundaries.
