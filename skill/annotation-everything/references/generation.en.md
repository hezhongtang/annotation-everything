# Report generation: default paradigm (English)

[中文](generation.zh-CN.md) · [English workflow](workflow.en.md) · [Input and output formats](formats.en.md) · [Shared safety and protocol](protocol.md)

With only AE installed, start here to turn raw material into a reviewable draft. This is the default paradigm; when the user names another generation skill, follow it, and the artifacts still go through the same checks (step 5). Generation reasoning, material reading, and content judgment are performed by you (the Agent); AE ships the workflow, base style, artifact conventions, and deterministic checks.

## Generation method: built-in paradigm and external generation skills

- **No designation and no extension need**: use this guide (the built-in paradigm) and record `method` as `{"kind": "builtin", "name": "annotation-everything default paradigm", "version": "<AE version>", "unavailable": []}`.
- **The user explicitly names a generation skill and it is available**: **actually invoke that skill** (locate and run it via the skill-invocation mechanisms in [Agent mechanisms](agent-mechanisms.en.md)) and follow its process; external skills **remain subject to the step 0 alignment contract and step 2–5 artifact rules** (nine-dimension alignment and Q&A before drafting, density modes, concrete visual rules, task-oriented navigation, supplement markers, per-page `<section class="page">`, stable `data-key`, optional script, and `ae-report-record/1`), and go through the same check entry (step 5) under the same acceptance. **A scanned candidate or a mentioned name is not an invocation**: claiming a designated skill was used means it was really invoked — record `method.invocation` stating how it was invoked and where the artifacts came from; the check entry verifies this declaration (`record.method`), and a missing basis surfaces as a program failure.
- **The designated skill is unavailable, disabled, or the invocation failed**: say so explicitly; never claim the skill was used or that its capabilities were met. When continuing with the built-in paradigm instead, record `method.requested` for the user-specified method and `note` for the reason; ask the user first if unsure whether to continue.
- **Capability gaps**: when an external skill lacks reading, layout, alignment, or verification capability, record the gaps in `method.unavailable`; missing sources, calculation definitions, deviations, or check evidence are disclosed in `gaps` and `deviations` — **the acceptance is not relaxed**: identical defects receive identical verdicts for built-in and external products.
- **Additional delivery formats** (PPTX / PDF / Word etc.): provided by external capabilities; AE's core ships no generic exporter. Record what was actually produced (e.g. `links.extras`) and what remains unmet in `gaps`; the core reviewable HTML still enters AE via step 6, and an extra format never replaces the shared acceptance evidence.
- **Never automatically search for or install remote skills**: generation skills are installed by the user in controlled locations; do not search, download, or install generation skills over the network, and do not widen the service boundary to admit external skills.

## 0. Clarify the request: nine dimensions and pre-draft Q&A alignment

Check and clarify the following nine dimensions before drafting (ask only for missing items that affect the output):

| Dimension | What must be clear | Where recorded / default handling |
|---|---|---|
| 1. **Purpose & expected action** | Decision, judgment, or next step expected from readers | `request.purpose` |
| 2. **Audience & reading mode** | Who reads it; live presentation vs standalone read vs thorough audit | Drives tone and items 5 / 7 |
| 3. **Core questions & content priority** | Which questions must be answered first vs moved to supplements | `request.coreQuestions` |
| 4. **Material scope & evidence standards** | Included files; period, unit, and citation rules | Registered per file in step 1 |
| 5. **Information density & length constraints** | `presentation-led` / `standalone reading` / `deep review`; `hard` vs `reference` limit, and whether supplements count | `request.length`, `request.lengthSpec`, `request.alignment.density` |
| 6. **Visual style & references** | Template, brand guide, or concrete palette, type hierarchy, whitespace, chart rules | `request.alignment.visual` (abstract terms translated to verifiable rules) |
| 7. **Navigation & lookup tasks** | Support for overview comprehension, section-topic lookup, and bidirectional conclusion <-> evidence jumps | `request.alignment.navigation` |
| 8. **Runtime environment & delivery format** | Standalone HTML and AE review viewer (`1280×720` Shadow DOM scaling), plus extra formats | `request.alignment.environment` |
| 9. **Speaker script & initial review focus** | Whether a script is wanted (`yes` / `no`) and what reviewers should check first | Decides step 3; recorded in `request.script` / `alignment.script` |

### Q&A alignment and source tracking rules

- **Reuse and delegation first**: reuse explicit requirements, traceable references (templates, brand specs, self-contained material structure), or explicit delegations ("let the Agent decide") without re-asking; record the basis (`reused` / `delegated` + `basis`). If all nine have clear evidence or delegations, ask nothing.
- **Must call an interactive Q&A tool when preferences are missing**: whenever density, visual style, navigation, script choice, or other output-affecting items lack clear reusable evidence or delegation, **call the host's available Q&A tool** before drafting (see [Agent mechanisms](agent-mechanisms.en.md)). Bundle missing items into **one round** (grouped within the host's per-call limits), giving user-friendly choices, a **recommended option with a short reason**, and free-text space.
- **Record sources and status separately**: record `density`, `visual`, and `navigation` in `request.alignment` with `value`, `source` (`user` / `reused` / `delegated` / `default` / `inferred`), and `status` (`confirmed` / `pending` / `conflict` / `deviated`). **Inferred preferences must never be marked as `user`** — label them `inferred` or `default` with a short `reason`; `delegated` entries must state the chosen plan in `basis`.
- **When no Q&A tool is available**: state the specific reason first, then ask in plain text, and record `available: false` plus non-empty `unavailableReason` in `request.qa`.
- **Async Q&A wait and non-blocking default**: while waiting for a reply, run independent prep work (e.g. material reading registration) and give the user a reasonable reply window; if still unanswered, preference items do **not** block drafting — pick explicit defaults tailored to the materials and goal, mark them `source: "default"`, `status: "pending"`, generate continuously, and surface `pending` defaults prominently in the summary and review checklist.
- **Detect internal conflicts and keep unresolved items incomplete**: when requirements contradict internally (e.g. heavy multi-table detail vs strict low hard page cap, or minimal whitespace vs packed single-page comparison), flag the tradeoff to the user. Keep any **unresolved conflict** as `status: "conflict"` and leave `content.requirements` incomplete (never `pass` or fully satisfied).
- **Continuous generation and optional inner-page calibration**: once aligned, generate continuously with **no outline approval or visual mock-up approval** gate. Trigger optional **representative inner-page calibration** (one typical body page and one typical chart page first) only when the user explicitly requests sample confirmation before expanding all pages.

## 1. Read the material and register coverage

- Register each file: `read` (fully read), `partial` (partially read, say which part), `unreadable` (could not read, say why).
- **A format name is not proof of reading**: scanned PDF pages without OCR, hidden spreadsheet columns not extracted — record the coverage you actually got.
- When reading tools are missing or insufficient, disclose it; never fabricate facts from missing content.

### Reading orchestration and capability limits

- Prefer the reading capability the host actually has; do not add third-party dependencies or online conversion services just to read. The success path needs the host to: **PDF** — extract the text layer keeping page numbers; **Word** (.docx etc.) — read body paragraphs and tables; **spreadsheets** (.xlsx / .csv) — read worksheets and cells; for formulas, note whether you got the computed result or the formula text.
- Keep locators through the pipeline: when extracting a fact, record **which PDF page, which Word paragraph, which worksheet and cells** it came from; write them into the report's `.ae-source` marks and the record's `sources[].locations` and `findings[].locations` so reviewers can check the original.
- In spreadsheets, **blank, missing, numeric zero, unit, and period** are different things: a blank cell is not 0, a missing value is not "nonexistent", 0 is not missing, and units and periods travel with the number. Unread formulas, charts, hidden rows/columns, or protected regions go into `gaps` when they affect conclusions.

### Registering insufficient capability

- Missing reading tool: `unreadable`, with `reason` naming the missing capability (e.g. "host has no PDF text extraction").
- Partial extraction: `partial`, with `coverage` stating both the range read and the range missed (e.g. "pages 1–8 read; pages 9–12 are scanned and unrecognized") and `reason` saying why. "The file opened" is not a registration.
- **All substantive materials unreadable**: stop, state the material gaps and what would let work continue (readable formats, an installed reader, or text versions); do not produce an empty shell or fabricated report and call the draft complete — the check entry reports `record.reading-blocked` and the draft does not count as complete.

## 2. Organize the report as page-per-section HTML

- Organize sections by purpose; the report must answer the user's core questions, and anything it cannot answer is listed as a gap.
- **Apply the aligned information density**:
  - `presentation-led`: one core takeaway per page with lean, large-type bullets; place supporting detail in speaker notes or marked supplement pages;
  - `standalone reading`: self-contained pages with full subject-predicate conclusions, context, definitions, and `.ae-source` so the report reads clearly without speaker notes within one viewport;
  - `deep review`: structured evidence comparisons, edge conditions, and calculation derivations organized in zoned cards and clear hierarchy (`.ae-cols-2`, `.ae-card`, `.ae-supplement` in `report-base.css`), readable without overflow.
  - **Keep standalone readability with or without a script**; mark supplementary pages with **`data-kind="supplement"`** on `<section class="page">`, and match `request.lengthSpec` (`hard` vs `reference` nature and `includeSupplements`).
- **Enforce concrete visual agreements**: extract verifiable visual rules (color variables, type scale, grid columns, whitespace baseline, chart and image style) from user templates/brand specs/references, translating abstract terms ("professional", "clean") into checkable rules; record explicit defaults when delegated to the Agent.
- **Design navigation around the three reader tasks**: (1) summary plus TOC (`.toc-row` with `data-range-start`/`data-range-end`, `.toc-label`/`.toc-title`) for **overview comprehension**; (2) recognizable section labels and page headings for **topic lookup**; (3) **bidirectional round-trip jumping (`conclusion -> evidence or supplement -> back to corresponding analysis`)** via `<a href="#page-<key>">` at claims and explicit return links on evidence/supplement pages (with `data-page-ref` and the viewer's visited-page back button as supplements). Keep anchors bound to stable `data-key` values so reordering and `--rebuild` preserve accuracy.
- Follow the review page convention: `<section class="page" data-page="N" data-key="key">`, page numbers continuous from 1, and **`data-key` must be a stable, unique semantic key** (like `sum`, `rev`, `sup-method`) — not `p3`-style page keys; re-annotation and cross-page links across rebuilds depend on them.
- Layout is designed for 1280×720. **The base style ships with the skill**: inline the contents of `references/report-base.css` from the installed skill copy into the report's `<style>` (the review page collects styles into a Shadow DOM; `$S` is the skill copy's `scripts/`, the style file sits one level up in `references/`). Adjust variables to taste, but keep the semantic classes `.ae-source`, `.ae-gap`, `.ae-todo`.
- Keep evidence next to key claims, figures, and impact judgments (`.ae-source` names the source location); mark unverified content with `.ae-todo`; write reading gaps as `.ae-gap` blocks or a dedicated gaps page.
- Numbers keep their dates, units, definitions, and calculation basis; computed results must be reproducible from the material, and the four definition elements (unit, period, definition, input locations) go into the record's `calculation` (see step 4).
- Give the report a meaningful `<title>`.

## 3. Speaker script (only when requested)

- Chinese pages use `### 第 N 页｜标题` with **时长 / 讲法 / 过渡 / 可能被问**; English uses `### Page N｜Title`; field details in [formats](formats.en.md).
- Page numbers must match the report order one-to-one; when no script was requested, do not fabricate an empty one to satisfy a check.

## 4. Write the verification record (`ae-report-record/1`)

Save `report-record.json` next to the report (same directory, i.e. beside the material) — a versioned, language-neutral structured record:

```json
{
  "schema": "ae-report-record/1",
  "generatedAt": "2026-11-02T09:00:00+08:00",
  "summary": "New stores drove third-quarter revenue growth, while delivery mix and higher ingredient costs reduced margins. The fourth quarter should prioritize productivity at existing stores.",
  "request": {"purpose": "Quarterly business review", "audience": "Management",
              "coreQuestions": ["Why did revenue grow"], "length": "6 pages", "script": "yes",
              "lengthSpec": {"nature": "hard", "includeSupplements": false},
              "qa": {"used": true, "tool": "ask_question",
                     "asked": ["density", "visual", "navigation"], "unanswered": []},
              "alignment": {
                "density": {"value": "standalone", "source": "user", "status": "confirmed"},
                "visual": {"value": "report-base readable layout (22px body, 64x72px padding)",
                           "source": "delegated", "status": "confirmed",
                           "basis": "User delegated visual choices to Agent; applied base readable variables"},
                "navigation": {"value": "summary-toc + conclusion-to-evidence",
                               "source": "reused", "status": "confirmed",
                               "reason": "Reused management QBR three-task navigation pattern"}}},
  "sources": [{"path": "materials/q3.md", "status": "read", "coverage": "full text",
               "sha256": "<sha256 of the material file>",
               "locations": ["q3.md paragraphs 1–3"], "note": ""},
              {"path": "materials/q3-scan.pdf", "status": "partial",
               "coverage": "text layer of pages 1–3 read; pages 4–5 are scanned and not recognized",
               "locations": ["q3-scan.pdf pages 1–3"], "reason": "no OCR capability on this host",
               "sha256": "<sha256 of the material file>"}],
  "method": {"kind": "builtin", "name": "annotation-everything default paradigm",
             "version": "0.9.0", "unavailable": []},
  "links": {"report": "report.html", "script": "script.md",
            "sha256": {"report": "<sha256 of report>", "script": "<sha256 of script>"},
            "sourceNote": "where the material lives"},
  "findings": [{"claim": "Revenue grew 12% year on year", "type": "calculation",
                "sources": ["materials/q3.md"], "locations": ["q3.md paragraph 2"], "pages": ["sum"],
                "refs": [{"path": "materials/q3.md", "locator": "§1 Revenue",
                          "quote": "100 in the same quarter last year", "found": true}],
                "support": "supported",
                "basis": "Compared both revenue figures in §1 and recomputed; no conflicting definition found",
                "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                "expected": 12,
                                "unit": "k CNY", "period": "2025Q3 vs 2024Q3",
                                "definition": "percentage growth versus the same quarter last year",
                                "inputLocations": {"a": "materials/q3.md §1: 100 last year",
                                                   "b": "materials/q3.md §1: 112 this quarter"}},
                "note": "Definition: versus the same quarter last year"}],
  "gaps": ["October weekly reports missing"],
  "checks": [{"id": "content.requirements", "status": "not_run",
              "scope": "all requirements", "note": ""},
             {"id": "content.claims", "status": "not_run",
              "scope": "all key claims", "note": ""},
             {"id": "content.fact-vs-inference", "status": "not_run",
              "scope": "all claim pages", "note": ""},
             {"id": "content.units-dates", "status": "not_run",
              "scope": "all numbers", "note": ""},
             {"id": "content.numbers-charts", "status": "not_run",
              "scope": "numbers and charts", "note": ""},
             {"id": "content.cross-page", "status": "not_run",
              "scope": "all pages", "note": ""},
             {"id": "content.script-consistency", "status": "not_run",
              "scope": "report and script", "note": ""},
             {"id": "content.open-questions", "status": "not_run",
              "scope": "openItems", "note": ""},
             {"id": "structure.flow", "status": "not_run",
              "scope": "all pages", "note": ""},
             {"id": "structure.headings", "status": "not_run",
              "scope": "all pages", "note": ""},
             {"id": "structure.claim-support", "status": "not_run",
              "scope": "all claim pages", "note": ""},
             {"id": "structure.dup-gaps", "status": "not_run",
              "scope": "all pages", "note": ""},
             {"id": "structure.length", "status": "not_run",
              "scope": "length and script duration", "note": ""},
             {"id": "visual.pages", "status": "not_run",
              "scope": "all 6 pages", "note": ""}],
  "openItems": [{"item": "price increase: pick one of two options", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

Field semantics:

- `summary`: write one or two sentences for every newly generated report, describing its content and the problem it addresses so readers can identify it in the review overview. The value must be a string; older records may omit it, and the schema remains `ae-report-record/1`. The overview also shows `request.purpose`, `request.audience`, and `request.coreQuestions`; without a summary it shows “(No summary)” and the cover-page title.
- `request`: the clarified request from step 0, including what was asked and what was reused. Optional alignment extensions keep `ae-report-record/1` backward compatible (older records omitting them remain valid and report unrecorded preferences):
  - `request.alignment`: per-dimension records (`density`, `visual`, `navigation`, etc.) with `value`, `source` (`user` / `reused` / `delegated` / `default` / `inferred`), `status` (`confirmed` / `pending` / `conflict` / `deviated`), and `basis` / `reason`.
  - `request.lengthSpec`: `{"nature": "hard" | "reference", "includeSupplements": true | false, "pages"?: "..."}`, declaring hard limit vs reference target and whether `data-kind="supplement"` pages count. A single page count under `hard` (such as `6 pages` or `at most 6 pages`) is an upper bound; an explicit range (such as `4–6 pages`) retains both bounds. Legacy records without `lengthSpec` retain exact-count or range comparisons.
  - `request.qa`: `{"used": bool, "available"?: bool, "tool"?: str, "unavailableReason"?: str, "asked": [...], "unanswered": [...]}`. Every `unanswered` item remains pending and blocks delivery, even without a corresponding `alignment` dimension; remove resolved items and update dimension statuses when answers arrive.
- `deviations` (optional): `[{"item": "...", "reason": "...", "impact"?: "..."}]`, honestly recording items that could not follow aligned constraints instead of claiming full compliance.
- `sources`: the reading registrations from step 1; `status` is `read` / `partial` / `unreadable`; `coverage` states the range actually read, and `partial` must state both the read and unread parts; `partial` / `unreadable` must include `reason`; **`path` is resolved relative to the directory containing the report** — the check entry looks material files up next to the report to verify hashes; `locations` lists locators (PDF pages, Word paragraphs, worksheets and cells); `sha256` is the material content hash (`shasum -a 256` or equivalent) — the check entry uses it to detect later material changes, and a changed material invalidates old verification results. **When all substantive materials are `unreadable`, the draft does not count as complete**; the check entry fails `record.reading`.
- `method`: what actually generated the output; `kind` is the closed enum `builtin` / `external`, `name` records the method actually used, and `version` any version information available. An external skill must carry `invocation` (how it was invoked and where the artifacts came from — a scanned candidate or a mentioned name is not an invocation; without it the check entry fails `record.method`); a user-specified method that was NOT the one used goes into `requested` with `note` (the reason); capability gaps go into `unavailable`. The check entry verifies these declarations and surfaces the method information in the summary and the structured result. External example: `{"kind": "external", "name": "synthetic-brief-kit", "version": "1.4.2", "invocation": "ran its scripts/make_report.py to generate", "unavailable": []}`.
- `links`: artifact association and sha256 (via `shasum -a 256` or equivalent); the check entry uses this to decide whether the record matches the current artifacts.
- `findings`: key claims (including figures and impact judgments) mapped to sources and report pages; `locations` may list locators inside the material (e.g. `q3.pdf page 2`, `Sheet1!B3`) so reviewers can check the original; `refs` give the structured locator: `path` uses the same basis as `sources[].path` (relative to the report's directory, so reviewers can locate the material), `quote` keeps the material's original wording (never translated or paraphrased), `locator` names the position (section, line, table), and `found` honestly records whether it was located; `support` records whether the material supports the claim — `supported` / `partial` (the claim is stronger than the material) / `unsupported` / `unverified`. **A citation existing does not mean the claim is supported; if the check says otherwise, mark `unsupported`.** `basis` records how the check was made and which parts of the material were consulted. Calculations with declared inputs and definitions include `calculation` (operations: `sum`, `difference`, `product`, `ratio`, `percent-change`) plus the four definition elements: `unit`, `period`, `definition`, and `inputLocations` covering every input key; calculations missing elements are marked **pending verification** by the check entry, and mismatches are located to the claim and report page.
- `gaps`: material and requirement gaps. `openItems`: items waiting on the user; `blocking: true` when they affect the core conclusions.
- `checks`: per-item declarations of content (`content.*`, the eight-item checklist below), structure (`structure.*`, the five-item checklist below), and visual (`visual.pages`) verification, with status `pass` / `fail` / `not_run` / `unavailable`; each entry carries `id` and `status` (plus `scope` / `note`), the content / structure / visual categories are derived by the check entry from the built-in checklist ids, and a category field on the entry itself plays no part in the verdict. **Anything not executed is `not_run`, never pass.** Reaching deliverable requires every item of all three checklists to be `pass`, and the visual item must carry page-by-page evidence covering every report page (see "Visual verification" below). Without a script, the two script-scoped items (`content.script-consistency`, `structure.length`) do not apply and need not be declared; the check entry excludes them from the coverage totals — declaring them `pass` anyway is a false checked claim and fails the program checks. Missing claim support, pending calculations, or unhashed materials downgrade the content and structure coverage so they are not shown as passed.
- `verdict.draftComplete` is `true` when the structure is complete and reviewable; `verdict.deliverable` is `true` only when content, structure, and visual verification all passed with no blocking items — otherwise leave `null` or `false`. **Draft complete is not the same as deliverable.**
- `revision` (optional, written when maintaining after a revision): the declaration of this revision's verification updates. `basis` states how the affected scope was determined (which pages, which sources, whether script and cross-page references are involved); `rechecked` lists the check ids verified this time, each of which must have an actual result (`pass` / `fail`) in `checks`; `pending` lists the items whose affected scope could not be confirmed, each with `scope` and `reason` — the check entry keeps any record with `pending` entries from reaching deliverable and lists the pending scope in the summary. Records without `revision` (first generation, existing reports) are unaffected. See "Maintaining verification info after a revision" in the [English workflow](workflow.en.md).

### Content verification checklist (`content.*`, eight items: check each one, write scope / note for each)

| id | what to check | what note records |
|---|---|---|
| `content.requirements` | every requirement and core question is answered, or explicitly listed as a gap | which requirements were checked, which are missing |
| `content.claims` | key claims have evidence and a support level: every finding carries refs, support, basis | claim count, and the unsupported / partial ones with reasons |
| `content.fact-vs-inference` | facts and inferences stay separate; projections must not read as confirmed facts | phrasings that were reworded |
| `content.units-dates` | dates, units, definitions, and periods checked one by one (August is not October; k CNY is not CNY) | confusions found and fixes |
| `content.numbers-charts` | numbers and charts agree; chart data is traceable to the material | chart list and data sources |
| `content.cross-page` | numbers and conclusions agree across pages; no contradictory definitions | conflicting pages and resolutions |
| `content.script-consistency` | report and script tell the same story page by page, with the same numbers | where both sides were changed together |
| `content.open-questions` | unresolved questions and their impact on conclusions are recorded (openItems, blocking) | why items are blocking |

- Every item needs an actual checking action; `note` records what was checked and what was found. A blanket "checked" does not count.
- Typical counterexamples: **a citation exists but the claim is not supported** (the material says the East region grew; the report claims nationwide growth) must be marked `support: "unsupported"` and fails content verification; missing evidence (a claim without refs / basis) is treated as pending verification; date or unit confusions and over-inference are failures — fix the report first, then re-run the check entry.
- Judgment items keep their evidence and checking notes; you perform these eight checks — the check entry only verifies record declarations and deterministic rules, it cannot prove your extraction is accurate for you.

### Structure verification checklist (`structure.*`, five items: check each one, write scope / note for each)

| id | what to check | what note records |
|---|---|---|
| `structure.flow` | narrative order and sections answer the core questions: the answers come first, setup stays lean, the whole reads as one line | page order and the reasoning behind it |
| `structure.headings` | every page title matches that page's content; no overclaiming, no drift | titles rewritten and why |
| `structure.claim-support` | claim pages have adjacent evidence (`.ae-source`, data, or charts); conclusions never stand alone | pages missing support and the fix |
| `structure.dup-gaps` | duplication and gaps that hurt comprehension: merge repeated numbers or paragraphs, fill missing key information | what was merged or added |
| `structure.length` | length or duration matches `request.length`; with a script, check the duration page by page | actual pages / duration versus the constraint |

- Applicability rule: **without a script**, `content.script-consistency` and `structure.length` do not apply — they need not be declared and the check entry excludes them from the coverage totals; declaring them `pass` is a false checked claim and fails the program checks. The length constraint itself is still compared directly against the report page count when `request.length` states one (`record.length`), and a parseable mismatch is an explicit error.

### Visual verification (`visual.pages`: render every page, evidence ships with the declaration)

- Inspect every page under **actual rendering** covering both standalone HTML report display and AE's `1280×720` Shadow DOM review scaling, checking five problem classes per page: **overflow** (content beyond the canvas), **occlusion** (elements overlapping), **text and chart readability** (font size, contrast, legibility of text/tables/legends), **fonts** (rendered with the base style), and **missing content** (blank pages, dropped paragraphs, charts without data).
- Write the results into the `pages` evidence of the `visual.pages` declaration, one entry per page:

```json
{"id": "visual.pages", "status": "pass",
 "scope": "all 3 pages (rendered in report & AE 1280×720 Shadow DOM)", "note": "page-by-page render check",
 "pages": [{"page": 1, "checked": true, "issues": [], "viewport": "1280x720"},
           {"page": 2, "checked": true, "issues": []},
           {"page": 3, "checked": true, "issues": []}]}
```

- Declaring `pass` requires evidence covering **every report page**: missing pages, a page with `checked: false`, or declaring pass while unresolved issues remain are all rejected by the check entry (`record.visual`).
- When pages cannot be rendered or viewing capability is missing, declare `unavailable` (`note` says what is missing); when a check is interrupted, record the interrupted/affected pages honestly as `checked: false` and set the overall status to `not_run`; record found problems in `issues` and set the status to `fail` — a passing program check never substitutes for a passing visual check, and unresolved issues block the deliverable verdict.
- Counterexamples must fail: **declaring a page-by-page pass while the record misses page coverage**, or **declaring pass with an unresolved overflow**, cannot pass the overall deliverable acceptance.

## 5. Run the public check entry

```bash
python3 $S/report_check.py report.html --script script.md --record report-record.json --locale en
```

- `--json` emits the `ae-report-check/1` structured result; exit code 0 means all program checks passed (**not deliverable**), 1 means some check failed, 2 means bad input or usage.
- Program checks cover: report parses, page keys unique, script matches page-for-page, record structure and version, record matches the current artifacts, referenced pages exist, calculations reproduce, check set complete, reading registration and gap disclosure, claim support mapping (support consistent with refs, no unsupported claims), material association (registration mapping and sha256 freshness), visual evidence consistency (a declared page-by-page pass requires evidence covering every report page), the length constraint (`record.length`, including `request.lengthSpec` hard vs reference targets and `data-kind="supplement"` counting), the revision declaration (`revision.rechecked` consistent with the record's check results, `pending` fully disclosed), alignment/Q&A/deviations validation (`record.alignment`, checking sources, delegation basis, Q&A unavailability reasons, pending/conflicting items, and deviations), and cross-page navigation (`report.navigation`, verifying `#page-<key>`, `data-page-ref`, and `data-range-start`/`data-range-end`). The program does not execute code from the material, does not render pages, and does not infer business definitions. The summary shows reading totals, unread ranges, disclosed gaps, alignment pending/conflict/deviation details (or unrecorded preferences on legacy records), and checked scope; maintained records also list rechecked and pending scope.
- Fix whatever `fail`s and re-run; the summary's content / structure / visual coverage comes from the record, and not-run is never shown as passed. Missing claim support, pending calculations, unhashed materials, or changed materials downgrade the coverage and say why.
- When reporting to the user, quote this summary: draft status, coverage, gaps and open items, and the record file location. **Do not claim the report is deliverable before the record supports it.**

## 6. Enter review

All artifacts (report, script, record, review directory) live beside the material:

```bash
python3 $S/build_viewer.py --report report.html --script script.md --out review-dir --name "Review name"
python3 $S/serve.py review-dir
```

Waiting, annotating, revision, and rebuilds continue from section 2 of the [English workflow](workflow.en.md). Existing reports without a verification record go straight into review; no need to backfill one.

## Boundaries

- No model service is embedded; generation is done by you. The page-by-page visual check is performed by you (or a browser in tests); the check entry only applies deterministic rules.
- **Draft complete is not the same as deliverable**: content, structure, and visual verification that has not run is recorded as `not_run`, and the report must not be called deliverable early; a passing program check does not mean deliverable either.
- Verification records and artifacts stay in the controlled material location — never in the repository or the skill copy.
