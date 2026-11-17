# PPTX extra deliverable: built-in production and verification (English)

[中文](generation-pptx.zh-CN.md) · [Report generation default paradigm](generation.en.md) · [English workflow](workflow.en.md)

When the user explicitly asks for a presentation at generation time, AE ships the production flow, helper tools, artifact conventions, and deterministic checks with the skill; you (the host agent) do the generation reasoning, content judgment, and page-by-page viewing. The success path never depends on installing another presentation skill. The web report still follows the [default paradigm](generation.en.md) for generation, verification, and review entry; the PPTX is a registered extra deliverable and never replaces web acceptance. Without a presentation request, keep the existing flow and add no new dependencies.

Pre-generation alignment — purpose, audience, length, script choice, and visual alignment — still follows step 0 of the default paradigm. **Confirmed brand colors, fonts, and layout take priority** and are never overridden by the example palette in this guide.

## Responsibility boundaries

- AE provides: `scripts/pptx_build.py` (builder, standard library only), `scripts/pptx_check.py` (file-level checks), `scripts/pptx_render.py` (render helper), the deck spec contract, record fields, and the public verdict (`record.deliverables` in `report_check.py`).
- You provide: content organization (sections, page order, density), writing the deck spec, viewing every page, content-consistency checking, and the record and summary.
- The review boundary is unchanged: producing a new deck does not extend the annotation write-back whitelist; images, charts, SmartArt, group interiors, and media still cannot be written back automatically.

## Optional tools

| Tool | Role | Platforms | Verified versions | Detection | Install |
|---|---|---|---|---|---|
| Python 3 (stdlib) | Build and checks | macOS / Linux / Windows | Ships with the host | `python3 --version` | — |
| LibreOffice (`soffice`) | Render page images | macOS / Linux / Windows | 26.8 (macOS arm64) verified; others unverified | `pptx_render.py` probes PATH and the macOS install location; override with `AE_SOFFICE` | `brew install --cask libreoffice` (macOS), `apt install libreoffice` (Debian/Ubuntu) |
| poppler (`pdftoppm`) | PDF to page PNGs | macOS / Linux / Windows | 26.08 (macOS arm64) verified; others unverified | `pptx_render.py` probes PATH; override with `AE_PDFTOPPM` | `brew install poppler`, `apt install poppler-utils` |

Building needs no third-party libraries. When render tools are missing or conversion fails, `pptx_render.py` reports `unavailable` / `failed` with a specific diagnosis; register it under "Statuses and failure handling" below, and basic HTML review stays available. The preferred verification environments for rendering are macOS and Linux; Windows is not machine-verified — when you verify it in a real task, record the version in the `render` field.

## The deck spec (`ae-pptx-deck/1`)

The builder consumes one JSON file. Page content comes from the confirmed content organization; semantic keys are shared with the HTML report's page keys.

```json
{
  "schema": "ae-pptx-deck/1",
  "title": "Deck title",
  "theme": {
    "colors": {"dk1": "1F2933", "lt1": "FFFFFF", "dk2": "3E4C59", "lt2": "F5F7FA",
               "accent1": "2B6CB0", "accent2": "...", "accent3": "...", "accent4": "...",
               "accent5": "...", "accent6": "...", "hlink": "...", "folHlink": "..."},
    "fonts": {"major": {"latin": "Display Latin", "ea": "標題字體"},
              "minor": {"latin": "Text Latin", "ea": "正文字體"}}
  },
  "size": {"width": 12192000, "height": 6858000},
  "master": {"footer": true, "footerText": "Fixed footer text", "pageNumber": true,
             "logo": {"path": "logo.png", "name": "Logo"}},
  "sections": [{"name": "Opening", "keys": ["cover", "sum"]}],
  "slides": [
    {"key": "cover", "layout": "title", "title": "Cover title", "subtitle": "Subtitle",
     "hideMasterShapes": false},
    {"key": "sum", "layout": "titleAndContent", "title": "Key points",
     "body": ["First point", {"text": "Second level", "level": 1, "bold": false}],
     "shapes": [{"shape": "rect", "x": 800000, "y": 5600000, "cx": 3000000, "cy": 600000,
                 "fill": "accent2", "text": "One-line takeaway"}],
     "charts": [{"chart": "column", "name": "Revenue", "x": 600000, "y": 1800000,
                 "cx": 6000000, "cy": 3600000,
                 "categories": ["Q1", "Q2"],
                 "series": [{"name": "Revenue", "values": [100, 112]}],
                 "legend": "bottom", "title": "Revenue"}],
     "notes": "Speaker notes for this slide"}
  ]
}
```

Field rules:

- **Theme**: all 12 color slots are required (fix the theme before writing slides). `major` is the title font, `minor` the body font; each needs a Latin name and an East-Asian name. Controlled elements — titles, body, plain shapes, chart series — reference theme slots only; the builder never writes fixed colors, except for fixed-color assets like brand logos. The builder writes a correct theme directly; no post-fix is needed. A theme-swap check should only change the theme part.
- **Layouts** (named): `title` (cover, title + subtitle), `sectionHeader`, `titleAndContent`, `titleOnly` (the usual home for charts and free shapes), `blank`. Footer, page numbers, and the logo live on the master and appear on every page; a cover can hide master shapes with `hideMasterShapes: true`.
- **Charts**: the first version supports `column`, `bar`, `line`, and `pie` — all native PowerPoint charts with an embedded editable data workbook. Categories and each series' values must have equal length; only `pie` accepts `labels: "percent"`. Combo, dual-axis, and 3-D charts are out of scope; report unsupported types as gaps and **never fake a native chart with a picture**.
- **Notes**: speaker notes go into the matching slide's notes part; omit the field when there is no script, and never fabricate empty notes to pass checks. Notes map to the actual slide — when pagination differs from the HTML script, do not copy HTML script page numbers.
- **Sections**: every slide belongs to exactly one section; the builder rejects missing pages and duplicate grouping before writing.
- **Geometry**: `x / y / cx / cy` are EMUs (1 inch = 914400); shapes and charts must stay inside the canvas. Prefer the `titleOnly` layout for chart pages and never let a chart overlap the body placeholder.

## Production and verification steps

1. **Alignment reuses step 0 of the default paradigm**: confirm purpose, audience, length, script, and visual alignment first; without a presentation request, do not enter this flow.
2. **Write the deck spec**: page by page from the confirmed content organization; use the confirmed brand colors and fonts. Numbers, units, periods, and source annotations share one source with the HTML report — never write two versions.
3. **Build**: `python3 $S/pptx_build.py --deck deck.json --out deck.pptx --json`. The file is written only after the whole spec validates; fix the spec on exit code 2. Building is deterministic — the same spec yields identical bytes; `--json` prints the `ae-pptx-build/1` build summary (file name, sha256, pages, title).
4. **File-level checks**: `python3 $S/pptx_check.py deck.pptx --json`. Covers required parts and relationship targets, content types, layout references and names, placeholder usage, key structure of all four chart kinds (axis references, label positions, data caches, embedded workbook, theme-referenced colors), theme parts, and section coverage. **A ZIP that opens and XML that parses is not a pass**; failures name the check, part path, locatable slide, and reason; the result schema is `ae-pptx-check/1`.
5. **Render and view every page**: `python3 $S/pptx_render.py deck.pptx --out-dir pages/ --json` produces page PNGs and the `ae-pptx-render/1` render manifest (tool versions, page hashes, source hash). Then **actually view every page** for text overflow or truncation, unexpected occlusion, elements outside the canvas, missing charts or body content, and font readability. Intentional stacking and reading-impacting occlusion are judged separately; geometry detection and thumbnails only assist — generating images or running detections is not page viewing.
6. **Fix and re-check**: fix the spec or styles, rebuild, rerun file checks, and view the **affected pages**; theme, font, or shared-layout changes require checking every affected page, or the whole deck when the range is unclear. Reusing old render evidence for unchanged pages requires a statement that the page's content and rendering were untouched and the file hash is unchanged.
7. **Content consistency**: check key conclusions, numbers, units, periods, sources, and unverified items across HTML and PPTX. Pagination and detail may differ, but every finding maps to the slide(s) carrying it. The verification entry extracts slide text and chart data from the mapped pages — **a key number absent from its mapped pages fails the check**, so a synthetic cross-format number mismatch cannot pass.
8. **Record**: write the evidence into `deliverables[]` in `report-record.json` (next section), then run the public verification entry (`report_check.py`); HTML and PPTX get separate results.

## Record fields (`deliverables[]` in `ae-report-record/1`)

A backward-compatible optional extension; records without the field keep the old rules and are never misread as checked or failed.

```json
{"kind": "pptx", "required": true, "file": "deck.pptx", "sha256": "<final file sha256>",
 "pages": 12,
 "build": {"tool": "annotation-everything built-in pptx_build.py", "version": "<AE version>",
           "invocation": "python3 $S/pptx_build.py --deck deck.json --out deck.pptx",
           "deckSpec": "deck.json", "deckSpecSha256": "<deck spec sha256>"},
 "render": {"tool": "LibreOffice soffice", "version": "26.8", "notes": "pdftoppm 26.08"},
 "checks": {
   "file": {"id": "pptx.file", "status": "pass", "note": "pptx_check all passed", "issues": []},
   "visual": {"id": "pptx.visual", "status": "pass", "note": "every page viewed",
              "pages": [{"page": 1, "image": "pages/page-01.png", "sha256": "<page sha256>",
                         "checked": true, "issues": []}]},
   "consistency": {"id": "pptx.consistency", "status": "pass", "note": "numbers checked per claim",
                   "findings": [{"claim": "Revenue up 12%", "pages": [2]}]}},
 "powerpoint": {"status": "not_run", "note": "No PowerPoint locally; pending PR review"},
 "openIssues": [], "note": ""}
```

- `required`: `true` when the user explicitly requested this deliverable. A required-but-unfinished PPTX keeps the whole-request gap in the summary without changing the HTML verdict; "HTML deliverable, PPTX unfinished" can both hold.
- `build`: when HTML used the built-in paradigm and PPTX another tool, record both separately — one umbrella method name never covers two artifacts. Missing `tool` or `invocation` fails the check; a skill name or a self-description is not invocation evidence.
- `checks.visual.pages[]`: per-page evidence is bound to the checked file version — each page carries the image path and sha256. Missing pages, `checked: false`, missing images, hash mismatch, or pass with open issues all fail.
- `checks.consistency.findings[]`: every finding maps to slide page numbers; key numbers from the claim text and the calculation's expected value must appear in the mapped slides' text or chart data.
- `powerpoint`: `pass` (with `os`, `version`, optionally `build`) / `fail` / `not_run` / `unavailable`. Unverified files must disclose that; never claim PowerPoint testing or universal Office/WPS compatibility.
- `openIssues`: non-empty while declaring pass fails the check.
- Each PPTX is recorded independently; after theme fixes or chart repairs, update `sha256` and re-verify the affected range.
- Records, page images, and artifacts live beside the material in its controlled location — never in the repo or the skill copy.

## Statuses and failure handling

| Situation | PPTX result and delivery note |
|---|---|
| Missing build tool, build failure, or no valid artifact | Not finished; write `{"file": null, "status": "unavailable", "note": "cause", "resume": "resume condition"}`; when the user required it, the whole request cannot claim completion |
| Valid file exists but rendering, viewing, or some pages are missing | A draft may remain; register the check as `unavailable` or `not_run`; do not declare the PPTX deliverable |
| File, visual, or consistency check failed | Record `fail` with the location; pass only after fixing and re-verifying |
| All applicable checks pass with no blocking gaps | PPTX is deliverable; disclose the actual check scope and PowerPoint verification status |

## PowerPoint compatibility acceptance

The first version does not make local PowerPoint a required dependency for every generated deck; delivery is judged on file checks, per-page viewing, and content consistency, with the verification status disclosed. The **required acceptance for the feature PR** is: open the synthetic sample in at least one Microsoft PowerPoint desktop environment, record the OS, PowerPoint version and build, and confirm no repair prompt, no missing body or charts, and correct chart updates after editing native chart data. Structure checks and successful LibreOffice rendering do not substitute; when no environment is available, hand over as "pending PR review verification". When the user explicitly requires per-file testing, missing verification blocks that PPTX from being deliverable.

## Boundaries

- Do not wire any reference skill's internal marker-editing capabilities into annotation write-back, and do not require every annotation to invoke the presentation skill first.
- Do not copy the official skill wholesale, and do not auto-search or install remote skills.
- No existing-template filling, review-time page add/remove, chart data editing, or whole-deck restyling (review write-back and later capabilities).
- No coverage of arbitrary OOXML errors, all chart types, or all Office/WPS versions; file checks are the minimum for this issue's production scope.
- No new model services; generation and viewing inference never run inside the local review service.
