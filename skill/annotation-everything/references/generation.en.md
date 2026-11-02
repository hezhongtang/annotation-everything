# Report generation: default paradigm (English)

[中文](generation.zh-CN.md) · [English workflow](workflow.en.md) · [Input and output formats](formats.en.md) · [Shared safety and protocol](protocol.md)

With only AE installed, start here to turn raw material into a reviewable draft. This is the default paradigm; when the user names another generation skill, follow it, and the artifacts still go through the same checks (step 5). Generation reasoning, material reading, and content judgment are performed by you (the Agent); AE ships the workflow, base style, artifact conventions, and deterministic checks.

## 0. Clarify the request: only ask what is missing

Six things must be clear: **purpose, audience, core questions, length or duration, material scope, and whether a script is wanted**.

- Reuse anything already said in the conversation; do not ask again.
- Ask only for missing items that affect the result; bundle them into a single round of questions, asked once.
- **If the script choice was not stated, ask first** (yes / no); it decides whether step 3 happens.
- Once the request is clear, generate continuously. There is **no outline approval or visual mock-up approval** step; unresolved items go into the record's `gaps` instead of blocking on confirmation.
- If all six have reasonable defaults (for example, the material is already a complete briefing), reuse them, note the source in the record, and ask nothing.

## 1. Read the material and register coverage

- Register each file: `read` (fully read), `partial` (partially read, say which part), `unreadable` (could not read, say why).
- **A format name is not proof of reading**: scanned PDF pages without OCR, hidden spreadsheet columns not extracted — record the coverage you actually got.
- When reading tools are missing or insufficient, disclose it; never fabricate facts from missing content.

## 2. Organize the report as page-per-section HTML

- Organize sections by purpose; the report must answer the user's core questions, and anything it cannot answer is listed as a gap.
- Follow the review page convention: `<section class="page" data-page="N" data-key="key">`, page numbers continuous from 1, and **`data-key` must be a stable, unique semantic key** (like `sum`, `rev`) — not `p3`-style page keys; re-annotation after rebuilds depends on them.
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
  "request": {"purpose": "Quarterly business review", "audience": "Management",
              "coreQuestions": ["Why did revenue grow"], "length": "6 pages", "script": "yes"},
  "sources": [{"path": "materials/q3.md", "status": "read", "coverage": "full text",
               "sha256": "<sha256 of the material file>", "note": ""}],
  "method": {"kind": "builtin", "name": "annotation-everything default paradigm",
             "version": "0.9.0", "unavailable": []},
  "links": {"report": "report.html", "script": "script.md",
            "sha256": {"report": "<sha256 of report>", "script": "<sha256 of script>"},
            "sourceNote": "where the material lives"},
  "findings": [{"claim": "Revenue grew 12% year on year", "type": "calculation",
                "sources": ["materials/q3.md"], "pages": ["sum"],
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
  "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
              "scope": "all requirements", "note": ""},
             {"id": "content.claims", "kind": "content", "status": "not_run",
              "scope": "all key claims", "note": ""},
             {"id": "content.fact-vs-inference", "kind": "content", "status": "not_run",
              "scope": "all claim pages", "note": ""},
             {"id": "content.units-dates", "kind": "content", "status": "not_run",
              "scope": "all numbers", "note": ""},
             {"id": "content.numbers-charts", "kind": "content", "status": "not_run",
              "scope": "numbers and charts", "note": ""},
             {"id": "content.cross-page", "kind": "content", "status": "not_run",
              "scope": "all pages", "note": ""},
             {"id": "content.script-consistency", "kind": "content", "status": "not_run",
              "scope": "report and script", "note": ""},
             {"id": "content.open-questions", "kind": "content", "status": "not_run",
              "scope": "openItems", "note": ""},
             {"id": "visual.pages", "kind": "visual", "status": "not_run",
              "scope": "all 6 pages", "note": ""}],
  "openItems": [{"item": "price increase: pick one of two options", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

Field semantics:

- `request`: the clarified request from step 0, including what was asked and what was reused.
- `sources`: the reading registrations from step 1; `status` is `read` / `partial` / `unreadable`; `sha256` is the material content hash (`shasum -a 256` or equivalent) — the check entry uses it to detect later material changes, and a changed material invalidates old verification results.
- `method`: what actually generated the output; for external skills record the name and any version information available. Scanning a candidate does not mean it was used.
- `links`: artifact association and sha256 (via `shasum -a 256` or equivalent); the check entry uses this to decide whether the record matches the current artifacts.
- `findings`: key claims (including figures and impact judgments) mapped to sources and report pages. `refs` locates the position inside the material: `quote` keeps the material's original wording (never translated or paraphrased), `locator` names the position (section, line, table), and `found` honestly records whether it was located; `support` records whether the material supports the claim — `supported` / `partial` (the claim is stronger than the material) / `unsupported` / `unverified`. **A citation existing does not mean the claim is supported; if the check says otherwise, mark `unsupported`.** `basis` records how the check was made and which parts of the material were consulted. Calculations with declared inputs and definitions include `calculation` (operations: `sum`, `difference`, `product`, `ratio`, `percent-change`) plus the four definition elements: `unit`, `period`, `definition`, and `inputLocations` covering every input key; calculations missing elements are marked **pending verification** by the check entry, and mismatches are located to the claim and report page.
- `gaps`: material and requirement gaps. `openItems`: items waiting on the user; `blocking: true` when they affect the core conclusions.
- `checks`: per-item declarations of content (`content.*`, the eight-item checklist below) and visual (`visual.*`) verification, with status `pass` / `fail` / `not_run` / `unavailable`. **Anything not executed is `not_run`, never pass.** Reaching deliverable requires every one of the eight content items to be `pass`; missing claim support, pending calculations, or unhashed materials downgrade the content coverage and it is not shown as passed.
- `verdict.draftComplete` is `true` when the structure is complete and reviewable; `verdict.deliverable` is `true` only when both content and visual verification passed with no blocking items — otherwise leave `null` or `false`. **Draft complete is not the same as deliverable.**

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

## 5. Run the public check entry

```bash
python3 $S/report_check.py report.html --script script.md --record report-record.json --locale en
```

- `--json` emits the `ae-report-check/1` structured result; exit code 0 means all program checks passed (**not deliverable**), 1 means some check failed, 2 means bad input or usage.
- Program checks cover: report parses, page keys unique, script matches page-for-page, record structure and version, record matches the current artifacts, referenced pages exist, calculations reproduce, check set complete, claim support mapping (support consistent with refs, no unsupported claims), and material association (registration mapping and sha256 freshness). The program does not execute code from the material and does not infer business definitions.
- Fix whatever `fail`s and re-run; the summary's content / visual coverage comes from the record, and not-run is never shown as passed. Missing claim support, pending calculations, unhashed materials, or changed materials downgrade the coverage and say why.
- When reporting to the user, quote this summary: draft status, coverage, gaps and open items, and the record file location. **Do not claim the report is deliverable before the record supports it.**

## 6. Enter review

All artifacts (report, script, record, review directory) live beside the material:

```bash
python3 $S/build_viewer.py --report report.html --script script.md --out review-dir --name "Review name"
python3 $S/serve.py review-dir
```

Waiting, annotating, revision, and rebuilds continue from section 2 of the [English workflow](workflow.en.md). Existing reports without a verification record go straight into review; no need to backfill one.

## Boundaries

- No model service is embedded; generation is done by you.
- This slice delivers "draft generation complete": content and visual verification that has not run is recorded as `not_run`, and the report must not be called deliverable early.
- Verification records and artifacts stay in the controlled material location — never in the repository or the skill copy.
