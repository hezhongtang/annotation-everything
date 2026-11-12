# Input and output formats

Use this reference with the [English workflow](workflow.en.md) and [shared protocol](protocol.md). The [Chinese reference](formats.md) documents the same inputs and storage fields. Display language never determines input language.

This page covers existing reports, scripts, and review data. **To generate a new report from raw material**, read [Report generation: default paradigm](generation.en.md) first; the field contract for the verification record that accompanies new reports (schema `ae-report-record/1`, saved as `report-record.json` beside the report) and the public check entry `scripts/report_check.py` live in that guide. Existing reports without a record go straight into review; there is no need to backfill one.

## HTML report

```html
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sample report</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box}</style>
</head><body>
<section class="page" data-page="1" data-key="cover" id="page-cover"><h1>Cover</h1></section>
<section class="page" data-page="2" data-key="summary" id="page-summary"><h2>Summary</h2></section>
</body></html>
```

Use one section per page, with `page` as a separate class token, an integer page number, and a stable unique key. Design at 1280 × 720. Nested sections are allowed. Duplicate or noninteger page numbers are errors. Without `data-key`, the generator uses `id` without `page-`, then `pN`; keys derived from page numbers cannot reliably track reordering.

The generator collects `<style>` and local stylesheets in order. Shadow DOM isolates them from the surrounding page. It maps `:root` and `html` to `:host`, preserves variables and root font size, and resets body layout properties that could displace slides. `vw`, `vh`, and media queries still use the browser window. Scripts and inline event handlers do not run.

Local images, fonts, audio, video, and CSS assets within the report directory are embedded as data URIs, with a 10 MB per-file limit. `--asset-root` can explicitly widen the allowed root. Unsupported types, out-of-root files, and external resources are skipped with warnings. Check the warnings and rendered result.

Links to `#page-KEY` navigate within the review. For section navigation, links can use `data-range-start="KEY"` and `data-range-end="KEY"`. If `.toc-label` links exist, they define the main sections; nested ranges are omitted. Use `--sections` to override inference.

Convert other source formats (except Word documents; see the next section) to this page structure first. Each section may contain a converted slide image. For PPTX, check declared fonts before conversion, compare PDF embedding afterwards, and inspect every page visually. `fc-list` (fontconfig) and `pdffonts` (poppler) provide evidence; missing tools, fallback fonts, script-specific fonts, outlines, and images can leave unknowns. A successful name comparison does not prove glyph-level rendering.

## Word documents (DOCX)

`--report` also accepts a `.docx` directly; no conversion to page-by-page HTML is needed:

```bash
python3 "$S/build_viewer.py" --report contract.docx --out contract-review --name "Purchase contract"
```

- **Pagination** follows the breaks Word saved (`w:lastRenderedPageBreak`), hard page breaks, page-break-before and section breaks. Page size, orientation and margins come from each section, so an A4 portrait page is shown at 794 × 1123 px instead of the landscape stage. Content longer than a page extends downward and is never cut off. The review page fits each page to the pane width and scrolls vertically.
- **Pagination confidence** appears in the generator output, in `review.json` (`docx.pagination`) and in the review page banner:

  | Value | Meaning | What to do |
  |---|---|---|
  | `verified` | Word's saved breaks match the page count in `docProps/app.xml` | Treat as Word's original pagination |
  | `unverified` | Saved breaks exist but the page count differs or is missing | The pagination may be stale; save once in Word and rebuild |
  | `estimated` | No saved breaks (python-docx and some online exports) | Split only at explicit breaks; pages are not Word page numbers. Save once in Word or WPS and rebuild |

  There is no read-only page-image mode.
- **Locators**: body paragraphs carry `data-docx-p` (document order of `w:p` in `document.xml`) and `data-docx-pid` (`w14:paraId`); runs carry `data-docx-r`. Page keys use the paraId of the first paragraph on the page (with `~c` when the page starts mid-paragraph), or a digest of the page's opening text when paraIds are missing. Body paragraphs also carry `data-docx-d` (text digest: the first 12 hex digits of the SHA-1 of the complete paragraph text, including whitespace), and the continuation of a paragraph split across pages carries `data-docx-o` (the character offset where that fragment starts). Headers, footers, text boxes and repeated table headers carry no locators.
- **Annotation locations**: quoting text, placing a point or drawing a region on the report writes a versioned `docx` field on the annotation (see `notes.json` below) with the paragraph's paraId, paragraph number, text digest and the character offsets of the selection inside the paragraph. When the browser exposes no selection inside the Shadow DOM (non-Chromium), the page looks the text up on the current page, writes a location only when exactly one paragraph contains it, and records the source as `text`. A rebuild first finds the page each paragraph is on now and updates `page`, `key` and `title`: a paraId that is unique in the document is used directly; when the paraId is missing, duplicated or gone, the paragraph number and text digest must agree (a unique digest is accepted; several identical digests also require the same number), and the generator output says so. Cross-paragraph quotes and regions check both endpoints and update both paragraph numbers. If either endpoint is missing, ambiguous or out of order, the annotation is marked `orphan` and keeps its page number and old fingerprint. Once the reviewer confirms where the annotation belongs or moves it to another page, its `docx` location is removed and the page key applies from then on. The digest includes whitespace; pagination changes alone leave it unchanged. After text or whitespace edits, only a unique paraId can still confirm the paragraph identity. A single-page paragraph retains `changed` and the old fingerprint. If the edited paragraph spans pages, the old offset cannot establish which fragment it belongs to: the annotation is marked `orphan` and keeps its page number. Without a unique paraId, text edits that change the digest also require confirmation. Page keys only apply to annotations without a paragraph location. `docx.fingerprint` in `review.json` is the SHA-256 of the source file; an annotation whose `docx.fp` equals it has been checked against the current version.
- **Write-back**: `docx_edit.py apply REVIEW --batch N`, or repeated `--id ID`, applies open report-side quick deletions and replacements. For a free-form annotation use `replace REVIEW --paragraph N --from ORIGINAL --to REPLACEMENT`; paragraph numbers start at 0. paraId, paragraph number and text digest are cross-checked; original text matches exactly first, then with whitespace differences ignored. Repeated text needs selection offsets that identify one occurrence. Cross-run edits retain run formatting; ranges crossing paragraphs, tabs, symbols or line-break structures are not written automatically. Quick edits on read-only structures, unlocated selections or cross-paragraph ranges still save as annotations and explain the limitation.
  - Only changed `w:t` elements in the intended paragraphs are rewritten. Other XML bytes and other ZIP members' local headers, compressed streams and descriptors are preserved; only required offsets change in the central directory. UTF-8, BOM, alternate namespace prefixes, entities, CDATA and empty text elements are supported. UTF-16, ZIP64/split archives and unsupported package structures are explicitly refused. Limits: 256 MiB package, 64 MiB per XML part, 512 MiB total expanded members.
  - In-place writing is the default: save a binary original with mode 0600, write a same-directory temporary file and fsync, then reopen and check XML, paragraph count, expected text and every member's CRC. Recheck the source fingerprint and Word owner file before committing with `os.replace`. Failure leaves the original in place and keeps any backup already created. Backups use `.snapshots/batch-N/docx-writeback/`, with N = 0 when no batch was specified.
  - Word owner files (`~$`), a source fingerprint differing from `review.json`, digital signatures, editing protection and Track Changes block writing. Only Track Changes has an explicit override, `--allow-untracked`, which warns that AE writes untracked changes; paragraphs containing native revisions remain read-only. Follow other refusal instructions in Word and rebuild; there is no force-overwrite option.
  - `--dry-run` previews only; `--output NEW_PATH` writes a new file and keeps the source; `--locale zh-CN|en` chooses the CLI language. Write-back does not change annotation statuses or refresh the fingerprint: rebuild, check the result and record the outcome as described in the [revision workflow](workflow.en.md). Binary snapshots and page-text comparisons through `notes.py diff` also apply.
- **Not supported**: legacy binary `.doc`, encrypted documents and Strict Open XML stop with a specific error and a fix; they are never read as text. DOCX files are treated as untrusted: colors and font names are validated before they reach styles, XML parts are size-limited and DTDs are rejected.

Support matrix (the generator lists structures that are approximated, placeholders, hidden or read-only, with their pages; the review page shows the same list under "Show limitations"):

| Structure | Display | Paragraph write-back |
|---|---|---|
| Body text, highlighting, strikethrough, bold, italic, underline, size, color | As written | Yes |
| Tables (column widths, merged cells, shading, borders, repeated headers) | Approximate: conditional table-style formatting is not reproduced; rows spanning pages are split at break markers | Yes |
| List numbering | Approximate: computed with Word's rules, not selectable | Yes (numbers themselves are not written) |
| Headers and footers | Approximate: per section, first-page and odd/even settings; PAGE / NUMPAGES / SECTIONPAGES use actual numbers | Not applicable |
| Images | Approximate: inline at original size, each image embedded once; over 10 MB shows a placeholder | Not applicable |
| Floating images and shapes | Approximate: shown at the anchor, without original position | Not applicable |
| Tracked changes (insertions, deletions, moves, table row/cell changes) | Approximate: green underline / red strikethrough; neither accepted nor rejected | No |
| Formatting changes | Approximate: current formatting only | No |
| Word comments | Placeholder: `[W id]` marker with hover text and a tinted range; not imported as AE annotations | Yes |
| Text boxes | Approximate: inline dashed box at the anchor | No (and not locatable) |
| Fields (TOC, dates, cross-references) | Approximate: result saved by Word | No |
| Footnotes and endnotes | Placeholder: reference marks only | Not applicable |
| Equations | Approximate: plain text | No |
| Content controls | Approximate: their content | No |
| Hidden text | Hidden (as in Word by default) | No |
| Embedded OLE objects, linked images, unsupported image formats | Placeholder | No |
| Multiple columns | Approximate: single column | Not applicable |
| Unrecognized structures | Hidden; the diagnostic lists element names | No |

`review.json` records `format` as `{"report": "docx", "version": 1}`. When the version is newer than the installed copy supports, `--rebuild`, `serve.py` and `notes.py` stop with a clear error instead of reading the file as text. Older reviews have no `format` field and are treated as page-by-page HTML.

## PPTX (direct review and whitelisted write-back)

`--report` accepts a `.pptx` directly; each slide becomes one review page that keeps the original size, orientation and order. Slides are rendered to images with local LibreOffice (`soffice`) and Poppler (`pdftoppm`): on macOS install LibreOffice.app, on Linux install the `libreoffice` and `poppler-utils` packages, and the build then works without any manual conversion. A missing tool, a failed conversion, a page-count mismatch, or empty output fails the build with a clear diagnostic; nothing is downloaded or installed automatically, and a text preview never stands in for visual review. The legacy binary `.ppt` (as well as `.pptm`, `.potx`, `.ppsx`) is not supported; save it as `.pptx` in PowerPoint or WPS first. Run `python3 $S/check_pptx_fonts.py deck.pptx` before conversion and add `--pdf converted.pdf` afterwards for font evidence; page-by-page visual inspection stays mandatory, and matching font names do not prove correct glyph rendering.

**Text and notes write-back**: `python3 $S/pptx_edit.py apply REVIEW --batch N` (or repeated `--id ID`) applies pending report-side quick replacements/deletions and existing speaker-notes edits. Report-side edits follow the annotation's slide location (slide key + element identity + logical paragraph + code-point offsets) down to the exact `a:t`; cross-run ranges keep untouched text and its properties. The script side is written only when no external script is used, matching the quoted text uniquely. Only the target slide/notes parts are rewritten; all other parts and ZIP metadata keep their original bytes. A 0600 binary backup is saved first (`.snapshots/batch-N/pptx-writeback/`), then the package is written to a temporary file and atomically replaced; after writing, every member CRC and the text model are re-checked. PowerPoint owner files (`~$`) and source fingerprint changes are refused before writing. `--dry-run` previews; `--output NEW` writes to a file that does not exist yet. Write-back never changes annotation status; rebuild the review page and re-render the affected slides afterwards.

**Geometry and style write-back (whitelist contract v1)**: agents issue explicit element-level changes through `python3 $S/pptx_edit.py ops REVIEW --ops OPSFILE`. The operations file is UTF-8 JSON `{"v": 1, "ops": [...]}`; a `v` newer than the supported version is rejected as a whole. Each operation looks like:

```json
{"v": 1, "ops": [
  {"id": "move-title", "slide": "s260", "aspect": "geometry", "unit": "normalized",
   "set": {"x": 0.25, "y": 0.125, "width": 0.5, "height": 0.25},
   "target": {"key": "e2", "id": "2", "digest": "64-hex evidence digest"}},
  {"id": "recolor", "slide": "s260", "aspect": "style", "scope": "fill",
   "props": {"color": "FF8800"},
   "target": {"key": "e2", "id": "2", "digest": "64-hex evidence digest"}}
]}
```

- **Target**: `slide` is the persistent slide key; `target` has the same shape as an element target in the annotation exchange (`key`/`id`/`digest` required). The element is resolved by identity first: a unique shape id is accepted, otherwise the evidence digest disambiguates. Failed resolution, conflicting evidence and duplicate ids are refused; a nearby look-alike is never guessed. One element can be hit by at most one operation per group.
- **Capability boundary**: only elements whose capability matrix declares `geometryWrite` / `styleWrite` are writable - top-level ordinary shapes (including placeholders with an explicit `a:xfrm`). Group interiors, pictures, charts, SmartArt, media, OLE, shapes with extensions or fields stay read-only; elements without their own `a:xfrm` (geometry inherited from the layout) cannot have geometry written.
- **Geometry (aspect=geometry)**: `unit` must be `normalized` (0 to 1 relative to the slide canvas); other units are refused. `set` takes a non-empty subset of `x`, `y`, `width`, `height`, converted to source EMU per the canvas size (`a:off` x/y, `a:ext` cx/cy); dimensions that are not set keep their current values. Negative positions, non-positive sizes, values beyond the canvas (tolerance 0.0001) and degenerate conversions are refused.
- **Style (aspect=style)**: `scope` is `text` (applies to every text run in the shape), `fill` (shape fill) or `line` (outline). The only allowed properties are - text: `bold`, `italic`, `underline` (booleans; false writes an explicit off), `size` (points, 1 to 4000, written as hundredths of a point), `color` (6-digit sRGB hex); fill: `color`; line: `color`, `width` (points, greater than 0 and at most 1584, written as EMU). Writes are always direct formatting: run `a:rPr` attributes and fill, the fill inside `p:spPr`, and the width and fill of `a:ln`. Untouched attributes and child elements (such as `a:prstDash`) keep their bytes, and new elements are inserted in ECMA-376 child order. Unknown properties, child structures outside the whitelist, out-of-range values and no-op operations that match the current direct formatting are refused; structures whose safety cannot be proven stay read-only instead of guessing inheritance.
- **Preflight and closed loop**: multi-element changes are preflighted target by target; if any target is unsafe nothing is written. The fingerprint, snapshot and atomic-replacement loop is shared with the text path. The result JSON lists actual property changes per operation in `changes` (`from`/`to`: EMU for geometry, contract units for style; `from` is `null` when no direct formatting existed and `mixed` when runs disagree). `warnings` are automatic geometric hints (new rectangle overlapping other elements, reduced size that may overflow text, edges slightly outside the canvas after EMU rounding) - advisory only; `visual` is always `{"status": "notRun"}` - visual verification has not run until the review page is rebuilt and the slides are inspected one by one, and the write-back must not be treated as passed before that.
- **Not supported**: arbitrary XML path or attribute writes, chart data, SmartArt, animations, media, master/theme rewrites, one-click restyling or auto-layout; do these in PowerPoint/WPS.

## Markdown speaker notes

```markdown
# Speaker notes: Sample report

## How to use these notes
- Lead with the conclusion.

### Page 1｜Cover
**Duration**: 20 s
**Talking points**:
All figures in this example are synthetic.
**Transition**: Now the summary.

### Page 2 | Summary
**Time**: 1 min 30 s
**Talk**:
One line per paragraph.
**Q&A**:
- Q: Where did growth come from? A: New stores.
```

Content before the first page header becomes the instruction overlay. A page header must use three hashes, `Page N` or `第 N 页`, a full-width `｜` or ASCII `|`, then the title. English headers are case-insensitive; spacing after `Page` is optional. Chinese and English pages and fields can be mixed in one file.

| Meaning | Chinese field | English field | English aliases |
|---|---|---|---|
| Duration | `**时长**` | `**Duration**` | `**Time**` |
| Body | `**讲法**` | `**Talking points**` | `**Talking point**`, `**Talk**` |
| Transition | `**过渡**` | `**Transition**` | — |
| Questions | `**可能被问**` | `**Likely questions**` | `**Likely question**`, `**Q&A**`, `**Questions**` |

Fields accept ASCII or full-width colons, case-insensitive English names, and flexible spaces between English words. Text before the first field, or the whole page when no fields exist, remains body text. A duplicate field uses its last value and warns about earlier lines. Unknown fields remain visible body text and produce a warning.

Write questions as `- Q: ... A: ...` or `- 问：...答：...`. A question marker is stripped only at the start. The first recognized answer marker separates the answer. A bare word-ending `A:` is not an answer delimiter. Optional leading `-`, `*`, or `•` is stripped.

Durations accept Chinese minutes/seconds, `min`, `minutes`, `seconds`, and `s`, including mixed components. Recognized components add up; a nonempty duration with no recognized component produces a warning.

Speaker-note page numbers must match the report sequence. A mismatch lists differences and stops; `--lenient` uses report order and leaves missing notes empty. Duplicate pages or malformed headers remain errors even in lenient mode, with a line number, source text, and expected syntax. Avoid page-like level-three headings in ordinary body text.

## Structured annotations

`notes.json` has `version: 1`, `rev`, `updatedAt`, and a `notes` array. New state writes use the CLI or service, not direct file edits. Key fields are:

| Field | Meaning and stable values |
|---|---|
| `id`, `page`, `key`, `title` | Annotation identity, page number, stable page key, original title |
| `target` | `报告` (report), `讲稿` (speaker notes), `报告和讲稿` (both) |
| `text`, `quote`, `quoteFrom` | Original annotation, quotation, and source side |
| `pin` | `{x, y}` percentages from the report's top-left corner |
| `region` | `{x, y, w, h}` percentages for the selected region |
| `anchor` | Text hints about elements under a point or within a region |
| `edit` | `{op: "delete", from: "..."}` or `{op: "replace", from: "...", to: "..."}` |
| `pair` | `{side, text, match}`; side uses the same Chinese values; match is `exact` or `similar` |
| `style` | `{scope}` where scope is `page`, `pin`, or `region`; the target is the report |
| `skills` | Original skill invocation names |
| `status` | `open`, `drafted`, `resolved`, or `held` |
| `resolution` | Original handling explanation, including the initial edit explanation for a draft |
| `at`, `updatedAt`, `resolvedAt`, `resolvedBy`, `resolvedBatch` | Creation, update, and final handling metadata |
| `draftedAt`, `draftedBy`, `draftBatch` | Initial edit metadata |
| `trail` | Append-only stage actions with `at`, `by`, `stage`, `batch`, `action`, `note`; at most 50 entries |
| `orphan` | Original page (DOCX: original paragraph) can no longer be found; confirm before handling |
| `docx` | DOCX reviews only. `{v: 1, kind: quote/pin/region, via: selection/text/point, fp, para: {id, n, digest}, start, end, endPara, run, changed}`: paragraph location with character offsets in Unicode code points; `run` is a hint only; `changed` means the paragraph text changed after a rebuild, so offsets refer to the old version. A `v` newer than the installed copy supports is kept as is (up to 4000 characters), is not used for remapping, and `notes.py show` marks it as unusable for write-back |

New annotations use either a point or a region. Older annotations can contain both. Legacy `style.region` is migrated on read. Rebuilds use stable keys to remap annotations and mark missing pages as orphaned; in DOCX reviews, annotations with a `docx` location are remapped by paragraph first. DOCX review pages require the `docx` service feature: older services that do not advertise it drop the `docx` field, so the page shows a blocking notice and stops writing until a current service is started and the page reloaded. The Markdown export carries the location on a `DOCX location: v1 …` line (`DOCX 定位：v1 …` in the Chinese grammar) and restores it on import. Skill scopes in `skills.json` remain `all`, `报告`, `讲稿`, or `style`.

`notes.jsonl` is an operation log. It includes annotation and submission operations and supports inspection and recovery. It is not an alternate unlocked write interface.

Held annotations use `status: "held"`, a `hold` reason (`分析中` or `结论已变`), and `heldBy` for the related analysis. Revision snapshots include only `open`. `fromAnalysis`, `basis`, and a source annotation's `analysisId` retain analysis links; same-ID updates and imports preserve unrecognized fields. Older importers and pages treat an unknown held status as open. Old services still submit only open annotations. Restart the service, rebuild the page, and refresh after an upgrade.

## Analysis data

`analysis/<analysis-id>/events.jsonl` contains append-only events; its first event carries the schema version. Current state is derived from the log, without batch-history or handling-trail limits on questions and answers. `analysis/counter.json` assigns round numbers unique across the review. `handoff.json` uses shared `submitSeq` / `order` values for revision, review, and analysis submission order, including submissions in the same second.

Analysis, annotations, and batches share `.notes.lock`. Starting an analysis holds the source and removes it from unclaimed snapshots under that lock. An emptied batch becomes `void`. Conclusion drafts stay in analysis events; confirmation adds an open annotation. Stable page keys are mapped again at conversion and confirmation; a deleted or ambiguous page is marked `orphan`.

`GET /api/analysis` returns summaries and queue counts; `?analysis=<id>` adds the full state, `linkedNotes` and current-line round context. Add `&round=N` to read any round in that analysis without switching lines. Reviewer writes use `/api/analysis/start|answer|submit|convert|confirm|release|activate|pause|redecide|update-note|keep-note|delete-note|seal|fork|switch-line|close|reopen|refute` with the existing Host, Origin, JSON content type, review identity, and applicable receiver checks. `start` accepts a source `noteId`, or `focus` with optional `page` and no source annotation. The `analysis` feature declares support; file mode does not provide analysis.

A round's `line` identifies its analysis line. `assumptions` stores stable identities and `confirmed` states (`pending`, `confirmed`, or `rejected`) per line; `refuted` is distinct from rejection. A decision's `assumptions` lists its direct dependencies. `facts` preserves stable identities, original text, sources and optional question references. `analysis-writeback --proposals` accepts the legacy question list or an object containing `proposals`, `assumptions` and `facts`. Adding, splitting, merging and voiding are structure proposals: confirmation changes the tree while historical nodes and decisions remain. See the [analysis guide](analysis.en.md) for fields and handling rules.

Free-text answers retain the original words and create stable options. `park` moves a question out of the frontier; `verify` records a verification request with Boolean `allowExternal` defaulting to `false`. Pausing or switching an analysis changes focus without increasing its baseline revision or cancelling submitted or claimed rounds. `notes.py suggest-analysis` only adds an annotation's `analysisSuggestion`; it does not create an analysis.

`protection` reports whether this analysis has a claimed round. A node's `branch` distinguishes the current branch, a detached branch and a void branch. Changing a decision or requesting an update voids unclaimed rounds on that line; a rejected baseline voids its round too. Still-valid answers carry into a new request on the same line without recreating confirmed entities. Done and void rounds reject further answers; valid answers during a claim do not increase the baseline revision.

A derived annotation's `tracking` controls whether it follows its basis. Invalid bases affect only open annotations or those held for changed conclusions. Claimed/reviewing batches keep their annotations, and resolved statuses remain unchanged. `updateRequests` stores requests and `updates` stores the agent's text drafts. Confirming a round's `U:` item changes the ordinary annotation and appends its old text and basis to `analysisHistory`. A writeback object can include `updates: [{noteId, text, node?}]`; rejecting a draft preserves the original text.

`lines` holds each line's latest node projection; `nodes` remains the current line's projection. Decisions, assumptions, facts and node-option identities are shared across lines. `checkpoints` has stable `cpN` identities, a line, source round, automatic/sealed flags and a snapshot. Snapshots include the focus, nodes, decisions, assumptions, facts, effective conclusions, unresolved questions and parked questions without nesting earlier checkpoints. Sealing an existing checkpoint changes only `sealed`; forking preserves inherited identities.

A snapshot's `pendingProposals` retains unconfirmed structure proposals without creating nodes on a fork. After refutation, a node's `reopened` preserves its old decision, answer, assumption and reason. `needsReview` includes invalid dependencies on the node or its ancestors; the old line's `basedOnRefuted` identifies the assumptions. Refutation is global, while snapshots and frozen contexts retain the judgement recorded at that time.

A writeback can include `deadEnds`, using stable `key` values for option or line proposals, full `reason` text and existing assumption dependencies. `state.deadEnds` assigns stable `eN` identities with `pending`, `confirmed`, `rejected` or `review` status. Refuting a dependency changes the same record to `review`. Round `D:` items use the answer operation for confirmation. The next round is ordered D → U → A → S → Q. Dead-end rejections expose only safe details such as the reason and original assumption text for presentation in the current language.

New rounds freeze complete context on `done` / `void`. Historical reads return `historical: true`, original `items`, `answeredItems`, answers, explanations and `writeback`. Old logs without frozen context retain compatible derived reads. A round's line is separate from the page's current line: background claims, answers, writebacks and automatic checkpoints belong to the round, while annotation validity follows the current line. `closed` rejects answers and state changes; reopening queues a new request. There is no analysis-delete operation.

Neither JSON nor Markdown exports contain analysis events, questions, trees, or round queues. Annotation JSON may retain held states and source/basis references but cannot reconstruct the analysis. JSON backs up annotations and standing skills, not the entire review; preserve the review directory for a complete backup. Markdown keeps its existing grammar and omits analysis links and hold reasons, so imported held annotations are interpreted as open.

## Submissions and stages

`handoff.json` stores `version`, `seq`, `updatedAt`, and `items`. Each batch has its sequence, status, original `userMessage`, compatibility `message`, `target` (`final` or `review`), `allowSubagents`, `fullCheck`, included annotation IDs and pages, standing skills, timestamps, handling summaries, and stage history.

Batch states are `pending` → `claimed` → `done`, with optional `drafted` → `review` → `reviewing` before completion. A `review` target stops at `drafted` until the user requests review or finalizes. New agents use structured options and the original message. Legacy `message` can contain the fixed Chinese full-check requirement; the current CLI handles this compatibility rule independently of locale.

The local API uses `POST /api/handoff`, `/api/handoff/review`, and `/api/handoff/finalize`. A submission accepts `message`, `userMessage`, `target`, `allowSubagents`, and `fullCheck`. Omitting `fullCheck` preserves it when merging a pending batch and otherwise defaults to false. `acceptedFullCheck` confirms what was stored. Review requests retain the accepted options. Use the CLI for agent operations so locking and batch ownership remain enforced.

Bulk finalization is reserved for reviewer confirmation on the page; there is no Agent or CLI entry. The `finalize-all` service feature advertises support. Older services hide the button and show update, restart and refresh guidance. `GET /api/notes` includes `handoff.finalizable`; `GET /api/handoff` includes top-level `finalizable`. This snapshot is `{count, batches: [{seq, ids}], skipped: [{id, batch, reason}]}`. Only `drafted` annotations in `drafted` or `review` batches are eligible; drafts in `claimed` or `reviewing` batches are skipped.

`POST /api/handoff/finalize-all` accepts JSON `{batches: [{seq, ids}]}` from the confirmed snapshot. Batch numbers must be positive integers; batch numbers and annotation IDs must be unique. An empty list makes no writes. Under the shared `.notes.lock`, the service rereads state and checks batch ownership and annotation status. New batches are outside the confirmation; a batch with newly added drafts is skipped as a whole to avoid leaving drafts inside a completed batch. The response includes `finalizedCount`, `finalizedIds`, completed batch numbers in `batches`, `skippedCount`, `skipped`, and updated `doc` and `handoff`. Reasons are `claimed` (being drafted), `reviewing` (being reviewed), `unavailable` (missing or ineligible batch), `changed` (status or ownership changed), and `unconfirmed` (outside this confirmation). The page reports actual partial results; cancel sends no write request, and disconnected actions are neither queued nor applied optimistically.

The summary and response fields are additive; older pages can ignore them. Stored formats and the batch state machine stay unchanged. Finalization uses the existing `accepted` annotation trail, `accept` batch stage, `done` status and per-batch logs. Existing endpoints continue serving older pages. The new write endpoint shares Host validation, same-origin Origin, JSON Content-Type and the no-CORS rule.

Source snapshots are stored in `.snapshots/batch-N/base/` when an initial edit is accepted and `draft/` when it finishes. They cover only the report and speaker-notes source paths, retain the latest 30 batches, and share the review's data protections. Processing history retains the latest 200 batches. `notes.py diff` compares pages against those snapshots.

## Markdown exchange and JSON backup

The Markdown export begins with `<!-- annotation-everything export v1 lang=en -->` or `lang=zh-CN`. The declared language selects the grammar in either interface. Unmarked legacy Chinese Markdown remains readable. Unsupported versions, languages, and unrecognized text produce errors rather than a guessed empty import. Old tools require an upgrade to read the new English format.

```markdown
<!-- annotation-everything export v1 lang=en -->
# Annotations · Sample review

## Page 2 | Summary
1. [Report · Pin 1 (x 38.8%, y 59.8%)] Change +12% to "YoY +12%"
   > Quote (Report): +12%
2. [Script] [Drafted] Shorten this sentence.
   Draft note: Shortened without changing the figures.
```

Markdown preserves page numbers and titles, targets, content, quotations, marks, edits, pairs, style, skills, status and visible handling explanations, plus standing skills. It does **not** preserve IDs, stable page keys, timestamps, processing trails, or batches. JSON carries the complete annotation structure and review identity. The visible Markdown body is authoritative when edited; there is no hidden snapshot overriding it.

Three legacy grammar limits also apply to English exports: body lines that look like metadata can be read as metadata; blank body lines and trailing newlines are discarded; a body starting with a status marker such as `[Resolved] ` or `[Drafted] ` is interpreted as that status. Multiline quotations fold into one line. Delete/replace operations retain the historical fixed Chinese forms (`删除引用的这段文字` and `改为：...`) for compatibility. Prefer JSON when exact structure or unconstrained text must survive. JSON backs up annotations and standing skills; it is not an archive of analysis events, submission history, or source snapshots. Preserve the complete review directory to retain those records.

```bash
python3 "$S/notes.py" export "$R" --lang en --all --out annotations.md
python3 "$S/notes.py" export "$R" --format json --all --out annotations.json
python3 "$S/notes.py" import "$R" annotations.md --locale zh-CN
```

Imports deduplicate annotations and check JSON review identity. A mismatched identity requires the matching target review or the user's confirmed `--force`. Original text, names, and v1 protocol values remain unchanged in either language.
