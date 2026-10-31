<p align="center">
  <a href="README.md">简体中文</a> · <b>English</b>
</p>

<p align="center">
  <img src="docs/assets/banner.en.svg" alt="annotation-everything: annotate pages, then hand off to an agent" width="100%">
</p>

<p align="center">
  annotation-everything is a review and annotation tool that runs on your machine, packaged as a Claude Code skill.<br>It puts a page-by-page HTML report and its speaker notes side by side in one page. Reviewers annotate each page in the browser, submit with one click, and an agent revises the report and the speaker notes accordingly.
</p>

<p align="center">
  <img alt="version 0.9.0" src="https://img.shields.io/badge/version-0.9.0-1f3a5f?style=flat-square">
  <img alt="Claude Code skill" src="https://img.shields.io/badge/Claude_Code-skill-d97757?style=flat-square">
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
  <img src="docs/assets/viewer.png" alt="Review page: the report with a pin and a highlighted region on the left, speaker notes and this page's annotations on the right" width="92%">
  <br>
  <sub>The review page: the report on the left, where you can click to drop a pin or drag to mark a region; the speaker notes and this page's annotations on the right. The interface is in Chinese, and all data shown is fictional.</sub>
</p>

## Why

Reviewing a presentation of a few dozen pages usually means taking screenshots, marking them up in red, sending messages, and then relaying every comment to whoever revises the material, whether that is a colleague or an agent. That approach has problems: comments end up scattered across places and page numbers get mixed up; the report gets fixed while the speaker notes are forgotten; and by the end, nobody can say which comments have been addressed.

annotation-everything moves this whole process into a single page that runs on your machine:

- **Comments stay where they belong.** Every annotation is tied to a specific page, a quoted passage, or a spot on the report. The agent receives the original annotations with their pages and positions, not a retelling.
- **The report and the notes change together.** Report pages and speaker-note pages are matched one to one. When you select a sentence on one side to request a change, and the same page on the other side has an identical or similar sentence, the page suggests changing both. The agent revises page by page and then regenerates the review page.
- **Every round is recorded.** Each click on **Hand off to Agent** creates a batch. Every annotation has a status and a resolution note that you can look back on at any time.
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
      The agent runs <code>notes.py wait</code> ahead of time to wait for a submission. When the reviewer clicks <b>Hand off to Agent</b>, the agent receives the batch, revises the report and speaker notes page by page, regenerates the review page, and marks each annotation as resolved. The page shows progress as it happens.
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
      Finds the Claude Code and Codex skills installed on your machine, lets you search them by name and description, and suggests skills based on what is being reviewed. Once a skill is set as a standing skill, the agent calls it first for every annotation within its scope.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>🗂️ Batch history</h4>
      See, for each submission, how many annotations were resolved, who handled each stage, and what was done. When you reopen the page, it tells you which page you were last annotating and whether the agent has finished.
    </td>
    <td valign="top">
      <h4>🔤 PPTX font check</h4>
      Before converting a PPTX file to images, list the fonts it declares and those missing on your machine. After converting to PDF, compare against the fonts embedded in the PDF. This catches cases where a missing font is silently replaced by a different one.
    </td>
  </tr>
</table>

The review page can use Simplified Chinese or English. Pass `--locale auto`, `zh-CN`, or `en` when building; the page can switch language too, and the choice stays in this browser. Annotation text stays as written, and the stored target values `报告`, `讲稿`, and `报告和讲稿` do not change with the language. Omitting `--locale` keeps a new review in Chinese and leaves an existing review on its saved choice. Adding, editing, deleting and reopening annotations, quotes, quick delete and replace, pins and area selection, re-annotating, styling, page numbers, the speaker-notes column, Q&A labels, the help text, tooltips and accessible names all follow the current language, as do the confirmation dialogs. So do the skill box, its suggestions and "Suggested" tag, the empty result, the standing-skill scope labels and add/remove prompts, and the output of `notes.py skills`. Search still matches the skill catalog's original name and description, and the stored scope values `all` / `报告` / `讲稿` / `style` and the Chinese export wording stay as they are. The hand-off and batch history, and the import/export wording, are still being adapted.

<details>
<summary><b>Export and import annotations</b></summary>
<br>
<p align="center"><img src="docs/assets/notes-export.png" alt="All annotations panel: copy for an agent, download as Markdown or JSON, import" width="88%"></p>
When no agent is waiting, you can copy all annotations from the All annotations panel and paste them to any agent, or download them as a Markdown or JSON file and import them later.
</details>

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

Each click on **Hand off to Agent** creates a batch. Without the two-stage flow, a batch goes straight from "In progress" to "Resolved". With draft first, then review, a finished draft also goes through review, unless the reviewer finalizes it directly.

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

The management command is `ae.py` at the repository root (Python standard library only), with four subcommands: `install`, `doctor`, `repair`, `uninstall`.

### For people

Prerequisites: Python 3.9 or later, macOS or Linux.

```bash
python3 ae.py install                            # install: copy the skill into ~/.claude/skills/ (the skill copy)
python3 ae.py install --link codex,agents,zcode  # optional: also symlink the copy into these agents' skill directories
python3 ae.py doctor                             # check: installation, runtime, optional tools, dev environment
python3 ae.py repair                             # repair: prints a plan first, fixes the safe items automatically
python3 ae.py uninstall                          # uninstall: remove the skill copy and links, keep the review ledger by default
```

- Each check reports pass / warning / failure; failures include a repair or diagnostic command. Exit code 0 means all passed or only warnings, 1 means there are failures, and 2 means the command itself failed (`doctor --json` outputs `error`). Development checks appear only when the current working directory is inside the source repository.
- In `repair`, deleting stale ledger entries (`--forget-stale`) and stopping orphaned servers (`--stop-orphans`) are destructive and need their explicit flags; `--dry-run` only prints the plan.
- `uninstall` never touches review directories; `--purge` also deletes the review ledger; a running `serve.py` blocks it unless `--stop-servers` stops it first.
- Before stopping a server, the command verifies that Python is executing the script from this repository or the current skill copy, then checks the process identity again before sending a signal; it refuses to stop an unverified process. `--purge` removes only known ledger files and refuses to proceed if the ledger is corrupt or its directory contains other files or unregistered review directories.

Once installed, tell Claude Code "review this report and its speaker notes" or "I've finished annotating, apply my notes", and it will follow this workflow.

### For agents

Send the following to your agent, replacing `<repo-path>` with the absolute path of this repository:

> Install and check the annotation-everything skill in `<repo-path>`. Follow the numbered steps, confirming the success signal before moving on:
>
> 1. Run `python3 <repo-path>/ae.py doctor --json`. On exit code 2, inspect the JSON `error` and resolve it; a corrupt installation record can be rebuilt by reinstalling in step 2. In normal output, if no item in `results` has `category` `安装状态` (installation) with `level` `失败` (fail), the skill is already installed — skip to step 4.
> 2. Pick the link flag by which agent you are: Claude Code needs no `--link`; Codex uses `--link codex`; agents under `~/.agents` use `--link agents`; zcode uses `--link zcode`. Run `python3 <repo-path>/ae.py install` (adding the `--link` you need). Success: exit code 0.
> 3. Run `python3 <repo-path>/ae.py doctor --json` again and judge by the JSON: no failures in the `安装状态` (installation) category means success. Failures in the `运行状态` (runtime) category, such as orphan servers or an owned port held by another process, do not affect the install; note them for the report in step 5. Warnings in `summary.warn` (such as missing optional tools `fc-list`/`pdffonts`) do not block use. If installation failures remain, apply that item's `fix_command` and repeat this step; if a `fix_command` contains a destructive flag listed in step 4, ask the user for consent before running it.
> 4. When repairs are needed, run `python3 <repo-path>/ae.py repair` (`--dry-run` shows the plan first). **`--purge`, `--forget-stale`, `--stop-orphans`, `--stop-servers` are all destructive — ask the user for consent before adding any of them.**
> 5. Report back to the user: whether the install succeeded, the check results, and any warnings still unresolved.

## Quick start

**1. Install the skill**

```bash
python3 ae.py install
```

The skill is copied to `~/.claude/skills/annotation-everything` (that copy is the *skill copy*). See **Installation and maintenance** above for the full install, check, repair and uninstall instructions.

**2. Try it on the bundled sample**

```bash
S=skill/annotation-everything/scripts
export AE_HOME=$(mktemp -d)   # keep the review ledger in a temporary directory so your real ledger is untouched
python3 $S/build_viewer.py --report fixtures/sample/report.html --script fixtures/sample/script.md --out /tmp/ae-demo
python3 $S/serve.py /tmp/ae-demo
```

The review page opens in your browser. Add a few annotations, click **Hand off to Agent**, then run this in another terminal:

```bash
python3 $S/notes.py wait /tmp/ae-demo
```

The command prints the annotations you just submitted, which is exactly what the agent receives.

To try English speaker notes, point `--script` at `fixtures/sample/script.en.md` (the same report in English): the notes column then shows the English talking points, durations, transitions and questions, while the interface language stays as it is.

**3. Prepare your own material**

- **Report**: one HTML file in which every page is a `<section class="page" data-page="N" data-key="unique-key">` element.
- **Speaker notes**: one Markdown file split into pages by `### 第 N 页｜标题` headings ("page N | title"), giving the duration, talking points, transition and likely questions for each page. English notes use `### Page N｜Title` with `**Duration**`, `**Talking points**`, `**Transition**` and `**Likely questions**` instead. Both forms may be mixed in one file, and which form is recognized does not depend on the interface language: a Chinese interface reviews English notes and an English interface reviews Chinese notes, with titles and body text kept exactly as written. When a heading cannot be recognized, a page number repeats or a field repeats, the builder reports the line number and the expected form in the current language instead of treating the input as an empty script.

See [references/formats.md](skill/annotation-everything/references/formats.md) for the full format (in Chinese).

<details>
<summary><b>Command reference</b></summary>

| Command | What it does |
|---|---|
| `ae.py install` / `doctor` / `repair` / `uninstall` | Management commands run from the repository root: install the skill copy into `~/.claude/skills/` (`--link codex,agents,zcode` symlinks it into other agents' skill directories); check installation, runtime, optional tools and dev environment (`--json` for structured output); repair what the checks find (prints a plan first, fixes safe items automatically, `--forget-stale`/`--stop-orphans` run the destructive ones explicitly, `--dry-run` only prints); uninstall removes the skill copy and the links pointing at it, keeps the review ledger by default (`--purge` deletes it too; review directories are never touched; a running `serve.py` blocks uninstall unless `--stop-servers` stops it first) |
| `build_viewer.py --report … --script … --out <review-dir>` | Build the review page; `--rebuild <review-dir>` rebuilds it with the recorded options; `--locale auto`, `zh-CN`, or `en` sets the interface language. Omit it and a new review stays in Chinese; a rebuild keeps the saved choice |
| `serve.py <review-dir>` | Start the local server; each review keeps its own dedicated port |
| `serve.py list` / `status` / `forget` | List every review on this machine, show one review's status, remove a ledger entry you no longer need |
| `serve.py check` | Run the server's security self-check |
| `notes.py wait` / `claim` | Wait for a submission, or take one that is already waiting |
| `notes.py show` / `resolve` / `reopen` | List annotations, mark them resolved, reopen them. `show --locale` chooses the reading language and beats `AE_LOCALE` and the review setting; `show --json` still exports the raw values |
| `notes.py done` / `review` / `diff` | Close the current stage, record a review verdict, show page by page what the draft changed |
| `notes.py batches` | Show batch history |
| `notes.py skills` | Show or set the skill catalog and standing skills. `--locale` chooses the output language and beats `AE_LOCALE` and the review setting; scopes accept `全部` / `报告` / `讲稿` / `风格优化` or `all` / `report` / `script` / `style` |
| `check_pptx_fonts.py original.pptx [--pdf converted.pdf]` | Check fonts before and after converting a PPTX file |

</details>

## Security and privacy

| Constraint | How it is enforced |
|---|---|
| Reachable from this machine only | The server listens on `127.0.0.1` only, validates the Host header, and never sends cross-origin resource sharing (CORS) headers |
| No cross-site forged writes | Write requests must come from the same origin as the page and use a JSON content type |
| Only what is needed is exposed | Only `index.html` and the `/api/*` endpoints are served; no other file in the review directory is reachable through the server |
| No third-party Python packages | The runtime uses the Python standard library only; the font check calls the system tools `fc-list` and `pdffonts` and explains how to install them if they are missing |
| No network access | No usage data is sent; external resources referenced by the report are not loaded, and local images and fonts are embedded in the page |
| Your data stays local | Annotations and the review page live in the review directory you choose; the local review ledger `~/.annotation-everything/` records only review IDs, directory paths and ports |

Run `serve.py check` after any change to the server; it verifies each of these constraints.

## Project layout

```text
skill/annotation-everything/       the skill itself, copied as a whole on install
├── SKILL.md                       operating guide for the agent
├── references/formats.md          input and output formats
├── locales/                       Chinese and English messages (zh-CN.json, en.json) and the locale contract
├── scripts/
│   ├── build_viewer.py            builds the review page index.html and review.json
│   ├── i18n.py                    loads the shared catalogs and fills in the chosen language
│   ├── viewer_template.html       review page template (server mode and local file mode)
│   ├── serve.py                   local server: serve / status / list / forget / check
│   ├── notes.py                   annotation command line: wait / show / resolve / done / review and more
│   ├── notes_store.py             annotation storage: atomic writes, file locks, change log
│   ├── ledger.py                  review ledger: review IDs, review directories and dedicated ports
│   ├── match.py                   finds matching sentences between the report and the speaker notes
│   ├── skill_sources.py           discovers skill directories on this machine
│   └── check_pptx_fonts.py        PPTX and PDF font check
└── evals/                         skill evaluation cases (used with skill-creator)
ae.py                             unified management command: install the skill copy, doctor checks it, repair fixes it, uninstall removes it
fixtures/sample/                   synthetic sample (fictional data)
tests/                             unit tests and end-to-end browser tests
tools/
├── setup-dev.sh                   enables the repository's git hooks
└── git-hooks/                     refuse commits on main, pushes to main and deleting main
.github/                           CI, issue templates and the pull request template
docs/                              architecture decision records (ADRs), agent conventions, README images
GLOSSARY.md                        glossary
```

## Development

After cloning, run `tools/setup-dev.sh` once to enable the git hooks.

```bash
python3 -m unittest discover tests                                   # unit tests (standard library only)
uv run --no-project --with playwright python tests/e2e_browser.py   # end-to-end browser tests (uses your local Google Chrome)
python3 skill/annotation-everything/scripts/serve.py check          # server security self-check
python3 ae.py install                                               # sync to ~/.claude/skills/
```

- Tests put the review ledger in a temporary directory (the `AE_HOME` environment variable). When trying things by hand or running skill evaluations, run `export AE_HOME=$(mktemp -d)` first, so throwaway reviews are not registered in your real ledger and do not take ports that belong to real reviews.
- `main` is updated only by squash-merging pull requests: branch from an issue, put `Closes #<number>` in the pull request body, and merge once CI passes; the issue closes automatically on merge. See "分支、issue 与 PR" (branches, issues and pull requests) in [AGENTS.md](AGENTS.md) for the full workflow.
- The repository holds synthetic samples only. Real material, annotations and review directories never go into the repository or the skill directory.
- [GLOSSARY.md](GLOSSARY.md) defines the terms, [docs/adr/](docs/adr/) holds the architecture decision records, and [AGENTS.md](AGENTS.md) describes the collaboration conventions. All three are in Chinese.
