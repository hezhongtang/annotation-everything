# English workflow

Read [Shared safety and protocol](protocol.md) before acting. `$S` is the `scripts/` directory in the loaded skill copy. `$R` is the chosen review directory. Use absolute paths when invoking commands from another directory.

Pick the entry point by request: **generate a new report from raw material** (the user supplies material and a request; no report exists yet) — read [Report generation: default paradigm](generation.en.md) first, then continue from section 2 below once the draft and its verification are done; **review an existing report** starts at section 1; **apply annotations** starts at section 3; for an **explicitly requested full verification** run `report_check.py` on the complete artifact set (see step 5 of the generation guide), and never treat annotation-processing status as the report's quality verdict. The generation entry uses the built-in paradigm by default; when the user names an external generation skill, see "Generation method: built-in paradigm and external generation skills" in the generation guide for the selection and integration rules.

## 1. Find the review and select a language

```bash
python3 "$S/serve.py" list --locale en
python3 "$S/serve.py" list --under /path/to/project --locale en
python3 "$S/serve.py" overview --locale en   # overview home: open the overview without binding a review
python3 "$S/serve.py" overview --under /path/to/project --locale en  # list and operate reviews under that directory only
```

If the user asks to apply annotations without naming a review, match the review name and directory before editing. Each review has its own `reviewId` and dedicated port. Most `serve.py` and `notes.py` commands accept a unique ID prefix of at least four characters instead of a directory; `build_viewer.py` needs a directory. A missing or mismatched ledger entry keeps its port until the user confirms it can be forgotten.

Choose `--locale en`, `zh-CN`, or `auto` explicitly when requested. The page's saved browser preference overrides the review default. `auto` matches browser languages in preference order and falls back to English. A new review with no locale option stays Chinese; rebuilding retains its saved setting. The display language does not choose the material language.

CLI commands accept `--locale` and `AE_LOCALE`. An explicit option wins, then the environment, then the review setting for review-specific commands. Unconfigured calls remain Chinese. Only `auto` follows the system language. The repository's `ae.py` has no review default. Use `notes.py export --lang en` to select the export wrapper language. Commands and raw JSON values stay unchanged. Inspect `--help` for the command's arguments.

### Use the review overview in the browser

Start a local review service first. If none is running, use `python3 "$S/serve.py" <review-id>`, or open the overview home directly with `python3 "$S/serve.py" overview` — no review has to be running. Then choose "Review overview" on its review page, or open `/#overview` at that review's address. The overview can also stay open in a separate tab.

- The default scope is "This workspace"; choose "All on this machine" to see other reviews in the same ledger. The workspace is the current review's git repository root, or the review directory's parent when it is outside a git repository. Paths are relative to the workspace; paths in other workspaces start with `~` or show only the review name.
- Reviews are grouped as Running, Stopped, Archived and Invalid, with the latest activity first within each group. Select a review to see its summary, purpose, audience, core questions and pending receipt/review counts. A missing summary shows "(No summary)" with the cover title. Material text retains its original language.
- Choose "Open" for a running review or "Start and open" for a stopped review. Both open the target review page in a new tab. Starting uses the target's owned port; if the port is occupied or startup fails, follow the error guidance before retrying. The start result and details show the target's receiver and binding state: with no receiver configured it warns immediately and offers a copyable `notes.py wait <short-id>` (run it from the bound session). When starting from the overview home (`serve.py overview`), the `--on-handoff` hook on the overview's command line is inherited by the newly started service; conflict and recovery rules are in [the matching mechanisms per agent](agent-mechanisms.en.md). Check the rest of the receiver setup under section 4. "Back to current review" returns to the page hosting the overview.
- Choose "Stop review" in a running review's details. Enable "Show archived" to stop an archived review that is still running. The overview displays "Stopping…", "Stopped" or "Stop incomplete". Stopping retains materials, annotations, submission and batch state, analysis history, receiver binding and the owned port. If you stop the review hosting the overview, reopen the overview from a running review to refresh it.
- The overview asks registered target pages to save queued changes. If saves remain unconfirmed, open the target at the same address in the original browser, recover any queued or failed changes, and choose "Retry stop". If the service lacks stop support, update the skill and service. If the target page lacks save confirmation, rebuild and reload it before retrying.
- To stop all running reviews in a scope, select "This workspace" or "All on this machine", then choose the corresponding "Stop all running reviews" button. Check the scope and count in the confirmation, including archived reviews that are still running. Confirmation fixes the target list. Check the new count and start another batch for reviews started later.
- The batch result shows each target's progress and any reason it could not stop. Changing scope or interface language keeps the original scope visible. The same tab retains the latest batch when reloaded. Check the status of any interrupted request before retrying. Choose "Retry failures only" after resolving the reported issues. If the review hosting the overview has stopped, run the start command shown in the result and refresh the same tab. The confirmed result for that host stays in the batch.
- Archiving means the review is no longer being advanced and hides it by default. Annotations, submissions and the owned port remain, and the service keeps running. Choose "Expand" or "Show archived" to reveal archived reviews, then select one and choose "Unarchive". An archived current review shows a banner in both views and still accepts annotations and submissions. **Archiving does not cancel work on submitted annotations**; continue according to the batch requirements.
- Invalid entries cannot be opened. Their details provide `serve.py forget <short-id>`. Once the entry is confirmed unnecessary, remove it from the ledger using the CLI. The overview does not delete review directories. `serve.py list` and `list --json` still include all reviews and mark archived entries.

### Overview home: open the overview without a review

```bash
python3 "$S/serve.py" overview --locale en
python3 "$S/serve.py" overview --under /path/to/project --locale en
```

- Entry and host: one command opens the standalone overview home — **no review has to be running**. The old path, entering the overview from any running review page, stays as is. The home serves only the overview view under the same local security gates (loopback binding, Host and same-origin write checks); review-only APIs (annotations, analysis, handoff, stop lifecycle) are explicitly refused there with stable reason codes.
- Reserved address: the home's reserved port is recorded in the review ledger directory and shares the ledger write lock with review-owned ports. After quitting, a restart returns the same reserved address you can bookmark. The home avoids reviews' owned ports, and new review services avoid the home's reserved port when assigning, claiming or falling back — also after the home quits.
- Single instance and reuse: one home per ledger. The same start configuration (normalized `--under`, an explicit `--port`, `--locale`, hook arguments and the hook working directory) reuses the running instance and its address; a different configuration exits non-zero and asks you to quit the current home first. `--no-open` only affects whether this call opens the browser.
- Temporary fallback: when the reserved port is taken, or an older registered port conflicts, this run falls back temporarily with a warning; the reserved-port record stays, and `--port N` affects only this run's address.
- `--under` scope: the same directory-containment rule as `serve.py list --under`. Listing, starting, archiving and unarchiving, individual or batch stopping, and failure retries are all validated server-side against the same effective scope; the workspace scope (`scope=workspace`) is unavailable on the home (HTTP 400) — pass `--under` to limit the directory.
- Exit and maintenance: the home has no stop lifecycle. Press `Ctrl+C` in the terminal that started it. Quitting clears run state and keeps the reserved-port record; the page, log and status files live in the ledger directory. While the home runs it holds the copy's update lock, so `update.py` reports a running service and defers; quit the home before updating. `ae.py uninstall` recognizes a running home and refuses; `--stop-servers` verifies the process identity and stops it first (the same path as review servers). Review directories are never touched.
- Capability probe: the actually invoked skill copy may predate the `overview` subcommand. Run a command-style check before opening the home; it exits non-zero when the capability is missing, and the upgrade guidance swaps in a copy that has the feature (`python3 "$S/update.py" now` updates the current copy, or reinstall per the repository instructions):

  ```bash
  python3 "$S/serve.py" --help | grep -q overview || {
    echo "This skill copy has no overview subcommand: update the copy with update.py first, then retry" >&2
    exit 1
  }
  ```

  Receiver rules for delegated starts from the home (hook inheritance, binding conflicts, backlog claiming) are in [the matching mechanisms per agent](agent-mechanisms.en.md).

When generating a new report, write a one- or two-sentence `summary` in its verification record so the overview can identify it. This optional field requires no backfill for older records. See [step 4, "Write the verification record," in the generation guide](generation.en.md).

## 2. Generate or rebuild the page

```bash
python3 "$S/build_viewer.py" --report /path/to/report.html --script /path/to/script.md --out "$R" --name "Q3 review" --locale en
python3 "$S/build_viewer.py" --rebuild "$R"
```

Name the directory after the material, such as `q3-review/`. The report uses one `<section class="page" data-page="N" data-key="stable-key">` per page. Speaker notes use `### Page N｜Title` and `**Duration**`, `**Talking points**`, `**Transition**`, `**Likely questions**`. Chinese headers and mixed-language notes also work. See the [format reference](formats.en.md) before adapting unfamiliar material.

`--script` is optional. Page mismatches stop generation; use `--lenient` only after confirming missing note pages are intentional. Malformed headers and duplicate page numbers remain errors. Read warnings about fields, duration, unstable keys, and skipped resources before delivery. Local assets are embedded only within the allowed asset root. Use `--sections "Opening:1-4,Main line:5-6"` if inferred navigation is wrong; `--title` overrides the title.

For PPTX inputs, run `check_pptx_fonts.py input.pptx --locale en` before conversion and add `--pdf converted.pdf` afterwards. Exit codes are 0 for no detected difference, 1 for discrepancies or unknowns, and 2 for input or tool errors. Verify wrapping, spacing, and overlap on every converted page; matching font names do not prove correct glyph rendering.

`--report` also accepts a Word document (`.docx`) directly: the review page is generated with the pagination, page size, and orientation Word saved — no HTML conversion step. The generator prints the **pagination confidence** (verified / unverified / estimated) and records it in the page banner; estimated page numbers are not Word page numbers, and the page suggests saving the document once in Word or WPS and rebuilding. Structure and write-back limits, including the support matrix, are in the [format reference](formats.en.md) under "Word documents (DOCX)." **Read the pagination and limits warnings before delivery.**

`--report` also accepts a PowerPoint document (`.pptx`) directly: each slide becomes one review page that keeps the original size, orientation and order, rendered locally with LibreOffice and Poppler. A missing tool, a failed conversion or a page-count mismatch fails the build with a clear diagnostic — a text preview never stands in for visual review (tool installation is described in the [format reference](formats.en.md) under "PPTX"). Without an external script, speaker notes are placed page by page on the script side; an explicit `--script` file takes precedence and PPT notes are not merged into it. The generator records font evidence (declared, missing, substituted), rendering info, and "page-by-page visual inspection not run" in the manifest and the banner. **Read the rendering and font warnings before delivery; groups of read-only structures (group interiors, charts, SmartArt, media) can be selected and annotated but are explicitly marked as read-only elements.** Structure and write-back limits are in the [format reference](formats.en.md) under "PPTX".

Rebuilding retains identity and annotations and remaps pages by stable keys. A copied review gets a new identity. An annotation whose page disappeared is marked orphaned; confirm its destination. Only the report and notes source files enter batch snapshots; upstream renderer changes need their own source-control review.

## 3. Skills and standing requirements

Generation discovers local skills and plugin caches. `--skills-from /path` adds a source; `--no-scan-skills` disables scanning. Rebuilds retain these choices and manual edits. Plugin candidates use `plugin:skill` names; a cached plugin may not be enabled. Search uses original skill names and descriptions without translating them. Large catalogs require current tools throughout.

```bash
python3 "$S/notes.py" skills "$R" --set "writer=Editorial checks" "layout=Layout checks" --locale en
python3 "$S/notes.py" skills "$R" --always "writer=script" "layout=style" --locale en
```

Scopes accept `all`, `report`, `script`, `style` or their Chinese equivalents. The CLI maps aliases to the same stored values. `--always-add`, `--always-remove NAME`, and `--always-clear` maintain the list. In the page, search by name or description, use arrows and Enter to select, or enter an unlisted name. Separate names with commas. The standing dialog uses Enter for the next row and Ctrl/Cmd+Enter to save. Applicable standing and per-annotation skills must both be invoked under the shared protocol. Read [Agent mechanisms](agent-mechanisms.en.md) before selecting the host-specific invocation method.

## 4. Start the service and wait

First bind the current receiving session using [Receiver binding and transfer](receiver-binding.en.md). Supply the bound `--harness` / `--session` or their environment variables when waiting. Codex desktop event mode uses the Agent mechanisms procedure instead of a claiming waiter. Check service connection and receiver identity separately on the page.

For automatic handling in Codex desktop, first follow the [persistent binding procedure](agent-mechanisms.en.md#codex-desktop-review-scoped-event-delivery), then start the service normally. A Codex event binding restores delivery on restart and must not start the claiming waiter below.

```bash
python3 "$S/serve.py" "$R" --locale en
python3 "$S/notes.py" wait "$R" --events analysis --locale en
```

These are long-running commands. Read [Agent mechanisms](agent-mechanisms.en.md) and select a method that the host supports: background task notifications, bounded polling, or a submission hook. A background process wakes the agent only if the host supplies that notification. `wait --timeout SECONDS` exits 3 when no submission arrives before the timeout. `notes.py claim "$R"` accepts a waiting submission, prints JSON, and exits 0; no waiting batch exits 3. One waiter handles one review. Start the selected waiting mechanism before reporting that the review is ready.

Reuse a matching running service. If the dedicated port is occupied, read the temporary address from the service output; do not casually change port ownership. Stop the whole review with `serve.py stop REVIEW_ID`; query with `--status` and retry the same command for unfinished components. A waiter stopped by this lifecycle exits 4 rather than timing out; do not re-suspend it. Only an explicit service start accepts work again. An optional user-configured `--on-handoff "command arguments"` hook receives JSON on stdin and `AE_EVENT`, `AE_REVIEW_DIR`, `AE_HANDOFF_SEQ`, `AE_URL`. It runs from the service's starting directory without a shell. Its snapshot can be older than the accepted batch, so claim before editing. Repeated pending submissions merge rather than trigger another hook. When the overview home starts the review (`serve.py overview --on-handoff "command arguments"`), the hook comes only from the overview's command line, never from the web page; the newly started service inherits the parsed arguments and runs in the overview's starting directory (relative paths resolve there). A saved receiver binding conflicts with the hook and blocks the start; the binding stays untouched.

The page supports ordinary annotations, quotations, exact delete/replace edits, paired edits, point/region marks, re-annotation, style requests, and skill requirements. Reviewers choose finalization or a stop after the initial edit, subagent permission, and the full check. Processing history shows each batch and its results. Chrome and Edge provide the most reliable report text selection; other browsers can still annotate and mark regions.

In service mode, choose “Stop review” in the top bar. Press `Esc` to cancel confirmation. The page sends pending changes first. It then coordinates stopping this review’s waiters and active agent work before closing the local service. Materials, annotations, submissions and processing history, the receiver binding, the owned port and open tabs are kept. The page shows “Stopping review”, “Review stopped”, or “Review not fully stopped”. Other tabs at the same address receive confirmed status updates. Expand “Stop components” for details and choose “Retry stop” after addressing incomplete components. If the page loses its connection, use `serve.py stop REVIEW_ID --status` to check the persisted state. Run the displayed `serve.py REVIEW_ID` command to explicitly reopen the review.

When the page asks for a service update, send pending changes first and use the current `serve.py stop REVIEW_ID` command. If it returns `upgrade-required`, check that this review’s active work has ended in the original agent session, then press `Ctrl+C` in the terminal that started the old service. Rebuild the page, start the current service and refresh the browser.

The overview and command line use registered review pages to confirm saves for each service address and browser storage context. If a tab reloads or closes, reopen the same address in the original browser and retry. After an incomplete stop, a retry checks registered browser storage again for any changes queued since the last confirmation. Previously confirmed waiters and agent work keep their results. Before upgrading an older review page, send or export its queued changes. After rebuilding, reload each open older tab at its original address to enable save confirmation before stopping from the overview or command line.

After checking the drafts, the reviewer can use **Finalize all (N)** above the annotation list. One confirmation lists the eligible drafts, batches and skipped count. Only drafts in `drafted` or `review` batches are finalized; `claimed` and `reviewing` batches are skipped, including review claims made after confirmation opens. The result uses actual server counts and explains skips. Single-annotation finalization, per-batch finalization and requesting agent review remain available. File mode, disconnection and older services explain how to start, update or restart the service and refresh the page. Agents still stop at the requested stage and must leave this decision to the reviewer.

Without a service, open `index.html` directly. Annotations persist in browser storage, and the reviewer can copy or download them. File mode does not perform a live submission; use the import path in section 7.

## 5. Read the accepted batch and edit sources

**Locations of DOCX annotations**: `show` prints a `DOCX location` line under the annotation (paragraph number, paraId, character offsets in the paragraph, source). A line starting with ⚠ means the location cannot be used as is: when the paragraph text has changed, check the position against the quoted text; when the location was written against another version of the source file, rebuild the review page first; when its version is newer than this tool supports, it cannot be used for write-back. For an annotation marked orphaned after a rebuild, confirm with the user which paragraph it belongs to.

**DOCX write-back**: use `show` to check the accepted batch requirements, original text and paragraph locations, then follow this branch. Supported content and refusal reasons are in [Formats](formats.en.md), under “Word documents (DOCX)”.

```bash
python3 "$S/docx_edit.py" apply "$R" --batch N --dry-run --locale en
python3 "$S/docx_edit.py" apply "$R" --batch N --locale en
# To apply selected quick edits, replace --batch N with --id ID (repeatable).
# For a free-form annotation, check the zero-based paragraph number and original text.
# After a successful preview, remove --dry-run to execute the same replacement.
python3 "$S/docx_edit.py" replace "$R" --paragraph 12 --from "Original text" --to "Revised text" --batch N --dry-run --locale en
```

- Quick edits are checked as a whole: an ambiguous location, overlapping edit or read-only structure prevents the entire selection from being written. Use the per-annotation reasons to adjust the selection. Handle speaker-note edits through the existing workflow; free-form replacements must follow the user's intent.
- The default updates the source DOCX after saving a binary original (0600) in `.snapshots/batch-N/docx-writeback/`. ID-based edits and replacements without a batch use `batch-0`. `--output new.docx` keeps the source and requires a new output path; create a separate review from that output. `--dry-run` writes neither document nor backup.
- Resolve Word owner files, fingerprint mismatches, signatures and editing protection as instructed by the error. Track Changes is refused by default; use `--allow-untracked` only when the user explicitly allows untracked edits, and explain that AE writes untracked changes. Paragraphs containing native revisions still require Word. A table style with hidden-text properties, including conditional styles, makes the entire table read-only.
- After each successful write, rebuild the review page, check the revised text and `notes.py diff`, then record each result with `resolve` and finish with `done` according to the batch requirements. The command never resolves annotations automatically. Rebuild and recheck locations before another write; stale quotes or unconfirmed locations cannot be forced. Write-back retains the pagination markers and does not calculate Word's new layout; to verify actual pagination, open and save in Word/WPS, then rebuild again.

**Locations of PPT annotations**: in a PPT review `show` prints a `Slide location v1` line (whole page / element targets / text range / region, slide key, logical paragraph and code-point offsets, read-only limits, source). Check each one before acting: **read-only elements** (pictures, charts, SmartArt, group interiors, media) state that they cannot be written back automatically — hand those requests to the user for PowerPoint/WPS instead of trying `pptx_edit.py`; check text locations against the annotation's quoted text, and when the source file has changed (fingerprint mismatch) or the location is marked stale/orphaned, rebuild the review page and recheck instead of forcing the old offsets.

**PPTX write-back**: use `show` to check the accepted batch requirements, original text and slide locations, then follow this branch. Supported content, the whitelist contract and refusal reasons are in [Formats](formats.en.md), under "PPTX".

```bash
python3 "$S/pptx_edit.py" apply "$R" --batch N --dry-run --locale en
python3 "$S/pptx_edit.py" apply "$R" --batch N --locale en
# To apply selected quick edits, replace --batch N with --id ID (repeatable).
```

- `apply` writes report-side quick replacements/deletions (following the slide location down to the exact text) and existing speaker-notes edits (only when no external script is used). Edits are checked as a whole: an ambiguous location, mismatched original text or read-only structure prevents the entire selection from being written. Quick replacements keep untouched paragraphs and run properties; chart data, SmartArt, animations, media and OLE are outside the write scope.
- Whitelisted geometry and common style changes go through an operations file (normalized coordinates, per-target evidence digests):
  ```bash
  python3 "$S/pptx_edit.py" ops "$R" --ops opsfile.json --dry-run --locale en
  python3 "$S/pptx_edit.py" ops "$R" --ops opsfile.json --locale en
  ```
  The operations format and capability boundaries are in [Formats](formats.en.md); one element can be hit by at most one operation per group. `warnings` are automatic geometry hints (overlap, possible text overflow) — advisory only; `visual` is always "not run".
- The default updates the source PPTX after saving a binary original (0600) in `.snapshots/batch-N/pptx-writeback/`; ops without a batch use `batch-0`. `--output new.pptx` keeps the source and requires a new output path. `--dry-run` writes neither document nor backup. PowerPoint owner files and source fingerprint changes are refused before writing; when the source has changed since the review was built — including the agent's own previous write — rebuild the review page before continuing.
- After each successful write, rebuild the review page (re-rendering the affected slides and checking them one by one for overflow, overlap, out-of-canvas content and fonts), check `notes.py diff`, then record each result with `resolve` and finish with `done` per the batch requirements; the command never resolves annotations automatically. **A successful write, an openable ZIP, or parseable XML never counts as visual verification.**

For an analysis round, follow the [analysis guide](analysis.en.md) instead of the revision steps below. Analysis proposes questions and explanations; only annotations later confirmed by the reviewer enter revision.

`wait` prints the batch number, original message, structured requirements, applicable skills, and annotations. It also claims the batch. A review request starts the review stage in section 6. Inspect the state at any time:

```bash
python3 "$S/notes.py" show "$R" --batch N --locale en
python3 "$S/notes.py" show "$R" --all --locale en
python3 "$S/notes.py" show "$R" --json
```

`--page N` filters by page. The source paths at the top identify what to edit. Read the target, stable key, exact edit, paired sentence, quotation and side, point or region percentages, anchor hints, skills, and processing trail. Do not treat English labels as stored enums. English output retains every original annotation and handling explanation.

For each annotation:

1. Invoke its applicable skills. Locate the quoted text or marked element in the original source. For generated reports, edit upstream and render again.
2. Make the requested edit. Check the other source on the same page and record whether it also changed. A region applies to all content inside it. Style requests change layout, alignment, hierarchy, color, spacing, or charts without inventing facts.
3. Follow the shared protocol's full-check requirements when selected. Leave unsupported decisions for the user and report any unread or ambiguous material.
4. Rebuild the review page. The browser offers an update/refresh notice.
5. Record each completed annotation, then finish the accepted batch:

```bash
python3 "$S/notes.py" resolve "$R" ANNOTATION_ID --seq N --by agent --note "Updated the heading and the matching speaker notes." --locale en
python3 "$S/notes.py" done "$R" --seq N --summary "Updated both sources. Checked all six pages; supplier choice still needs your decision." --locale en
python3 "$S/notes.py" batches "$R" --last 3 --locale en
```

Use `--by` to identify each worker; [Agent mechanisms](agent-mechanisms.en.md) describes the environment-dependent default. Use a summary that describes what was actually checked. Keep unimplemented annotations open and explain why. `reopen` corrects a mistaken resolution. Report changed pages, unresolved decisions, and full-check scope to the user; ask them to refresh and inspect. With a saved event binding, leave the service running and end the turn; with a verified host completion-notification mechanism, start `wait` again. A running process, exit 0, or a claimed batch does not alone prove chat receipt. If the batch requested a stop after the initial edit, stop at that stage as specified below.

If the user says “apply my annotations” directly, first run `show`. Claim a pending submission before editing so its options and source snapshot apply. With no submission, work from the explicit request and resolve completed annotations; `done` reports that no batch exists.

### Maintaining verification info after a revision (reports with a verification record)

When the report has a verification record beside it (`report-record.json`, step 4 of the [generation guide](generation.en.md)), any content, source, or style change requires maintaining the affected verification info. Old deliverable verdicts never silently cover new content; the check entry marks records that no longer match the current artifacts as such.

- **Determine the affected scope**: check the citations and calculations for the pages you changed; when the same figure or wording appears on other pages or in the matching script sentence, check those too (both sides stay in sync); when a shared style or template changed, include every affected page in the visual check and redo the page-by-page evidence before declaring. After reordering pages, annotations and record references follow the stable page keys (`data-key`) — rewrite the document order and update `data-page`; do not just swap attributes.
- **Update the record**: refresh `links.sha256` for the changed artifacts; update the affected `findings` (refs, calculation, support) and check results to match what you actually verified; then write the `revision` declaration — `basis` states how the affected scope was determined, `rechecked` lists the check ids verified this time (each with an actual result in `checks`), and items whose scope could not be confirmed go into `pending` with scope and reason. Never pretend verification completed. Evidence you can prove unaffected keeps its original status; it does not count as rechecked.
- **Re-verify**: after rebuilding, run `report_check.py` (step 5 of the generation guide). The summary lists the rechecked scope and the pending scope; until the pending items are confirmed, the report cannot reach a deliverable verdict. A normal revision maintains only the affected scope and never opens a full check on its own; when the user explicitly asks for overall verification, run the complete checklist and disclose the full coverage.
- **Existing reports without a record**: reviewing, annotating, editing, and rebuilding work exactly as before; do not force a generated record onto them. When the user explicitly requests overall verification, build the record with the complete checklist; register any unavailable original evidence as a gap instead of treating the report's own claims as source evidence.
- **Status separation**: maintaining verification info only affects the report quality verdict, never batch status or review rights — a batch stopped at *drafted, waiting for review* still waits for the user; subagent permissions, exact edits, and existing stop conditions keep constraining revisions; overlapping operations keep following the existing file locks and batch rules, and locked state files are never rewritten directly.

## 6. Initial edit, review, and delegation

| Accepted requirement | Required result |
|---|---|
| Finalize, no subagents | Handle the work personally. `resolve` records a final resolution; `done` finishes the batch. |
| Stop at drafted, waiting for review | `resolve` records an initial edit. `done` stops at that stage. Report it and wait for the user to request review or finalize. |
| Subagents allowed | You may delegate permitted work. Carry the batch number, target, full-check option, source paths, and skills to each worker. |
| Full check | Check the whole report and notes after each editing stage, with evidence and uncertainty as defined in the shared protocol. |

A review request is an explicit later action. Accept it with `wait` or `claim --stage review --seq N`. Then inspect the initial edits:

```bash
python3 "$S/notes.py" diff "$R" --batch N --locale en
python3 "$S/notes.py" diff "$R" --batch N --to current --locale en
python3 "$S/notes.py" review "$R" ANNOTATION_ID --seq N --ok --locale en
python3 "$S/notes.py" review "$R" ANNOTATION_ID --seq N --revise --note "Shortened the heading and updated the notes." --locale en
python3 "$S/notes.py" review "$R" ANNOTATION_ID --seq N --return --note "Two definitions remain possible; please choose." --locale en
```

Approve, revise directly, or return a decision to the user. Give reasons for revision or return. Do not send work back to the initial editor. Handle remaining open annotations in the same accepted batch with `resolve --seq N`. Review verdicts cannot apply to another batch or to a batch that has not entered review. Rebuild and run `done --seq N --summary ...`. Unreviewed drafts block completion; use `--accept-rest` only after confirming every remaining draft is acceptable.

For permitted delegation, choose the host-specific method in [Agent mechanisms](agent-mechanisms.en.md). If model selection is available, a lower-cost model can make the initial edit and a stronger model can review it. Without a subagent mechanism, the main session handles the accepted work personally. When delegating, the main agent gives an initial-edit worker the accepted requirements and asks it to `resolve --seq N --by NAME`, without `done`. The main agent runs `done --seq N --draft`. A final target enters the review queue; a review target stops for the user. Claim an authorized review with `claim --stage review --seq N`, give the reviewer the same requirements, and finish with `done --seq N` after its verdicts. Separate sessions may wait with `--stage draft` or `--stage review`; each handles only its accepted sequence.

## 7. Imports and recovery

```bash
python3 "$S/notes.py" import "$R" annotations.md --locale en
python3 "$S/notes.py" export "$R" --lang en --all --out annotations.md
python3 "$S/notes.py" export "$R" --format json --all --out annotations.json
```

Import pasted Markdown by saving it as UTF-8 first. Imports retain visible edits, marks, pairs, styles, skills, and declared statuses. JSON is the complete structured backup. Marked Markdown selects its own grammar; old Chinese Markdown still imports. Same-ID annotations update only when newer; identical content does not duplicate. Always-on skills import too; an existing different list produces a conflict for the user to resolve with `skills --always`.

JSON review IDs are checked. Import into the matching review, or obtain confirmation before forcing a different identity. Browser “unsaved changes from another review” exports carry that other identity; recover them there. Queued edits, deletions, and submissions remain in `ops` for inspection and are not replayed by annotation import. Clear recovered browser data only after confirming the import.

After an interrupted session, run `show`. `wait` immediately accepts pending initial-edit or review requests. If a batch is already claimed or reviewing and no agent is handling it, resume that same stage with its accepted options and finish it. Do not claim that work was completed merely because it was previously claimed.

For browser data from file mode, export under the original `file://` origin, then import into the service review. For an old page, use its copy/download controls; the current page can also offer a legacy import when data is visible under the same origin. A disconnected service queues changes and sends them after reconnection. A wrong service keeps them isolated rather than writing into a different review.

## 8. Troubleshooting

```bash
python3 "$S/serve.py" status "$R" --locale en
python3 "$S/serve.py" check --locale en
python3 "$S/serve.py" "$R" --debug --locale en
```

- Status exit codes: 0 running; 3 stopped; 4 stale state cleared after the process exited; 5 process alive but not responding. Startup code 6 means an old service is still running. Upgrade in this order: stop it, rebuild, start the current service, refresh the page.
- A PPT review build reports a missing renderer (`noRenderer` / `noRasterizer`): PPT mode renders slides with local LibreOffice (`soffice`) and Poppler (`pdftoppm`). Install whichever is missing (macOS: LibreOffice.app is detected at its default location; Linux: the `libreoffice` and `poppler-utils` packages); nothing is downloaded or installed automatically. For a failed conversion, page-count mismatch or empty output, check that LibreOffice on this machine can open the source file (convert a legacy `.ppt` to `.pptx` first). Rendering and font evidence is described in the [format reference](formats.en.md) under "PPTX". After installing the tools, build again — no manual image conversion is needed.
- A local-file badge while a service is running means the user opened the HTML directly; use the printed HTTP address. A wrong-service badge identifies another review or program on the port and gives a command to start the intended review by ID.
- `reject_counts` uses stable codes such as `bad-host`, `bad-origin`, `no-origin`, `cross-site`, `bad-content-type`, and `review-mismatch`. `errors` counts server-side write failures. Read reason codes and timestamps in `server.log`; request-supplied text is untrusted. Preserve validation when resolving a problem.
- Repeatedly failed changes can leave the send queue. Download the browser backup and inspect/import it. A corrupt `notes.json` prevents startup and is not overwritten; preserve a backup and use `notes.jsonl` as recovery evidence.
- A corrupt ledger does not overwrite itself. Review generation and annotation handling can continue with a warning and temporary port allocation. Repair the ledger or move it aside after preserving it. For synthetic demos, set `AE_HOME` to a temporary directory before any command.
- For installation or port problems, find the repository path in the skill copy's `.install-record.json` (`repo`). Run `python3 /path/to/repo/ae.py doctor --locale en` or add `--json`. For a custom installation, add `--target "/path/to/skills"` to both `doctor` and `repair`; this is the parent directory of the skill copy after resolving symlinks. `repair` lists its plan and performs safe repairs. Obtain approval for destructive options such as `--forget-stale` or `--stop-orphans`.
