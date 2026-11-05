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

Convert other source formats to this page structure first. Each section may contain a converted slide image. For PPTX, check declared fonts before conversion, compare PDF embedding afterwards, and inspect every page visually. `fc-list` (fontconfig) and `pdffonts` (poppler) provide evidence; missing tools, fallback fonts, script-specific fonts, outlines, and images can leave unknowns. A successful name comparison does not prove glyph-level rendering.

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
| `orphan` | Original page no longer exists; confirm before handling |

New annotations use either a point or a region. Older annotations can contain both. Legacy `style.region` is migrated on read. Rebuilds use stable keys to remap annotations and mark missing pages as orphaned. Skill scopes in `skills.json` remain `all`, `报告`, `讲稿`, or `style`.

`notes.jsonl` is an operation log. It includes annotation and submission operations and supports inspection and recovery. It is not an alternate unlocked write interface.

Held annotations use `status: "held"`, a `hold` reason (`分析中` or `结论已变`), and `heldBy` for the related analysis. Revision snapshots include only `open`. `fromAnalysis`, `basis`, and a source annotation's `analysisId` retain analysis links; same-ID updates and imports preserve unrecognized fields. Older importers and pages treat an unknown held status as open. Old services still submit only open annotations. Restart the service, rebuild the page, and refresh after an upgrade.

## Analysis data

`analysis/<analysis-id>/events.jsonl` contains append-only events; its first event carries the schema version. Current state is derived from the log, without batch-history or handling-trail limits on questions and answers. `analysis/counter.json` assigns round numbers unique across the review. `handoff.json` uses shared `submitSeq` / `order` values for revision, review, and analysis submission order, including submissions in the same second.

Analysis, annotations, and batches share `.notes.lock`. Starting an analysis holds the source and removes it from unclaimed snapshots under that lock. An emptied batch becomes `void`. Conclusion drafts stay in analysis events; confirmation adds an open annotation. Stable page keys are mapped again at conversion and confirmation; a deleted or ambiguous page is marked `orphan`.

`GET /api/analysis` returns summaries and queue counts; `?analysis=<id>` adds the full state and round context. Reviewer writes use `/api/analysis/start|answer|submit|convert|confirm|release|activate|pause` with the existing Host, Origin, JSON content type, review identity, and applicable receiver checks. `start` accepts a source `noteId`, or `focus` with optional `page` and no source annotation. The `analysis` feature declares support; file mode does not provide analysis.

A round's `line` identifies its analysis line. `assumptions` stores stable identities and `confirmed` states (`pending`, `confirmed`, or `rejected`) per line; `refuted` is distinct from rejection. A decision's `assumptions` lists its direct dependencies. `facts` preserves stable identities, original text, sources and optional question references. `analysis-writeback --proposals` accepts the legacy question list or an object containing `proposals`, `assumptions` and `facts`. Adding, splitting, merging and voiding are structure proposals: confirmation changes the tree while historical nodes and decisions remain. See the [analysis guide](analysis.en.md) for fields and handling rules.

Free-text answers retain the original words and create stable options. `park` moves a question out of the frontier; `verify` records a verification request with Boolean `allowExternal` defaulting to `false`. Pausing or switching an analysis changes focus without increasing its baseline revision or cancelling submitted or claimed rounds. `notes.py suggest-analysis` only adds an annotation's `analysisSuggestion`; it does not create an analysis.

Neither JSON nor Markdown exports contain analysis events, questions, trees, or round queues. Annotation JSON may retain held states and source/basis references but cannot reconstruct the analysis. JSON backs up annotations and standing skills, not the entire review; preserve the review directory for a complete backup. Markdown keeps its existing grammar and omits analysis links and hold reasons, so imported held annotations are interpreted as open.

## Submissions and stages

`handoff.json` stores `version`, `seq`, `updatedAt`, and `items`. Each batch has its sequence, status, original `userMessage`, compatibility `message`, `target` (`final` or `review`), `allowSubagents`, `fullCheck`, included annotation IDs and pages, standing skills, timestamps, handling summaries, and stage history.

Batch states are `pending` → `claimed` → `done`, with optional `drafted` → `review` → `reviewing` before completion. A `review` target stops at `drafted` until the user requests review or finalizes. New agents use structured options and the original message. Legacy `message` can contain the fixed Chinese full-check requirement; the current CLI handles this compatibility rule independently of locale.

The local API uses `POST /api/handoff`, `/api/handoff/review`, and `/api/handoff/finalize`. A submission accepts `message`, `userMessage`, `target`, `allowSubagents`, and `fullCheck`. Omitting `fullCheck` preserves it when merging a pending batch and otherwise defaults to false. `acceptedFullCheck` confirms what was stored. Review requests retain the accepted options. Use the CLI for agent operations so locking and batch ownership remain enforced.

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
