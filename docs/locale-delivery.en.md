# Chinese and English review delivery

[简体中文](locale-delivery.zh-CN.md)

Evidence for parent specification #28 and final child #36. Validated on 2026-10-31 with macOS 26.6.2 (Apple Silicon), Python 3.13.13, and Google Chrome 156.0.8078.74. The version remains 0.9.0; this work does not release or tag it. The implementation review baseline is `70bdd9c875cbbfc3d5d45ae0d175f8d56841d4b6`, after #29–#35 merged.

## Delivered behavior

- One installed `annotation-everything` copy and one bilingual discovery entry. Requests load `workflow.zh-CN.md` or `workflow.en.md` on demand, after the same `protocol.md`. No second installation or default loading of both complete workflows is required.
- Chinese and English format references, a synthetic English report, and evaluation inputs for discovery and stop conditions. The guides cover language, generation, rebuilds, waiting, skills, edits, review, completion, imports, and recovery.
- Integration fixes for missing translations: generator help, results, and resource diagnostics; annotation CLI help and import/export feedback; the page's annotation entry, import dialog, and recovery messages. Display strings do not decide object values, identity, status, or security refusals.
- Synchronized READMEs and Chinese/English screenshots. Version badges and banners still match version 0.9.0.

## Reproduce the complete product flow

Run these commands in order from the repository root. Unit tests inspect running services during installation maintenance; do not run them alongside the browser service tests.

```bash
python3 -m unittest discover tests
AE_DELIVERY_ARTIFACTS="$PWD/skill/annotation-everything-workspace/locale-delivery" \
  uv run --no-project --with playwright python tests/e2e_browser.py
AE_HOME=$(mktemp -d) python3 skill/annotation-everything/scripts/serve.py check --locale en
```

Append `--delivery-only` to the browser command to run just the installed-copy delivery scenarios. Skill copies, ledgers, sources, and services are temporary and isolated; cleanup reaps test processes and temporary directories. An explicit `AE_DELIVERY_ARTIFACTS` directory retains synthetic evidence: `evidence.json` with environment and checks, and per-scenario `commands.json`, source files before/after edits, `notes.json`, `handoff.json`, offline Markdown/JSON, and screenshots. This ignored directory is outside the installed skill's source.

| Scenario | Interface / material | Behavior verified |
|---|---|---|
| `english` | English / English | Generate through installed scripts; browser annotations and a real submission wake `wait`; edit both sources, rebuild, and stop after the initial edit; review only after an explicit browser request, then check processing counts |
| `chinese` | Chinese / Chinese | The same installation completes generation, submission, source edits, rebuild, and review while preserving the Chinese path |
| `mixed` | English / Chinese | English controls and handoff instructions; original material, annotations, messages, and explanations; unchanged identities and Chinese v1 object values |
| File mode for all three | As above | Open the same HTML without a service, retain annotations after reload, download real Markdown and JSON files, and import across languages without creating a server submission |

English scenarios also preserve an import draft through closing the dialog, switching language, and reopening it. Cross-review confirmations use English, cancellation does not import, and a malicious attribute in the review name does not execute. The scenarios exercise a wide delivery viewport and file-mode controls at a narrower width.

## English agent demonstration

`evals/english-apply-and-stop/` contains independent synthetic material. Set `AE_SKILL_DIR` to the temporary installed skill and `AE_HOME` to a temporary ledger. Run its `place-files.sh` in an empty evaluation directory, then give `prompt.md` to the agent. Installation still uses one `ae.py install` entry, without a language-specific copy.

In this session, the current agent read the installed entry, shared protocol, and English workflow, accepted an English request, and checked all six report and speaker-note pages before editing the sources. No subagent handled this batch. The page 3 KPI became `YoY +12%`; its speaker notes already specified year-on-year growth. Both sources on page 6 now evaluate the price change. Supplier choice remained open because quality evidence was missing. The agent also reported section-budget differences (opening 4:20 versus 4:00; main 3:30 versus 4:00) without inventing revised durations; the 7:50 total still fits within eight minutes.

The recorded result is a `drafted` batch, two `drafted` annotations, and one `open` annotation. The agent did not review or finalize its initial edits, rewrite locked state directly, or leave a service or waiter running. The request, commands, result JSON, and English report are inspectable under `skill/annotation-everything-workspace/agent-eval/`. This is a demonstration in the current session, not a blind or cross-model discovery benchmark.

`evals/discovery-cases.json` adds six Chinese/English requests for generation, applying annotations, and recovery. Their discovery description and intended workflow were manually checked. They can be used to inspect actual model skill-loading traces later. Literal keyword checks do not prove model discovery rates; unrun automated multi-model evaluations are not reported as passed.

## Representative compatibility matrix

| Contract | Regression evidence |
|---|---|
| Chinese defaults for old reviews and unconfigured calls; auto fallback; no annotation writes or identity changes on switching | `test_locale_loop.py`, `test_i18n.py`, browser `locale_checks` |
| Legacy Chinese Markdown, both marked grammars, cross-language imports, and JSON structure | `test_export_format.py`, browser `export_import_checks`, and installed-copy file exports and CLI imports |
| Machine JSON, verdict codes, exit codes, and Chinese stored values | `test_ae_cli_i18n.py`, `test_serve_locale.py`, `test_notes_exchange_i18n.py` |
| Legacy service/agent submission requirements, compatibility markers, and unconfirmed options | `test_handoff_i18n.py`, `test_core.py`, browser `full_check_checks` / `handoff_i18n_checks` |
| Disconnects, hung services, 5xx errors, queue order, wrong services, and isolation | Existing failure/recovery scenarios in the full browser suite |
| Script closing tags, quotes, braces, HTML, and malicious attributes | `test_review_page_i18n.py`, `test_script_formats.py`, browser CSP/injection and cross-review import scenarios |
| Missing keys, placeholders, required plural branches, and counts 0 / 1 / 2 | `test_i18n.py` and browser language scenarios |
| Complete installed resources and reachable on-demand guides | `test_ae_cli.py`, installed-copy delivery, and the existing whole-copy snapshot comparison |

## Supported scope and limits

Supported languages are Simplified Chinese and English. Tags such as `zh-Hant` and `zh-TW` are not Simplified Chinese aliases. User material, annotations, third-party skill descriptions, and raw system errors are not automatically translated. Display language does not choose the material language, region, or time zone. The priority of `--locale`, `AE_LOCALE`, and review settings is explicit; export wrappers use `--lang`.

File mode cannot wake an agent, and its storage is separate from HTTP. Markdown omits annotation IDs, page keys, timestamps, batches, and complete trails and retains the legacy limits for metadata-like lines, blank lines, and status prefixes. JSON is a complete annotation backup, not an archive of the separate submission history and source snapshots. Legacy services still require the shared protocol's treatment of compatibility messages and message limits.

Real-browser validation used the Chrome environment above. Safari, Firefox, other operating systems, and other model behaviors are not claimed as verified. The personal-machine trust boundary is unchanged: loopback binding, Host checks, same-origin Origin, JSON Content-Type, no CORS, and CSP remain required.
