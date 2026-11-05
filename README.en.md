<p align="center">
  <a href="README.md">简体中文</a> · <b>English</b>
</p>

<p align="center">
  <img src="docs/assets/banner.en.svg" alt="annotation-everything: annotate pages, then hand off to an agent" width="100%">
</p>

<p align="center">
  annotation-everything is a review and annotation tool that runs on your machine, packaged as an Agent Skills skill. Any agent that adopts the open Agent Skills standard can use it. It installs into Claude Code's skill directory by default, and <code>--target</code> / <code>--link</code> put it into other skill directories.<br>It puts a page-by-page HTML report and its speaker notes side by side in one page. Reviewers annotate each page in the browser, submit with one click, and an agent revises the report and the speaker notes accordingly.
</p>

<p align="center">
  <img alt="version 0.11.0" src="https://img.shields.io/badge/version-0.11.0-1f3a5f?style=flat-square">
  <img alt="Agent Skills skill" src="https://img.shields.io/badge/Agent_Skills-skill-d97757?style=flat-square">
  <img alt="Python standard library only" src="https://img.shields.io/badge/Python-standard_library_only-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="listens on 127.0.0.1 only" src="https://img.shields.io/badge/listens_on-127.0.0.1-2fa36b?style=flat-square">
  <img alt="usage data not sent" src="https://img.shields.io/badge/usage_data-not_sent-555?style=flat-square">
</p>

<p align="center">
  <a href="#why">Why</a> ·
  <a href="#features">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#security-and-privacy">Security and privacy</a> ·
  <a href="#project-layout">Project layout</a> ·
  <a href="#development">Development</a>
</p>

<br>

<p align="center">
  <img src="docs/assets/receiver.en.png" alt="Synthetic review page with report, speaker notes and annotations; the header identifies the receiving harness, session and waiting state" width="92%">
  <br>
  <sub>The review page places the report, speaker notes and annotations side by side, with the receiving harness, session and waiting state in the header. Interface labels are in English; material stays in its original language. All data shown is fictional.</sub>
</p>

## Why

Reviewing a presentation of a few dozen pages usually means taking screenshots, marking them up in red, sending messages, and then relaying every comment to whoever revises the material, whether that is a colleague or an agent. That approach has problems: comments end up scattered across places and page numbers get mixed up; the report gets fixed while the speaker notes are forgotten; and by the end, nobody can say which comments have been addressed.

annotation-everything moves this whole process into a single page that runs on your machine:

- **Comments stay where they belong.** Every annotation is tied to a specific page, a quoted passage, or a spot on the report. The agent receives the original annotations with their pages and positions, not a retelling.
- **The report and the notes change together.** Report pages and speaker-note pages are matched one to one. When you select a sentence on one side to request a change, and the same page on the other side has an identical or similar sentence, the page suggests changing both. The agent revises page by page and then regenerates the review page.
- **Every round is recorded.** Each click on **Hand to the agent** creates a batch. Every annotation has a status and a resolution note that you can look back on at any time.
- **Your material never leaves your machine.** The server listens on `127.0.0.1` only, makes no network requests, and sends no usage data. Annotations are saved in a review directory you choose.

## Features

<table>
  <tr>
    <td width="50%" valign="top">
      <h4>✍️ Annotate the text itself</h4>
      Select text in the report or the speaker notes to mark it for deletion, propose a rewrite, or quote it in a comment. When the same page on the other side has a matching sentence, the page suggests changing both.
    </td>
    <td width="50%" valign="top">
      <h4>📍 Mark positions on the report</h4>
      Click to drop a pin, or press and drag to mark a region. Pins and regions can be moved, and regions can be resized. Exported annotations include each mark's position, as percentages measured from the top-left corner of the page.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>🤖 Hand off to an agent in one click</h4>
      The agent binds its receiving session, then configures a waiter or event delivery. When the reviewer clicks <b>Hand off to Agent</b>, it revises the report and speaker notes, rebuilds the page and records each result. The page always shows the receiving harness, short session ID and state; other sessions cannot claim a bound review.
    </td>
    <td valign="top">
      <h4>🔍 Full consistency check</h4>
      An option you can tick when submitting. Besides handling the annotations, the agent checks the entire report and speaker notes for consistent figures, metric definitions and wording, fixes the problems that the source material supports, and lists anything the reviewer needs to confirm.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>🧭 Draft first, then review (optional)</h4>
      Handle a batch in two stages: one model drafts the changes, then a more capable model reviews them. You can also stop after the draft and decide whether to request a review or finalize directly. Every annotation keeps a record of what happened at each stage.
    </td>
    <td valign="top">
      <h4>🧩 Skills and standing skills</h4>
      Finds skills in common local skill directories and plugin caches (<code>~/.claude</code>, <code>~/.codex</code>, <code>~/.agents</code>, and <code>~/.zcode</code>), lets you search them by name and description, and suggests skills based on what is being reviewed. Once a skill is set as a standing skill, the agent calls it first for every annotation within its scope.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>🗂️ Batch history</h4>
      See, for each submission, how many annotations were resolved, who handled each stage, and what was done. The "Current task details" switch expands or collapses the latest batch, and you can stop this review's local service here. When you reopen the page, it tells you which page you were last annotating and whether the agent has finished.
    </td>
    <td valign="top">
      <h4>🔤 PPTX font check</h4>
      Before converting a PPTX file to images, list the fonts it declares and those missing on your machine. After converting to PDF, compare against the fonts embedded in the PDF. This catches cases where a missing font is silently replaced by a different one.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>📚 Review overview and archiving</h4>
      Browse reviews in this workspace or across the machine, find material by status and activity, and inspect summaries and pending submissions. Open, start or archive a review in the browser.
    </td>
    <td valign="top">
      <h4>🔄 Automatic release updates</h4>
      The skill periodically checks its installation source for stable releases, installs and announces updates, and keeps the current copy when offline or locally modified.
    </td>
  </tr>
</table>

The review page can use Simplified Chinese or English. Pass `--locale auto`, `zh-CN`, or `en` when building; the page can switch language too, and the choice stays in this browser. Annotation text stays as written, and the stored target values `报告`, `讲稿`, and `报告和讲稿` do not change with the language. Omitting `--locale` keeps a new review in Chinese and leaves an existing review on its saved choice. Adding, editing, deleting and reopening annotations, quotes, quick delete and replace, pins and area selection, re-annotating, styling, page numbers, the speaker-notes column, Q&A labels, the help text, tooltips and accessible names all follow the current language, as do the confirmation dialogs. So do the skill box, its suggestions and "Suggested" tag, the empty result, the standing-skill scope labels and add/remove prompts, and the output of `notes.py skills`. Search still matches the skill catalog's original name and description, and the stored scope values `all` / `报告` / `讲稿` / `style` stay unchanged. Exported Markdown can be Chinese or English (following the interface by default); both the page and CLI can import either language. The "Hand to the agent" dialog, submission badge, batch history, resume hint and connection guidance also follow the current language. Submission options (finalize or stop at drafted, allow subagents, full check) do not depend on the interface language.

### Review overview and archiving

<p align="center"><img src="docs/assets/overview.en.png" alt="Synthetic review overview: status groups and a detail pane with the report summary, purpose, audience, core questions and start action" width="92%"></p>

Choose "Review overview" on any running review page, or open `/#overview` at its address. The default scope is "This workspace"; switch to "All on this machine" to see other registered reviews. The workspace is the review's git repository root, or its parent directory when it is outside a git repository. Paths are relative; other workspace paths start with `~` or show only the review name. If no review is running, start one with `serve.py <review-id>` first.

The list groups Running, Stopped, Archived and Invalid reviews, with the latest activity first within each group. Select a review to see its summary, purpose, audience, core questions and pending receipt/review counts. Choose "Open" for a running review or "Start and open" for a stopped one to open its page in a new tab. Starting uses its owned port; free an occupied port or refresh the overview when the ledger entry or directory is invalid, then follow the guidance to retry. Invalid entries provide a `serve.py forget <short-id>` cleanup command. "Back to current review" returns to the page hosting the overview.

Archiving hides reviews that are no longer being advanced from the default list. Annotations, submissions and the owned port remain, and the service keeps running. Choose "Expand" or "Show archived" to reveal them, then select a review and choose "Unarchive". An archived current review displays a banner in both views and still accepts annotations and submissions; archiving does not cancel submitted processing requirements. `serve.py list` still includes every review and marks archived entries.

<details>
<summary><b>Current task and stopping the local service</b></summary>
<br>
<p align="center"><img src="docs/assets/batch-controls.en.png" alt="Current task details switch and Stop local service button in processing history" width="88%"></p>
Press `J` or click the agent status to open "Processing history". The current task is the latest batch handed to the agent. Its details start collapsed; expand them to see the note, requirements, stages and summary. Polling and language changes preserve the expanded state; a new batch starts collapsed.

"Stop local service" sends held changes first and keeps the service running if saving fails. Older services show an upgrade hint. Stopping takes every tab for this review offline while preserving the materials, annotations, submission history and owned port. Other reviews and running agents keep working. The page shows the command to restart the service. "Close" in the top right only closes the history panel.
</details>

<details>
<summary><b>Export and import annotations</b></summary>
<br>
<p align="center"><img src="docs/assets/notes-export.en.png" alt="All annotations panel: choose export language, copy for an agent, download as Markdown or JSON, import" width="88%"></p>
When no agent is waiting, you can copy all annotations from the All annotations panel and paste them to any agent, or download them as a Markdown or JSON file and import them later.

The first line of the Markdown is a format marker (`<!-- annotation-everything export v1 lang=… -->`) that names the format version and the content language, so parsing picks the Chinese or English grammar from the file itself and never from the language of the importing interface: a Chinese export imports in an English page and an English export imports in a Chinese page, and both land with the same stored values. The export language can be picked in the panel (following the interface by default) or on the command line with `notes.py export --lang en`. Annotation text, quotes, resolution notes and skill names stay as written; only the surrounding wording is translated. Exports without the marker are read as the older Chinese format, and text that matches nothing gets a clear message instead of a guess. Older tools cannot read an English export and are told to upgrade rather than left to guess. The command line exports the same way: `notes.py export <review-dir> [--lang zh-CN|en] [--format json]`.
</details>

### Drafting from source material and verifying reports

When no report exists yet, give the agent your source material and requirements. The agent follows the [built-in generation guide](skill/annotation-everything/references/generation.en.md), shipped with the skill, to produce a page-by-page HTML report and optional speaker notes. It reuses requirements already supplied, asks only about missing details that affect the result, and uses the supplied [base report stylesheet](skill/annotation-everything/references/report-base.css) by default. When you name an available external generation skill, or the task needs an extension, an external method can be used. Its output receives the same verification, and the method actually used is recorded.

Reading source material depends on the host's available capabilities. The agent records what was read, partly read or unreadable, including the scope and reasons, and preserves citations and calculation definitions for key claims and figures. Gaps are disclosed; if all substantive material is unreadable, the draft cannot be declared complete. The report, requested speaker notes and `report-record.json` verification record stay beside the controlled source material. The generation guide defines the record and the workflow.

For a newly generated report, write a one- or two-sentence `summary` in its verification record to identify it in the overview. The field is optional and older records remain valid. Without it, the overview shows "(No summary)" with the cover title. The summary does not replace the report's verification verdict; see [step 4, "Write the verification record," in the generation guide](skill/annotation-everything/references/generation.en.md).

“Draft complete” and “Report deliverable” are separate verdicts. Readable, structured material with disclosed gaps can form a reviewable draft. Delivery also requires program checks and content, structure and visual verification; gaps affecting core conclusions block delivery. `report_check.py` provides structured results and a readable summary, and checks deterministic rules only. The agent performs the content judgments and page-by-page visual checks; unperformed, unavailable or stale checks never count as passed. Existing reports without a record can still enter review directly, with no backfill required. Revisions to reports with a record maintain the affected verification info, while report quality stays separate from annotation-processing status.

### Language and agent guides

One skill copy handles Chinese requests such as “按批注改” and English requests such as “review this report” or “apply my annotations”. The same `annotation-everything` entry point loads the [Chinese workflow](skill/annotation-everything/references/workflow.zh-CN.md) or [English workflow](skill/annotation-everything/references/workflow.en.md) on demand. Both read one [shared safety and protocol reference](skill/annotation-everything/references/protocol.md), including the stop before review, subagent permission, and evidence and uncertainty requirements for a full check. No language-specific installation is needed.

Supported languages are Simplified Chinese and English. English regional tags share the English resources; `zh` and `zh-Hans` map to Simplified Chinese. Traditional Chinese and other languages are not supported. The saved browser preference overrides the review default. `auto` matches browser languages in preference order and falls back to English. New reviews and old commands without a language setting remain Chinese. Interface, material, and annotation languages are independent; switching preserves drafts and unsent changes.

The page separates service connection from receiver binding and always shows the harness, short session ID and waiting or processing state. Stopping a waiter retains the binding; receivers never switch automatically. Finish active batches before an explicit transfer. See [Receiver binding and transfer](skill/annotation-everything/references/receiver-binding.en.md) for setup and session changes.

Service mode accepts real submissions through a running waiter or hook. Automatic agent wake-up depends on host support. Codex desktop supports a review-scoped binding: the agent runs `wakeup.py bind REVIEW_DIR`, then normal `serve.py` starts and restarts restore event delivery. The page distinguishes desktop acceptance from actual chat acknowledgement. No Codex configuration changes are required. See [Agent mechanisms](skill/annotation-everything/references/agent-mechanisms.en.md#codex-desktop-review-scoped-event-delivery) for compatibility and idle-chat verification. Opening `index.html` directly supports browser annotations, copying, and exports; import those annotations to give them to an agent. JSON preserves the annotation structure and review identity. Markdown omits IDs, page keys, timestamps, and processing batches and retains the legacy grammar's text limits; see the [format reference](skill/annotation-everything/references/formats.en.md). The [delivery evidence](docs/locale-delivery.en.md) records three complete language combinations, the compatibility matrix, environment, and limits.

[Codex idle-wakeup acceptance](docs/codex-delivery.en.md) records two real idle submissions from an isolated synthetic page, normal restart recovery, and adapter limits.

## How it works

### Architecture

Every component runs on your machine. The browser talks only to the local server; the agent reads and writes the same review directory through a command-line tool; and the review ledger records nothing more than each review's ID, directory and port.

```mermaid
flowchart LR
    U(["Reviewer"]) --> B["Browser<br>review page"]
    B <-->|"HTTP<br>127.0.0.1 only"| SV["serve.py<br>local server"]
    SV <--> DIR[("Review directory (one per review)<br>index.html: review page<br>review.json: review ID, sources and build options<br>notes.json, notes.jsonl: annotations and change log<br>handoff.json: submitted batches and their status<br>skills.json: skill catalog and standing skills")]
    NP["notes.py<br>annotation command line"] <--> DIR
    A(["Agent"]) <-->|"wait / show<br>resolve / done"| NP
    A -->|"revises per the annotations"| SRC["Source material<br>report: page-by-page HTML<br>speaker notes: Markdown"]
    SRC --> BV["build_viewer.py<br>builds and rebuilds the review page"] --> DIR
    L[("Review ledger<br>~/.annotation-everything/<br>review ID → review directory, dedicated port")] -.-> SV
    L -.-> NP
```

### One review, end to end

```mermaid
sequenceDiagram
    autonumber
    actor U as Reviewer
    participant P as Review page (browser)
    participant S as Local server (127.0.0.1)
    participant A as Agent

    A->>S: Build the review page with build_viewer.py, start the server with serve.py
    A-->>S: Run notes.py wait to wait for the reviewer's submission
    U->>P: Annotate page by page: rewrite, quote, drop pins, mark regions
    P->>S: Each annotation is saved to the review directory automatically
    U->>P: Click "Hand off to Agent", optionally with the full consistency check
    S-->>A: Notify the agent and deliver this batch
    A->>A: Revise the report and speaker notes page by page, regenerate the review page
    A->>S: Mark each annotation resolved with notes.py resolve, then close the batch with notes.py done
    S-->>P: The page shows progress and batch history
    A-->>S: Run notes.py wait again for the next batch
```

### The life of a batch

Each click on **Hand to the agent** creates a batch. Without the two-stage flow, a batch goes straight from "In progress" to "Resolved". With draft first, then review, a finished draft also goes through review, unless the reviewer finalizes it directly.

```mermaid
stateDiagram-v2
    direction LR
    state "Waiting to be received" as pending
    state "In progress (draft)" as claimed
    state "Drafted, awaiting review" as drafted
    state "Waiting for review" as review
    state "Under review" as reviewing
    state "Resolved" as done

    [*] --> pending: Reviewer clicks "Hand off to Agent"
    pending --> claimed: Agent receives it
    claimed --> done: Single stage, finished
    claimed --> drafted: Draft done, asked to stop for review
    claimed --> review: Draft done, asked to finalize
    drafted --> review: Reviewer requests a review
    drafted --> done: Reviewer finalizes directly
    review --> reviewing: Reviewing agent receives it
    reviewing --> done: Review finished, finalized
    done --> [*]
```

### Draft first, then review

The reviewing agent gives a verdict on each annotation: approve it, revise it further itself, or return it to the reviewer to decide. It never sends an annotation back to the drafting agent.

```mermaid
flowchart LR
    S["A batch of annotations"] --> D["Draft<br>for example, by a lower-cost model"]
    D --> W{"What the reviewer asked for"}
    W -->|"Stop for review"| H["Reviewer checks the draft"]
    H -->|"Request a review"| RV
    H -->|"Finalize directly"| F["Resolved"]
    W -->|"Finalize"| RV["Review<br>for example, by a more capable model"]
    RV -->|"Approve"| F
    RV -->|"Revise further itself"| F
    RV -->|"Return to the reviewer"| U["Flags the annotations that need the reviewer's decision, with reasons"]
```

## Installation and maintenance

The management command is `ae.py` at the repository root (Python standard library only), with four subcommands: `install`, `doctor`, `repair`, `uninstall`. The tool is an Agent Skills skill: any agent that adopts the open Agent Skills standard can load `SKILL.md` and follow it. It installs into Claude Code's skill directory by default. `--target` and `--link` cover every other agent's skill directory (see below).

### For people

Prerequisites: Python 3.9 or later, macOS or Linux.

```bash
python3 ae.py install                            # install: copy the skill into ~/.claude/skills/ (the skill copy)
python3 ae.py install --target <dir>             # or into another skills root (the copy lands at <dir>/annotation-everything)
python3 ae.py install --link codex,agents,zcode  # optional: also symlink the copy into these agents' skill directories
python3 ae.py install --link cursor=~/.cursor/skills  # --link also accepts name=path to link any skills directory
python3 ae.py doctor                             # check: installation, runtime, optional tools, dev environment
python3 ae.py repair                             # repair: prints a plan first, fixes the safe items automatically
python3 ae.py uninstall                          # uninstall: remove the skill copy and links, keep the review ledger by default
python3 ae.py doctor --target <dir>              # doctor/repair/uninstall accept the same --target to locate a custom copy
python3 ae.py doctor --locale en                 # optional: interface language auto, zh-CN or en (before or after the subcommand)
python3 ~/.claude/skills/annotation-everything/scripts/update.py check  # check releases now without installing
python3 ~/.claude/skills/annotation-everything/scripts/update.py now    # check now and install a newer release
```

- **Release updates**: when an agent follows `SKILL.md`, each skill copy checks its install source's `origin` for stable version tags (`vMAJOR.MINOR.PATCH`) at most once a day, and automatically installs and announces a newer release. Prerelease tags are ignored. Offline, authentication, and source errors leave the current version usable. Local changes in the skill copy or a running review server or annotation process defer the update with a notice. Stop them and run `update.py now` to retry. For a custom installation, substitute that skill copy's path in the commands above. Set `AE_AUTO_UPDATE=0` to disable automatic checks; manual commands still work.
- Updates replace only the skill copy: its symlinks, review ledger, review directories, and source checkout are preserved. The release checkout used for later checks, repairs, and updates normally lives in `${XDG_DATA_HOME:-~/.local/share}/annotation-everything/updates/`; if that overlaps the copy, it lives in a separate directory beside it. Keep it while using that skill copy. To get the updater for the first time, run `ae.py install` once from a version that contains it. When upgrading an existing review, stop its old service, rebuild the review page, start the new service, and refresh the browser.
- Updating requires Git, access to the source repository, permission to enumerate local processes, and filesystem support for atomic directory exchange (`renamex_np` on macOS or a Linux C library that provides `renameat2`). A restricted environment leaves the current version in place and reports the limitation; run `update.py now` in a terminal with the required permissions. After a successful exchange, the old copy stays in `.ae-update-*` beside the installed copy so concurrent changes can be recovered.
- Each check reports pass / warning / failure; failures include a repair or diagnostic command. Exit code 0 means all passed or only warnings, 1 means there are failures, and 2 means the command itself failed (`doctor --json` outputs `error`). Development checks appear only when the current working directory is inside the source repository.
- **Interface language**: `ae.py`, `serve.py` (`serve` / `status` / `list` / `forget` / `check`), `notes.py`, `check_pptx_fonts.py` all accept `--locale auto|zh-CN|en` (before or after the subcommand) and the `AE_LOCALE` environment variable; generator help, results, and resource diagnostics follow the explicit `--locale`, then `AE_LOCALE`, then the saved review setting. The order is the explicit flag, then `AE_LOCALE`, and Chinese when neither is set; only `auto` reads `LC_ALL` / `LC_MESSAGES` / `LANG`. `serve.py serve` and `serve.py status` also fall back to the `locale` saved in that review once its directory is resolved; `list`, `forget` and `check` do not map to a single review and only honour the first two levels. An unsupported choice stops the command with exit code 2 and an explanation, changing nothing. Help, option descriptions, missing values, invalid values and command failures all use one language; source paths, commands, font names, skill names and raw system errors stay as they are.
- **Machine verdicts are separate from human wording**: in `doctor --json`, `category`, `level`, `code` and `summary` are the verdict fields older consumers read and stay in Chinese regardless of language; the language-independent additions are each item's `status` (`pass` / `warn` / `fail`) and `category_id` (`install` / `tools` / `dev` / `runtime`), plus the top-level `schema` (structure version) and `locale` (which language `message` uses). New consumers can compare `status` / `category_id` without comparing localized labels, while `message` and `fix_command` follow the language. `serve.py` works the same way: HTTP statuses and reason codes (`bad-host`, `cross-site`, `bad-origin`, `no-origin`, `bad-content-type`, `chunked`, `bad-length`, `too-large`, `bad-json`, `not-found`, `preflight`, `no-index`, `no-note`, `server-error`, `review-mismatch`, `no-api`) never change with the language, only the explanation does; an unknown error names the type and the recovery step and never echoes request content.
- In `repair`, deleting stale ledger entries (`--forget-stale`) and stopping orphaned servers (`--stop-orphans`) are destructive and need their explicit flags; `--dry-run` only prints the plan. Plans and execution reports state plainly that nothing was executed this time, in either language.
- `uninstall` never touches review directories; `--purge` also deletes the review ledger; a running `serve.py` blocks it unless `--stop-servers` stops it first.
- **Install into another skills directory**: by default `install` copies the skill into `~/.claude/skills/` (`CLAUDE_SKILLS_DIR` still works as an override); `--target <dir>` installs to `<dir>/annotation-everything` instead. `doctor`, `repair` and `uninstall` accept the same `--target` to check, fix and remove that copy, and failing items' fix commands repeat the same `--target`. Besides the built-in names `codex`, `agents` and `zcode`, `--link` accepts `name=path` (e.g. `cursor=~/.cursor/skills`) to symlink the copy into any skills directory and record it in the install record; custom links show up in the checks too (dangling ones fail with a fix command), `repair` rebuilds them and `uninstall` removes them. The install record lives inside the copy, so once the copy is gone custom links can no longer be discovered automatically (links in the built-in directories still have the scan fallback).
- Before stopping a server, the command verifies that Python is executing the script from this repository or the current skill copy, then checks the process identity again before sending a signal; it refuses to stop an unverified process. `--purge` removes only known ledger files and refuses to proceed if the ledger is corrupt or its directory contains other files or unregistered review directories.

Once installed, tell your agent "review this report and its speaker notes" or "I've finished annotating, apply my notes", and it will follow this workflow. How the agent is woken up and how it invokes a skill named on an annotation differ by agent. The [Agent mechanisms reference](skill/annotation-everything/references/agent-mechanisms.en.md) maps the four cases.

### For agents

Send the following to your agent, replacing `<repo-path>` with the absolute path of this repository:

> Install and check the annotation-everything skill in `<repo-path>`. Follow the numbered steps, confirming the success signal before moving on:
>
> 1. Run `python3 <repo-path>/ae.py doctor --json`. On exit code 2, inspect the JSON `error` and resolve it; a corrupt installation record can be rebuilt by reinstalling in step 2. In normal output, if no item in `results` has `category` `安装状态` (installation) with `level` `失败` (fail), the skill is already installed — skip to step 4.
> 2. Pick the link flag by which agent you are: Claude Code needs no `--link`; Codex uses `--link codex`; agents under `~/.agents` use `--link agents`; zcode uses `--link zcode`; other agents use `--link name=skills-dir` (e.g. `--link cursor=~/.cursor/skills`). To keep the primary copy out of the Claude directory, add `--target <dir>` (pass the same value to the later `doctor` / `repair` / `uninstall`). Run `python3 <repo-path>/ae.py install` (adding the flags you need). Success: exit code 0.
> 3. Run `python3 <repo-path>/ae.py doctor --json` again and judge by the JSON: prefer the language-independent verdict fields — no item with `status` `fail` and `category_id` `install` means the install succeeded (the older reading, `category` `安装状态` with `level` `失败`, still holds: both fields stay in Chinese). Failures in the `运行状态` (runtime) category, such as orphan servers or an owned port held by another process, do not affect the install; note them for the report in step 5. Warnings in `summary.warn` (such as missing optional tools `fc-list`/`pdffonts`) do not block use. If installation failures remain, apply that item's `fix_command` and repeat this step; if a `fix_command` contains a destructive flag listed in step 4, ask the user for consent before running it.
> 4. When repairs are needed, run `python3 <repo-path>/ae.py repair` (`--dry-run` shows the plan first). **`--purge`, `--forget-stale`, `--stop-orphans`, `--stop-servers` are all destructive — ask the user for consent before adding any of them.**
> 5. Report back to the user: whether the install succeeded, the check results, and any warnings still unresolved.

Interface text defaults to Chinese. To get English output, append `--locale en` to any command above (or set `AE_LOCALE=en`); judge the result with `status` / `category_id`, which do not depend on the language.

## Quick start

**1. Install the skill**

```bash
python3 ae.py install
```

The skill is copied to `~/.claude/skills/annotation-everything` (that copy is the *skill copy*); to install into another skills root, use `python3 ae.py install --target <dir>` (the copy lands at `<dir>/annotation-everything`). See **Installation and maintenance** above for the full install, check, repair and uninstall instructions.

**2. Try it on the bundled sample**

```bash
S=skill/annotation-everything/scripts
export AE_HOME=$(mktemp -d)   # keep the review ledger in a temporary directory so your real ledger is untouched
python3 $S/build_viewer.py --report fixtures/sample/report.en.html --script fixtures/sample/script.en.md --out /tmp/ae-demo --locale en
python3 $S/serve.py /tmp/ae-demo --locale en
```

The review page opens in your browser. Add a few annotations, click **Hand to the agent**, then run this in another terminal:

```bash
python3 $S/notes.py wait /tmp/ae-demo --locale en
```

The command prints the annotations you just submitted, which is exactly what the agent receives.

For Chinese material, use `fixtures/sample/report.html` and `fixtures/sample/script.md`. Keep `--locale en` to review it in English, or choose `--locale zh-CN` for a Chinese interface. Changing the source material alone does not change the interface language.

**3. Prepare your own material**

- **Report**: one HTML file in which every page is a `<section class="page" data-page="N" data-key="unique-key">` element.
- **Speaker notes (optional)**: one Markdown file split into pages by `### 第 N 页｜标题` headings ("page N | title"), giving the duration, talking points, transition and likely questions for each page. English notes use `### Page N｜Title` with `**Duration**`, `**Talking points**`, `**Transition**` and `**Likely questions**` instead. Both forms may be mixed in one file, and which form is recognized does not depend on the interface language: a Chinese interface reviews English notes and an English interface reviews Chinese notes, with titles and body text kept exactly as written. When a heading cannot be recognized, a page number repeats or a field repeats, the builder reports the line number and the expected form in the current language instead of treating the input as an empty script.

See the [English format reference](skill/annotation-everything/references/formats.en.md) for the full input and output formats.

<details>
<summary><b>Command reference</b></summary>

| Command | What it does |
|---|---|
| `ae.py install` / `doctor` / `repair` / `uninstall` | Management commands run from the repository root: install the skill copy into `~/.claude/skills/` (`--target <dir>` for another skills root; `--link codex,agents,zcode` or `name=path` symlinks it into other agents' skill directories); check installation, runtime, optional tools and dev environment (`--json` for structured output); repair what the checks find (prints a plan first, fixes safe items automatically, `--forget-stale`/`--stop-orphans` run the destructive ones explicitly, `--dry-run` only prints); uninstall removes the skill copy and the links pointing at it, keeps the review ledger by default (`--purge` deletes it too; review directories are never touched; a running `serve.py` blocks uninstall unless `--stop-servers` stops it first). All four subcommands accept `--target <dir>` to locate a custom copy |
| `update.py auto` / `check` / `now` | Check and automatically install a release / only check / check and install immediately from the skill copy; the skill entry runs `auto` at most once a day |
| `verify.py plan` / `run` / `report` | Unified verification entry run from the repository root: `plan` explains the groups and commands without starting anything (`plan --phase development` also shows what the current changes should run, why, and when it falls back to the full check); `run` executes and writes structured records plus raw logs into `.verify/` (git-ignored), records the shadow comparison between the suggested selection and the full gate by default, and lets later stages reference currently valid passing evidence (`--no-reuse` forces a full rerun); `report` only reads records and shows the evidence chain. Groups: `unit` (full unit), `unit-related` (explicitly named cases, always augmented with the global dictionary contract in `tests/test_i18n_catalog_contract.py`), `browser` (full browser), `serve-check` (server security self-check), `all` (runs `unit` → `serve-check` → `browser` in order). Field contract: [docs/verify/record-contract.md](docs/verify/record-contract.md), selection strategy: [docs/verify/selection-strategy.md](docs/verify/selection-strategy.md) |
| `build_viewer.py --report … [--script …] --out <review-dir>` | Build the review page with optional speaker notes; `--rebuild <review-dir>` rebuilds it with the recorded options; `--locale auto`, `zh-CN`, or `en` sets the interface language. Omit it and a new review stays in Chinese; a rebuild keeps the saved choice |
| `report_check.py report.html [--script script.md] [--record report-record.json]` | Public report verification entry: a readable summary by default, structured results with `--json`, and `--locale en` or `zh-CN` for the language. Exit 0 means program checks passed, not that the report is deliverable; 1 means checks failed; 2 means an input or usage error. Content, structure and visual coverage and delivery blockers are listed separately |
| `serve.py <review-dir-or-id>` | Start the local server; each review keeps its own dedicated port |
| `http://127.0.0.1:<port>/#overview` (browser address) | Open the review overview to switch scope, inspect details, open or start reviews, and archive or unarchive them; start a local service first |
| `serve.py list [--json]` | List every review on this machine, including archived entries; both readable output and JSON include an archive flag |
| `serve.py status <review-dir-or-id>` | Show one review's running status |
| `serve.py forget <short-id>` | Remove a ledger entry confirmed unnecessary and release its owned port while keeping the review directory. The overview provides this command for invalid entries; cleanup runs in the CLI |
| `serve.py check` | Run the server's security self-check |
| `wakeup.py bind` / `status` / `check` / `ack` / `unbind` | Bind the receiving harness and session and inspect identity and state; Codex desktop uses events, other harnesses can use `--adapter wait`; transfer with `bind --replace` after active batches finish |
| `notes.py wait` / `claim` | Wait for a submission, or take one that is already waiting. `wait --locale` chooses the handoff language and preserves location, operation and skill requirements; `claim` keeps the original JSON values |
| `notes.py show` / `resolve` / `reopen` | List annotations, mark them resolved, reopen them. `show --locale` chooses the reading language and beats `AE_LOCALE` and the review setting; `show --json` still exports the raw values |
| `notes.py export` / `import` | Export annotations as a re-importable summary (Markdown with a version and language marker, or a structured JSON backup) / import a page export. `export --lang` follows the same priority as `show --locale`, plus `--format json`, `--all`, `--page N` and `--out <file>` |
| `notes.py done` / `review` / `diff` | Close the current stage, record a review verdict, show page by page what the draft changed; all accept `--locale` to choose the output language |
| `notes.py batches` | Show batch history |
| `notes.py skills` | Show or set the skill catalog and standing skills. `--locale` chooses the output language and beats `AE_LOCALE` and the review setting; scopes accept `全部` / `报告` / `讲稿` / `风格优化` or `all` / `report` / `script` / `style` |
| `check_pptx_fonts.py original.pptx [--pdf converted.pdf]` | Check fonts before and after converting a PPTX file; `--locale auto\|zh-CN\|en` or `AE_LOCALE` picks the interface language |

</details>

## Security and privacy

| Constraint | How it is enforced |
|---|---|
| Reachable from this machine only | The server listens on `127.0.0.1` only, validates the Host header, and never sends cross-origin resource sharing (CORS) headers |
| No cross-site forged writes | Write requests must come from the same origin as the page and use a JSON content type |
| Only what is needed is exposed | Only `index.html` and the `/api/*` endpoints are served; no other file in the review directory is reachable through the server |
| No third-party Python packages | The runtime uses the Python standard library only; the font check calls the system tools `fc-list` and `pdffonts` and explains how to install them if they are missing |
| Network access | The review server makes no network requests; the updater only contacts the installation's Git remote, sending no review data or telemetry. External resources in reports are not loaded; local images and fonts are embedded in the page |
| Your data stays local | Annotations and the review page live in the review directory you choose; the local review ledger `~/.annotation-everything/` records only review IDs, directory paths and ports |

Run `serve.py check` after any change to the server; it verifies each of these constraints.

## Project layout

```text
skill/annotation-everything/       the skill itself, copied as a whole on install
├── SKILL.md                       bilingual discovery and on-demand workflow entry
├── references/
│   ├── protocol.md               shared safety, stop conditions, and protocol
│   ├── workflow.zh-CN.md          Chinese workflow
│   ├── workflow.en.md             English workflow
│   ├── generation.zh-CN.md        Chinese generation guide, checklists and record contract
│   ├── generation.en.md           English generation guide, checklists and record contract
│   ├── report-base.css            base stylesheet for page-by-page HTML reports
│   ├── agent-mechanisms.zh-CN.md  Chinese agent mechanism reference
│   ├── agent-mechanisms.en.md     English agent mechanism reference
│   ├── receiver-binding.zh-CN.md  Receiving session identity, states and explicit transfer
│   ├── receiver-binding.en.md     English receiver binding and transfer
│   ├── formats.md                 Chinese input and output formats
│   └── formats.en.md              English input and output formats
├── locales/                       Chinese and English messages (zh-CN.json, en.json) and the locale contract
├── scripts/
│   ├── build_viewer.py            builds the review page index.html and review.json
│   ├── report_check.py            report verification: deterministic rules, coverage and delivery verdict
│   ├── i18n.py                    loads the shared catalogs and fills in the chosen language; holds the process-wide interface language state (set once by an entry, shared by every module); the command line boilerplate shared by every entry (reading --locale, translating argparse errors, reporting an unsupported language)
│   ├── update.py                  checks stable releases and updates the skill copy
│   ├── content_manifest.py        fingerprints the skill copy's files, directories and links
│   ├── viewer_template.html       review page and overview template (server mode and local file mode)
│   ├── serve.py                   local server: serve / status / list / forget / check
│   ├── review_overview.py         overview scope, report metadata and archive markers
│   ├── wakeup.py                  Receiving session binding; Codex events, receipts and deduplication
│   ├── notes.py                   annotation command line: wait / show / resolve / done / review and more
│   ├── notes_store.py             annotation storage: atomic writes, file locks, change log
│   ├── export_format.py           annotation exchange format: the marked Markdown grammar and the JSON backup
│   ├── ledger.py                  review ledger: review IDs, review directories and dedicated ports; service status file and liveness check, copied-directory check
│   ├── match.py                   finds matching sentences between the report and the speaker notes
│   ├── skill_sources.py           discovers skill directories on this machine
│   ├── fsutil.py                  shared helper for atomic writes, file locks and CSP script hashes
│   └── check_pptx_fonts.py        PPTX and PDF font check
└── evals/                         skill evaluations, including generation, verification and revisions (used with skill-creator)
ae.py                             unified management command: install the skill copy, doctor checks it, repair fixes it, uninstall removes it
verify.py                         unified verification entry: plan explains the scope, run executes and records, report only reads
verify_selection.py               staged test selection and shadow comparison strategy: versioned mapping, conservative fallback and gap comparison
verify_evidence.py                evidence reuse policy: valid same-input same-environment passes are referenced across stages
verify_report.py                  continuous efficiency reports, comparable groups and bounded deduplicated reviews
verify_parallel.py                bounded process scheduling inside a verification group; existing locks and failures are preserved
host_isolation.py                 host isolation protocol: locks and declarations for mutual exclusion by shared resource
fixtures/sample/                   synthetic sample (fictional data)
fixtures/show-baseline/            verbatim baselines of notes.py show output in Chinese and English (synthetic data)
fixtures/verify-doc-allowlist.json  synthetic document-gate paths and dependency-contract examples
fixtures/external-report-skill/    synthetic external generation skill; its output goes through the same acceptance
fixtures/external-report-skill-lite/ limited variant of it; proves missing evidence is reported as gaps
tests/                             unit tests and end-to-end browser tests
tests/e2e_waits.py                  browser condition waits: bounded polling, events and scenario timeout diagnostics
tests/e2e_scenarios.py              browser scenario registry and selection scheduling (no browser dependency)
tests/e2e_servers.py                server start/stop for end-to-end tests and cleanup after interruption
tools/
├── setup-dev.sh                   enables the repository's git hooks
├── collect_verify_history.py      reads existing verification artifacts and merge events from GitHub without running tests
├── grade_iteration.py             grades one round of skill evals and writes grading.json for each run
└── git-hooks/                     refuse commits on main, pushes to main and deleting main
.github/                           CI, issue templates and the pull request template
docs/                              architecture decision records (ADRs), agent conventions, README images
docs/verify/                       verification record field contract, the unified entry's onboarding guide and the browser scenario registry
GLOSSARY.md                        glossary
```

## Development

After cloning, run `tools/setup-dev.sh` once to enable the git hooks.

```bash
python3 verify.py plan                                            # unified verification entry: explains the groups and commands
python3 verify.py plan --phase development                        # selection advice: what the current changes should run, why, when to fall back
python3 verify.py run                                             # full unit tests (the default group), then prints a summary
python3 verify.py run --group all                                 # full acceptance: unit + server self-check + browser
AE_VERIFY_SERIAL=1 python3 verify.py run --group all               # restore serial execution with the same complete scope
python3 verify.py run --no-reuse                                  # independent recheck: bypass evidence reuse, run everything
python3 verify.py report --latest                                 # read the latest record only, start nothing
python3 verify.py report --efficiency                             # read the efficiency report from existing records
python3 verify.py report --efficiency --rebuild                   # rebuild derived reports and reviews without tests
python3 -m unittest discover tests                                   # unit tests (standard library only)
uv run --no-project --with playwright python tests/e2e_browser.py   # end-to-end browser tests (uses your local Google Chrome)
python3 skill/annotation-everything/scripts/serve.py check          # server security self-check
python3 ae.py install                                               # sync to ~/.claude/skills/
uv run --no-project --with playwright python tests/e2e_browser.py --list            # browser scenario registry (IDs, behavior, fixtures, isolation)
uv run --no-project --with playwright python tests/e2e_browser.py --only skill-i18n # run only the selected scenarios (comma-separated, deduplicated; legacy flags like --delivery-only remain as aliases)
```

`verify.py` is the unified verification entry: `plan` explains the scope, `run` executes and writes structured records plus raw logs into `.verify/` (git-ignored), and `report` only reads records. The input fingerprint covers the full content of effective input files; text and JSON reports preserve the original test result and show whether the evidence passes the current integrity checks. A record also carries the commit and baseline, the environment and per-case timings; the field contract is in [docs/verify/record-contract.md](docs/verify/record-contract.md) and the onboarding guide in [docs/verify/onboarding.md](docs/verify/onboarding.md). For PRs whose changes all fall within the built-in documentation allowlist, `run --group all --phase pr` records product tests as not applicable and leaves coverage to CI. Other PRs still execute the full local gate; other phases retain shadow mode. The direct commands keep working, with the same success and failure exit codes for completed test runs. Empty and fully skipped runs do not count as a pass; the new entry returns 1. When several workspaces on the same machine verify in parallel, resource-sensitive groups (full unit, full browser, direct runs of sensitive modules) queue on a shared-resource lock that spans worktrees and independent checkouts: lock waits are timed separately, a wait timeout is not a pass, and pure-logic groups are never blocked. See the [host isolation protocol](docs/verify/isolation.md).

A verification group uses at most 3 isolated processes. Sensitive unit modules, runner tests and unclassified new modules remain in the original verification process; only explicitly approved independent modules run in parallel. Each full-browser scenario has its own browser, synthetic HOME, ledger and skill directories. The group retains its existing resource lock, so sensitive verification in other workspaces still queues. `AE_VERIFY_SERIAL=1` restores serial execution. The execution mode is recorded in the environment, and later successes never erase failed or incomplete results. See [docs/verify/parallel-execution.md](docs/verify/parallel-execution.md) for boundaries and fallback.

Staged selection and shadow comparison are on by default: `run` records what the versioned mapping in `verify_selection.py` suggests for the current changes, except for documentation-only PR exemptions, the groups actually run stay the requested full gate; when the run finishes, the suggestion is compared side by side with the actual result, and any failure the full check found outside the suggested scope is kept as a candidate omission with an attribution. Unknown paths, bad baselines, broken rules, test-manifest gaps and changes to the strategy itself all fall back to the full check with an explanation. The `issue` phase never starts tests automatically; defect reproduction asks for a related group explicitly. See [docs/verify/selection-strategy.md](docs/verify/selection-strategy.md) for the strategy, fallback kinds and comparison samples.

Valid verification evidence is referenced across stages (`verify_evidence.py`, deliberately strict): within the same logical work session, when the input fingerprint and environment match, the requested scope is covered by the verified scope, the source record still passes the current checks, it is within the 24-hour freshness window and all versions agree, `run` references the existing result during the `issue` / `development` / `review` / `manual` phases. Only a hash of the session identity is stored: `AE_VERIFY_SESSION` takes precedence over inherited `CODEX_THREAD_ID` / `CLAUDE_SESSION_ID`; an unknown identity requires execution. Terminal setup is described in the [onboarding guide](docs/verify/onboarding.md). The report lists the source run, fingerprint, scope, environment and reason (differences proven to stay inside the documentation allowlist do not count as input changes; the relaxation is disabled under override mappings). Host-state-dependent groups (full unit, full browser, direct runs of sensitive modules) are never reused even with identical fingerprints, `pr` / `ci` gate phases never reuse results (documentation-only PRs mark product tests as not applicable; other gates execute), and class / case-level requests for targeted rechecks never reuse. Referenced groups show up as "reused" and are not counted twice; if the source is tampered with, expired or invalid, evidence integrity and shadow conclusions also degrade to unusable. `--no-reuse` forces a full rerun.

Continuous efficiency reports update timings, repeated executions, evidence references and failure candidates after each run. Read history with `python3 verify.py report --efficiency`; add `--rebuild` to rebuild derived reports without starting product tests. Comparison rules, periodic reviews, CI gaps and policy maintenance are described in [docs/verify/efficiency.md](docs/verify/efficiency.md). See the [final acceptance record](docs/verify/final-acceptance.md) for coverage comparisons, performance samples and raw synthetic evidence.

The browser end-to-end tests are organized around a scenario registry: no arguments runs every registered scenario; `--list` prints the registry; `--only scenario1,scenario2` deduplicates and runs only the targets plus their declared fixtures, while unknown, empty or conflicting selections fail before the browser starts. See [docs/verify/browser-scenarios.md](docs/verify/browser-scenarios.md) for the registry, legacy flag aliases and how to register new scenarios.

Browser waits observe DOM state, files, network events or server revisions according to the behavior under test. Timeouts record the scenario, condition and last observed state. See [docs/verify/browser-waits.md](docs/verify/browser-waits.md) for retained fixed observation windows, behavior comparisons and performance evidence.

- Tests put the review ledger in a temporary directory (the `AE_HOME` environment variable). When trying things by hand or running skill evaluations, run `export AE_HOME=$(mktemp -d)` first, so throwaway reviews are not registered in your real ledger and do not take ports that belong to real reviews.
- Set `AE_DELIVERY_ARTIFACTS` to a directory under `skill/annotation-everything-workspace/` to keep synthetic sources, command outputs, batch state, offline exports, and screenshots. See the [delivery evidence](docs/locale-delivery.en.md) for the command and evaluation boundaries.
- `main` is updated only by squash-merging pull requests: branch from an issue, put `Closes #<number>` in the pull request body, and merge once CI passes; the issue closes automatically on merge. See "分支、issue 与 PR" (branches, issues and pull requests) in [AGENTS.md](AGENTS.md) for the full workflow.
- The repository holds synthetic samples only. Real material, annotations and review directories never go into the repository or the skill directory.
- [GLOSSARY.md](GLOSSARY.md) defines the terms, [docs/adr/](docs/adr/) holds the architecture decision records, and [AGENTS.md](AGENTS.md) describes the collaboration conventions. All three are in Chinese.
