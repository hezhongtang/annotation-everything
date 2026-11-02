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
- Numbers keep their dates, units, definitions, and calculation basis; computed results must be reproducible from the material.
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
  "sources": [{"path": "materials/q3.md", "status": "read", "coverage": "full text", "note": ""}],
  "method": {"kind": "builtin", "name": "annotation-everything default paradigm",
             "version": "0.9.0", "unavailable": []},
  "links": {"report": "report.html", "script": "script.md",
            "sha256": {"report": "<sha256 of report>", "script": "<sha256 of script>"},
            "sourceNote": "where the material lives"},
  "findings": [{"claim": "Revenue grew 12% year on year", "type": "calculation",
                "sources": ["materials/q3.md"], "pages": ["sum"],
                "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                "expected": 12},
                "note": "Definition: versus the same quarter last year"}],
  "gaps": ["October weekly reports missing"],
  "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
              "scope": "all requirements", "note": ""},
             {"id": "visual.pages", "kind": "visual", "status": "not_run",
              "scope": "all 6 pages", "note": ""}],
  "openItems": [{"item": "price increase: pick one of two options", "blocking": false}],
  "verdict": {"draftComplete": true, "deliverable": null}
}
```

Field semantics:

- `request`: the clarified request from step 0, including what was asked and what was reused.
- `sources`: the reading registrations from step 1; `status` is `read` / `partial` / `unreadable`.
- `method`: what actually generated the output; for external skills record the name and any version information available. Scanning a candidate does not mean it was used.
- `links`: artifact association and sha256 (via `shasum -a 256` or equivalent); the check entry uses this to decide whether the record matches the current artifacts.
- `findings`: key claims mapped to sources and pages; calculations with declared inputs and definitions include `calculation` (supported: `sum`, `difference`, `product`, `ratio`, `percent-change`) and are recomputed by the check entry.
- `gaps`: material and requirement gaps. `openItems`: items waiting on the user; `blocking: true` when they affect the core conclusions.
- `checks`: per-item declarations of content (`content.*`) and visual (`visual.*`) verification, with status `pass` / `fail` / `not_run` / `unavailable`. **Anything not executed is `not_run`, never pass.** This slice requires at least `content.requirements` and `visual.pages`.
- `verdict.draftComplete` is `true` when the structure is complete and reviewable; `verdict.deliverable` is `true` only when both content and visual verification passed with no blocking items — otherwise leave `null` or `false`. **Draft complete is not the same as deliverable.**

## 5. Run the public check entry

```bash
python3 $S/report_check.py report.html --script script.md --record report-record.json --locale en
```

- `--json` emits the `ae-report-check/1` structured result; exit code 0 means all program checks passed (**not deliverable**), 1 means some check failed, 2 means bad input or usage.
- Program checks cover: report parses, page keys unique, script matches page-for-page, record structure and version, record matches the current artifacts, referenced pages exist, calculations reproduce, check set complete.
- Fix whatever `fail`s and re-run; the summary's content / visual coverage comes from the record, and not-run is never shown as passed.
- When reporting to the user, quote this summary: draft status, gaps, verification not yet executed. **Do not claim the report is deliverable before the record supports it.**

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
