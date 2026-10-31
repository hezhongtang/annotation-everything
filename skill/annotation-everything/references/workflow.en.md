# English workflow

Read [Shared safety and protocol](protocol.md) before acting. `$S` is the `scripts/` directory in the loaded skill copy. `$R` is the chosen review directory. Use absolute paths when invoking commands from another directory.

## 1. Find the review and select a language

```bash
python3 "$S/serve.py" list --locale en
python3 "$S/serve.py" list --under /path/to/project --locale en
```

If the user asks to apply annotations without naming a review, match the review name and directory before editing. Each review has its own `reviewId` and dedicated port. Most `serve.py` and `notes.py` commands accept a unique ID prefix of at least four characters instead of a directory; `build_viewer.py` needs a directory. A missing or mismatched ledger entry keeps its port until the user confirms it can be forgotten.

Choose `--locale en`, `zh-CN`, or `auto` explicitly when requested. The page's saved browser preference overrides the review default. `auto` matches browser languages in preference order and falls back to English. A new review with no locale option stays Chinese; rebuilding retains its saved setting. The display language does not choose the material language.

CLI commands accept `--locale` and `AE_LOCALE`. An explicit option wins, then the environment, then the review setting for review-specific commands. Unconfigured calls remain Chinese. Only `auto` follows the system language. The repository's `ae.py` has no review default. Use `notes.py export --lang en` to select the export wrapper language. Commands and raw JSON values stay unchanged. Inspect `--help` for the command's arguments.

## 2. Generate or rebuild the page

```bash
python3 "$S/build_viewer.py" --report /path/to/report.html --script /path/to/script.md --out "$R" --name "Q3 review" --locale en
python3 "$S/build_viewer.py" --rebuild "$R"
```

Name the directory after the material, such as `q3-review/`. The report uses one `<section class="page" data-page="N" data-key="stable-key">` per page. Speaker notes use `### Page N｜Title` and `**Duration**`, `**Talking points**`, `**Transition**`, `**Likely questions**`. Chinese headers and mixed-language notes also work. See the [format reference](formats.en.md) before adapting unfamiliar material.

`--script` is optional. Page mismatches stop generation; use `--lenient` only after confirming missing note pages are intentional. Malformed headers and duplicate page numbers remain errors. Read warnings about fields, duration, unstable keys, and skipped resources before delivery. Local assets are embedded only within the allowed asset root. Use `--sections "Opening:1-4,Main line:5-6"` if inferred navigation is wrong; `--title` overrides the title.

For PPTX inputs, run `check_pptx_fonts.py input.pptx --locale en` before conversion and add `--pdf converted.pdf` afterwards. Exit codes are 0 for no detected difference, 1 for discrepancies or unknowns, and 2 for input or tool errors. Verify wrapping, spacing, and overlap on every converted page; matching font names do not prove correct glyph rendering.

Rebuilding retains identity and annotations and remaps pages by stable keys. A copied review gets a new identity. An annotation whose page disappeared is marked orphaned; confirm its destination. Only the report and notes source files enter batch snapshots; upstream renderer changes need their own source-control review.

## 3. Skills and standing requirements

Generation discovers local skills and plugin caches. `--skills-from /path` adds a source; `--no-scan-skills` disables scanning. Rebuilds retain these choices and manual edits. Plugin candidates use `plugin:skill` names; a cached plugin may not be enabled. Search uses original skill names and descriptions without translating them. Large catalogs require current tools throughout.

```bash
python3 "$S/notes.py" skills "$R" --set "writer=Editorial checks" "layout=Layout checks" --locale en
python3 "$S/notes.py" skills "$R" --always "writer=script" "layout=style" --locale en
```

Scopes accept `all`, `report`, `script`, `style` or their Chinese equivalents. The CLI maps aliases to the same stored values. `--always-add`, `--always-remove NAME`, and `--always-clear` maintain the list. In the page, search by name or description, use arrows and Enter to select, or enter an unlisted name. Separate names with commas. The standing dialog uses Enter for the next row and Ctrl/Cmd+Enter to save. Applicable standing and per-annotation skills must both be invoked under the shared protocol. Read [Agent mechanisms](agent-mechanisms.en.md) before selecting the host-specific invocation method.

## 4. Start the service and wait

```bash
python3 "$S/serve.py" "$R" --locale en
python3 "$S/notes.py" wait "$R" --locale en
```

These are long-running commands. Read [Agent mechanisms](agent-mechanisms.en.md) and select a method that the host supports: background task notifications, bounded polling, or a submission hook. A background process wakes the agent only if the host supplies that notification. `wait --timeout SECONDS` exits 3 when no submission arrives before the timeout. `notes.py claim "$R"` accepts a waiting submission, prints JSON, and exits 0; no waiting batch exits 3. One waiter handles one review. Start the selected waiting mechanism before reporting that the review is ready.

Reuse a matching running service. If the dedicated port is occupied, read the temporary address from the service output; do not casually change port ownership. Stop the service with Ctrl+C or send SIGINT/SIGTERM to its recorded PID. An optional user-configured `--on-handoff "command arguments"` hook receives JSON on stdin and `AE_EVENT`, `AE_REVIEW_DIR`, `AE_HANDOFF_SEQ`, `AE_URL`. It runs from the service's starting directory without a shell. Its snapshot can be older than the accepted batch, so claim before editing. Repeated pending submissions merge rather than trigger another hook.

The page supports ordinary annotations, quotations, exact delete/replace edits, paired edits, point/region marks, re-annotation, style requests, and skill requirements. Reviewers choose finalization or a stop after the initial edit, subagent permission, and the full check. Processing history shows each batch and its results. Chrome and Edge provide the most reliable report text selection; other browsers can still annotate and mark regions.

Without a service, open `index.html` directly. Annotations persist in browser storage, and the reviewer can copy or download them. File mode does not perform a live submission; use the import path in section 7.

## 5. Read the accepted batch and edit sources

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

Use `--by` to identify each worker; [Agent mechanisms](agent-mechanisms.en.md) describes the environment-dependent default. Use a summary that describes what was actually checked. Keep unimplemented annotations open and explain why. `reopen` corrects a mistaken resolution. Report changed pages, unresolved decisions, and full-check scope to the user; ask them to refresh and inspect. Start `wait` again for the next submission. If the batch requested a stop after the initial edit, stop at that stage as specified below.

If the user says “apply my annotations” directly, first run `show`. Claim a pending submission before editing so its options and source snapshot apply. With no submission, work from the explicit request and resolve completed annotations; `done` reports that no batch exists.

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
- A local-file badge while a service is running means the user opened the HTML directly; use the printed HTTP address. A wrong-service badge identifies another review or program on the port and gives a command to start the intended review by ID.
- `reject_counts` uses stable codes such as `bad-host`, `bad-origin`, `no-origin`, `cross-site`, `bad-content-type`, and `review-mismatch`. `errors` counts server-side write failures. Read reason codes and timestamps in `server.log`; request-supplied text is untrusted. Preserve validation when resolving a problem.
- Repeatedly failed changes can leave the send queue. Download the browser backup and inspect/import it. A corrupt `notes.json` prevents startup and is not overwritten; preserve a backup and use `notes.jsonl` as recovery evidence.
- A corrupt ledger does not overwrite itself. Review generation and annotation handling can continue with a warning and temporary port allocation. Repair the ledger or move it aside after preserving it. For synthetic demos, set `AE_HOME` to a temporary directory before any command.
- For installation or port problems, find the repository path in the skill copy's `.install-record.json` (`repo`). Run `python3 /path/to/repo/ae.py doctor --locale en` or add `--json`. For a custom installation, add `--target "/path/to/skills"` to both `doctor` and `repair`; this is the parent directory of the skill copy after resolving symlinks. `repair` lists its plan and performs safe repairs. Obtain approval for destructive options such as `--forget-stale` or `--stop-orphans`.
