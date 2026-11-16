# Receiver binding and transfer

[简体中文](receiver-binding.zh-CN.md)

Read this page before configuring a receiver or changing its harness or session. “Connected to local service” means annotations can be saved. A separate indicator shows the receiver harness, a short session ID and its state. Hover to check the full ID; the submission dialog repeats the destination. `--by` is a processing signature, not a session identity.

## Configure the receiving session

Run `wakeup.py status REVIEW_DIR` first (`python3 "$S/wakeup.py"` on POSIX, `python "$S\wakeup.py"` in Windows PowerShell). Check that any existing binding belongs to this session; never copy another session's saved ID and use it as your own.

- **Event delivery on verified platforms**: only when the host supports a verified local event IPC (macOS Codex desktop; unavailable in Windows Phase 1, where `bind` returns `desktop-ipc-unavailable`), follow [Agent mechanisms](agent-mechanisms.en.md#event-delivery-flow-verified-platforms-only). Run `wakeup.py bind` using the current `CODEX_THREAD_ID` or the confirmed current chat ID. Keep event delivery and actual chat acknowledgement; do not start a claiming `wait`.
- **Waiting or active claiming**: use the current harness's stable session ID. If the host provides none, generate one random ID for this session and keep it for subsequent operations; use a new ID in a new session. A service PID, `--by` signature or temporary waiter process is not a session identity.

```bash
# POSIX (bash / zsh)
python3 "$S/wakeup.py" bind REVIEW_DIR --adapter wait --harness "<HARNESS>" --session CURRENT_SESSION_ID
python3 "$S/serve.py" REVIEW_DIR
python3 "$S/notes.py" wait REVIEW_DIR --events analysis --harness "<HARNESS>" --session CURRENT_SESSION_ID
```

```powershell
# Windows PowerShell
python "$S\wakeup.py" bind REVIEW_DIR --adapter wait --harness "<HARNESS>" --session CURRENT_SESSION_ID
python "$S\serve.py" REVIEW_DIR
python "$S\notes.py" wait REVIEW_DIR --events analysis --harness "<HARNESS>" --session CURRENT_SESSION_ID
```

If the host reclaims background processes across turns/sandboxes or lacks completion notifications, ask the user to run `serve.py` in a persistent terminal and explain clearly when they must keep the session open or return to chat to trigger `notes.py claim` or a bounded `wait --timeout 30`; never promise automatic wake-up of an arbitrary idle or closed chat.

`claim`, `resolve`, `reopen`, `review` and `done` also accept `--harness` / `--session`. The current process's `AE_HARNESS` / `AE_SESSION_ID` can supply them instead; explicit arguments take precedence. When neither is provided, only a known `CODEX_THREAD_ID` identifies a Codex session. Missing or mismatched identity returns exit code 2 without claiming or updating the batch. The receiving session coordinates permitted workers under the existing protocol. A separate session can take over review after drafting finishes and an explicit transfer is made.

After creating or changing a binding, restart this review service, refresh the page and check the harness and session. Submission is refused if the service needs a restart or the dialog's binding is stale. Annotations remain available; check the receiver and submit again. The page never silently forwards to a new session. Unbound legacy reviews retain saving, exports and legacy CLI compatibility, with an explicit “No receiver bound” indicator. Session claim protection applies once a receiver is bound.

## States and transfer

- **Waiting**: a live claiming waiter belongs to the bound session. Read-only `--no-claim` does not indicate a receiver. Stopping the waiter changes the state to “Not waiting” and retains the binding.
- **Processing**: the review has a claimed drafting or reviewing batch. This is batch state and does not prove the chat is still online.
- **Event receiver bound**: Codex desktop event delivery is configured. Connecting, sending, desktop acceptance and actual chat receipt remain separate; lack of a waiter does not mean offline.
- **Unknown receiver / binding error / service disconnected**: the current receiver cannot be confirmed. Upgrade, check the binding or restore the service as indicated; do not guess another harness.

After the user explicitly chooses a new receiver, add `--replace` to its `bind` command. To switch to Codex desktop, use its event binding procedure for the target chat. Use `wakeup.py unbind REVIEW_DIR` to unbind. Finish and `done` any claimed drafting or reviewing batches first; transfer and unbinding are refused while they are active. Unclaimed batches remain available. Old waiters exit after detecting the binding change and never take it back automatically. Finally restart this review service and refresh the page.
