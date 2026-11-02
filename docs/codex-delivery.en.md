# Codex desktop delivery acceptance

Validated on 2026-11-02 with macOS 25.6 arm64 and Codex desktop 26.1027.51404, using fictional material from `fixtures/sample/`. The review, ledger, annotations, and browser were isolated. Raw receipt evidence stays in controlled local storage; chat identities, personal paths, and business material are not committed.

## Actual idle state and normal restart

For each phase, a synthetic annotation was prepared. A user-approved, one-shot Playwright acceptance script waited for the chat's `task_complete` event, allowed a 3-second quiet interval, checked that no new turn had started, and actually clicked the page's “Hand to the agent” button. This bounded acceptance script is not a product scheduler. Both submissions automatically started distinct new chat turns, which ran `ack`, claimed the exact batch, resolved its annotation, and ran `done`. No user reminder was sent.

Times are UTC+8:

| Phase | Previous turn completed | Browser click | New turn started | Result |
|---|---|---|---|---|
| First idle submission | 10:31:43.019 | 10:31:46.223 | 10:31:46.530 | Batch 1 received and completed |
| Idle submission after normal restart | 10:33:59.020 | 10:34:02.136 | 10:34:02.282 | Batch 2 received and completed |

Between phases, only the synthetic review service was stopped, then started with `serve.py REVIEW --no-open`. Its process identity changed, the binding hash stayed the same, its dedicated port returned, and `hook=true`. No `--on-handoff` argument or new binding was supplied. The SHA-256 of the delivery implementation, `scripts/wakeup.py`, was `f4ca270ea2dd7dedec0c818c98bb31afb412929d218f305da116767ef2c027c1`. Later additions to unknown-receipt diagnostics did not change that delivery implementation.

## Regression coverage and state boundaries

- Synthetic automated tests cover HTTP submission, a real hook subprocess, the local socket protocol, normal CLI restart, recovery of unsent submissions, duplicate suppression, retry after explicit rejection, no replay of uncertain sends, identity validation, and conflicts with claiming waiters.
- `accepted` means the desktop accepted delivery; `received` means the bound chat executed its acknowledgement. Neither means annotation work is complete; batch history records completion.
- A claimed batch without a receipt displays “Chat receipt unconfirmed.” The diagnostic field `receiptConfirmed=null` means unknown. It does not imply an offline agent and never triggers takeover, rollback, or another claim.
- Busy receipt was tested separately at 10:45:31 through the same HTTP submission endpoint. The chat received batch 3 while its original work turn remained active; the receipt carried the same turn identity as before submission. It then claimed, handled, and completed the batch. This establishes receipt in the active turn, not every host queue policy.

## Adapter limits

The adapter uses existing local desktop IPC. It does not modify Codex global configuration, application files, permissions, or models; launch another CLI agent; or create a scheduler or startup service. This IPC is not a stable public API. Versions beyond the tested desktop require fresh verification; indefinite cross-version compatibility is not promised.

If the desktop exits, the chat has no owner, or the protocol is incompatible, annotations remain saved and delivery fails visibly. A lost response or interruption after sending leaves `unconfirmed`; inspect the chat and batch before resending. Moved or copied reviews require a fresh identity check. Service restart recovers submissions still awaiting receipt and does not take over another agent's claimed work. See [Agent mechanisms](../skill/annotation-everything/references/agent-mechanisms.en.md#codex-desktop-review-scoped-event-delivery) for setup.
