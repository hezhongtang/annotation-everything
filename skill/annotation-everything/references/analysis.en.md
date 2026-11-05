# Handle analysis rounds

Follow [Shared safety and protocol](protocol.md) first. Analysis uses the review's receiving-session binding and a separate queue. The reviewer starts an analysis; the agent opens the topic and asks follow-up questions. Analysis does not revise the report directly.

Here, `$S` is this skill's `scripts` directory and `$R` is the current review directory. The reviewer can start from an annotation, a page, or a blank focus question. An analysis without a source annotation does not create a placeholder annotation. To suggest analysis of an open annotation, use `notes.py suggest-analysis "$R" NOTE_ID --reason "Reason"`; the reviewer decides whether to start it.

## Receive and read

When the host supplies completion notifications, use `python3 "$S/notes.py" wait "$R" --events analysis --locale en`. Without the explicit option, a waiter receives revision batches only. Both queues follow submission order, with one item in progress at a time. An exact claim returns 3 when an earlier item is at the head; finish that item first instead of bypassing it.

On a desktop event, run the supplied `wakeup.py ack ...` command first, then its matching claim command. Analysis uses a review-wide round number; revision uses a batch sequence. Keep them distinct:

```bash
python3 "$S/notes.py" claim "$R" --events analysis --round N --by Agent
python3 "$S/notes.py" analysis "$R" --round N --json
```

Keep `baseRev` from the claim result. The complete context contains the focus question, decisions on the current path, this round's items, the reviewer's original answers, related assumptions with their confirmation status on this line, verified facts, parked questions, and the original source annotation when one exists. Read these records before continuing; a chat summary does not replace the reviewer's words.

When `wait --events analysis` returns a round, it has already claimed it; read its context without claiming again. If an exact claim exits 3, use `claim "$R" --events analysis` to accept the actual queue head and route by the returned `event`. Handle the target round only after a successful claim.

## Write back

Save question proposals as a JSON list. Each proposal needs a unique `key`, full `text`, a natural `short` name, `options` with stable `id` and `label` values, and a `recommend` that identifies one option. Use `parent: {node, option}` when attaching it to an existing question. A proposal enters the question tree only after the reviewer confirms it in a round. The system derives decisions from answers; the agent does not write decisions.

The list format remains supported. To include assumptions, verified facts or annotation updates, use an object: `{proposals: [], assumptions: [], facts: [], updates: []}`. An assumption has `{key, text, decisions: [decision IDs]}`; a fact has `{key, text, source?, node?}`. Reuse `key` for the same record without changing its original text. Preserve the full source for each fact. The reviewer confirms or rejects a proposed assumption in the next round; rejection does not refute it.

A structure proposal defaults to `op: "add"`. Splitting uses `op: "split"`, one `targets` entry, and at least two `replacements`. Merging uses `op: "merge"`, at least two targets, and one replacement. Voiding uses `op: "void"` and targets. Replacements use the new-question fields. Targets must be on the current selected branch and cannot occur twice in the same round; a replacement cannot attach to a branch being voided. Structure changes take effect only after reviewer confirmation. Do not repeat rejected proposals. Historical questions and decisions remain available.

```bash
python3 "$S/notes.py" analysis-writeback "$R" --round N --base-rev REV --proposals questions.json --note "Analysis of this round"
```

Use `--proposals -` to read JSON from stdin. Pass an empty list when there are no new questions; the system carries unanswered valid questions forward. Keep complete answers and explanations rather than replacing them with chat summaries. Write back only a claimed round and preserve the baseline check. If rejected, read the current context and report the reason.

When the context contains `updateRequests`, draft `{noteId, text, node?}` for each requested annotation from current conclusions and put the drafts in `updates`. The next round asks the reviewer to confirm. Ordinary annotation text changes only on confirmation, with the original text preserved and the basis updated. Do not replace this confirmation with an annotation-edit command.

The reviewer can keep answering while you hold a round; read the saved original answers from context. A baseline mismatch rejects the result, voids the old round and automatically requests new questions with still-valid answers prefilled. Do not force an overwrite, resubmit the old round or bypass the queue head. Changing a prior answer preserves detached decisions; the system decides whether each can be reused when restored.

Answers can accept the recommendation, choose an option, supply free text, park a question, or request verification. Read free text verbatim; parked questions leave the frontier. A verification request defaults to `allowExternal: false`; only the explicit Boolean `true` permits outside sources. Otherwise use the review's original material. Return verification results in `facts` for the reviewer to consider. A review can contain multiple analyses; submitted rounds of paused analyses still need processing. Switching the active analysis does not invalidate a claimed baseline.

An analysis round does not run annotation `resolve` / `done`, edit the upstream report or speaker notes, or rebuild the review page. Verification defaults to the review's original source material; outside research requires the reviewer's explicit permission on that item. The reviewer turns a decision into a draft and confirms it in the page. Only confirmed annotations enter the normal “Hand to the agent for revision” flow; conversion does not submit a revision batch.

When a revision batch requests a full check, read all analyses' effective current-line conclusions with `python3 "$S/notes.py" analysis-check "$R" --json`. This is read-only and works without an open round. Compare the report and speaker notes, then list four categories for confirmation: changed-conclusion annotations, resolved annotations with invalid bases, report contradictions with current conclusions, and contradictions between analyses. This analysis-related check only lists items for review; do not replace annotation text or revert resolved statuses yourself.

With a saved desktop binding, leave the service running and finish the current turn, awaiting the next event. With a verified host completion-notification mechanism, start another waiter with `--events analysis`. A running process or successful claim does not prove chat receipt.
