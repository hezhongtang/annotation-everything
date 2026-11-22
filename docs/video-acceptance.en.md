# Review a source change on a video cut

[中文](video-acceptance.zh-CN.md)

Use two separate local projects for this walkthrough. Each review has one engine, one composition, and one bound receiving session. The synthetic source generator and dependency versions are in the [fixture guide](../fixtures/video/README.md).

## Start a review

Set `S` to the installed skill's `scripts` directory, `R` to a new local review directory and `P` to the project root. Use the same isolated `AE_HOME` for the reviewer and receiver of each demo.

| Starting material | Action | What to check |
| --- | --- | --- |
| Source project | Run `python3 "$S/video_engines.py" inspect "$P"`, then `python3 "$S/build_viewer.py" --report "$P" --video-composition ENTRY --out "$R" --locale en`. | Choose the intended composition if several are listed. A missing cut is rendered. Inspect the candidate evidence before explicitly confirming reuse with `--reuse-movie PATH`. |
| MP4 | Run `python3 "$S/build_viewer.py" --report cut.mp4 --out "$R" --locale en`. | Add annotations before linking the project. Use **Link source project**, enter the engine, root and entry, then confirm any differences. The original annotations still open V1. |

Start the local page with `python3 "$S/serve.py" "$R"`. In the receiving session, bind the review using its supported host adapter. For the wait adapter, use `python3 "$S/wakeup.py" bind "$R" --adapter wait --harness "$AE_HARNESS" --session "$AE_SESSION_ID"`. Set those two variables to the actual receiving session's harness and stable identity before binding. The same identity must claim the work and submit the receipts.

## Revise and compare

1. Add five annotations. At 30 fps, drag a region around the subtitle on frame 96. Mark frames 120 through 179 inclusive for the music, which records `[120,180)`, or 4–6 seconds. Add a question about the intro and two lower-priority notes.
2. In the handoff dialog, leave only the subtitle, music and question checked. Add a batch message. Use `python3 "$S/notes.py" claim "$R"` in the bound session. Check the three original IDs, locations, original media and batch message.
3. Edit the project, add 12 intro frames, move the subtitle and reduce the cue volume. Render V2 with the same engine. Write a JSON receipt with a `notes` array. For the subtitle, use its original `noteId`, `"outcome":"fixed"`, a change summary and `"target":{"kind":"frame","frame":108}`. Use `{"kind":"range","startFrame":132,"endFrame":192}` for the music. Use `"outcome":"question"` and `question` for the third note.
4. Run `python3 "$S/notes.py" video-receipt "$R" --review-id "$REVIEW_ID" --seq 1 --receipt-id demo-v2 --movie V2.mp4 --receipt v2.json`. Both fixes should await review, the question should await a reply, and the other two notes should remain unsubmitted. If rendering fails, send `--render-error REASON` with its own receipt ID and retry with a fresh ID after recovery. Previously registered cuts remain available.
5. Open the original cards. Compare V1 frame 96 with V2 frame 108 and play the two music ranges separately. Approve the subtitle, return the music with a reason and answer the intro question. Check the batch history before submitting again. Select only the returned music, render V3 with a further gain reduction, and send a new receipt for that original ID and the new batch number. Read V1, the V2 return and the V3 fix from that card.
6. For a removed shot, report `deleted` with the reason and a `cut` target including context frames on either side. If the location is uncertain, report `needs_relocate` with a reason. The reviewer can correct the target on the page. Bulk approval applies to the selected changes that still have complete evidence at confirmation.
