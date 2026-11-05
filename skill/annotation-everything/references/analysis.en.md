# Handle analysis rounds

Follow [Shared safety and protocol](protocol.md) first. Analysis uses the review's receiving-session binding and a separate queue. The reviewer starts an analysis; the agent opens the topic and asks follow-up questions. Analysis does not revise the report directly.

## Receive and read

When the host supplies completion notifications, use `python3 "$S/notes.py" wait "$R" --events analysis --locale en`. Without the explicit option, a waiter receives revision batches only. Both queues follow submission order, with one item in progress at a time. An exact claim returns 3 when an earlier item is at the head; finish that item first instead of bypassing it.

On a desktop event, run the supplied `wakeup.py ack ...` command first, then its matching claim command. Analysis uses a review-wide round number; revision uses a batch sequence. Keep them distinct:

```bash
python3 "$S/notes.py" claim "$R" --events analysis --round N --by Agent
python3 "$S/notes.py" analysis "$R" --round N --json
```

Keep `baseRev` from the claim result. The complete context contains the focus question, decisions on the current path, this round's items, the reviewer's original answers and a copy of the source annotation as it was when analysis started. Read these records before continuing; a chat summary does not replace the reviewer's words.

When `wait --events analysis` returns a round, it has already claimed it; read its context without claiming again. If an exact claim exits 3, use `claim "$R" --events analysis` to accept the actual queue head and route by the returned `event`. Handle the target round only after a successful claim.

## Write back

Save question proposals as a JSON list. Each proposal needs a unique `key`, full `text`, a natural `short` name, `options` with stable `id` and `label` values, and a `recommend` that identifies one option. Use `parent: {node, option}` when attaching it to an existing question. A proposal enters the question tree only after the reviewer confirms it in a round. The system derives decisions from answers; the agent does not write decisions.

```bash
python3 "$S/notes.py" analysis-writeback "$R" --round N --base-rev REV --proposals questions.json --note "Analysis of this round"
```

Use `--proposals -` to read JSON from stdin. Pass an empty list when there are no new questions; the system carries unanswered valid questions forward. Keep complete answers and explanations rather than replacing them with chat summaries. Write back only a claimed round and preserve the baseline check. If rejected, read the current context and report the reason.

An analysis round does not run annotation `resolve` / `done`, edit the upstream report or speaker notes, or rebuild the review page. Verification defaults to the review's original source material; outside research requires the reviewer's explicit permission on that item. The reviewer turns a decision into a draft and confirms it in the page. Only confirmed annotations enter the normal “Hand to the agent for revision” flow; conversion does not submit a revision batch.

With a saved desktop binding, leave the service running and finish the current turn, awaiting the next event. With a verified host completion-notification mechanism, start another waiter with `--events analysis`. A running process or successful claim does not prove chat receipt.
