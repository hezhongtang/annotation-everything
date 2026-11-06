# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body-file <body-file>`. Store multi-line bodies in a UTF-8 file.
- **Read an issue**: `gh issue view <number> --json number,title,body,labels,comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Make an issue a sub-issue of a parent**: `gh issue create --parent <parent> ...`, or `gh issue edit <parent> --add-sub-issue <child>` afterwards (`gh` 2.94+). Older `gh`: `gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>` (database id, as in **Blocking** below). Without sub-issues, put `Part of #<parent>` at the top of the child body.
- **Comment on an issue**: `gh issue comment <number> --body-file <body-file>`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: implemented issues are closed by merging a PR whose body says `Closes #<number>` — never close them by hand. Close manually only for `wontfix`, duplicates or invalid reports: `gh issue close <number> --reason "not planned" --comment "..."`.

Infer the repo from `git remote -v`; `gh` does this automatically when run inside a clone.

## Issue lifecycle: issue → branch → PR → merge

Every change reaches `main` through a squash-merged PR. The repository is public. The active GitHub ruleset `protect-main` requires a PR, squash merging and both required CI checks, and blocks deletion and force pushes to `main`; `protect-release-tags` blocks changes to and deletion of `v*` tags. Local hooks (`tools/setup-dev.sh`) also refuse commits on `main`, pushes to `main` and deleting `main`, and only let local `main` move to commits already on `origin/main`. Use an administrator bypass only with the user's explicit approval.

1. **Create** from a template (`.github/ISSUE_TEMPLATE/`), labelled `needs-triage`.
2. **Triage**: once the acceptance criteria and compatibility notes are complete, swap `needs-triage` for `ready-for-agent` (or `ready-for-human`). Missing details → `needs-info`. Agents pick up only `ready-for-agent` issues.
3. **Claim and branch**: `gh issue edit <n> --add-assignee @me`, then `gh issue develop <n> --name <type>/<n>-<slug> --base main --checkout`. This links the branch to the issue in GitHub's sidebar. `<type>` matches the commit type (`feat`, `fix`, `docs`, `chore`, …). One issue per branch and per PR; for a chain of dependent issues, open one PR per issue and merge them in order.
4. **Implement and open the PR** with `Closes #<n>` in the body (the PR template has the slot). Follow AGENTS.md's development and PR review workflow: run quick related unit tests and configured lint and format checks, then open the PR and hand off. State implementation status, quick-check results and pending verification; leave acceptance criteria that require full validation unticked. Lint and formatting tools are not configured yet and will be introduced in a separate PR.
5. **Review when the user invokes agents**: perform the full code review and run `python3 verify.py run --group all --phase pr --pr <n>`. Fix findings on the same branch, validate the fixes and follow CI through the latest commit. Development agents do not start this stage automatically or wait for it before handing off.
6. **Merge** after full review, the applicable PR verification gate and CI pass: squash only. Documentation-only exemptions follow AGENTS.md and `docs/verify/selection-strategy.md`; all other PRs execute full validation. The squash commit uses the PR title and body, so `Closes #<n>` lands on `main` and closes the issue. The branch is deleted automatically.
7. **Release**: PRs that bump the `VERSION` constant in `skill/annotation-everything/scripts/serve.py` go into the matching milestone (e.g. `v0.9.0`). After merging into `main` and passing CI on that commit, `.github/workflows/release.yml` runs `tools/release.py` to create the annotated tag and GitHub Release, fill in missing historical Releases, and close the milestone when it has no open items. Let the workflow create `v*` tags; versions must increase. After fixing a workflow failure, run `gh workflow run Release` to fill in only the missing parts; a new tag still requires successful CI on the target commit on `main`.

Parent issues with sub-issues close when their last child's PR merges: that PR carries both `Closes #<child>` and `Closes #<parent>`. This is the only case where one PR closes more than one issue.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`.
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (see **Make an issue a sub-issue of a parent**). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body-file <answer-file>`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
