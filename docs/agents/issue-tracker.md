# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues on `waiff/sreality`. Use the `gh` CLI for all operations.

**The repo is public: every issue is world-readable.** Keep tokens, DB URLs, operator-private rulings and personal data out of issue titles, bodies and comments.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --json number,title,body,state,labels,comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically when run inside a clone.

## Always pass `--json` to a view

In this repo a plain `gh issue view <n>` or `gh pr view <n>` (with or without `--comments`) exits 1 and prints only a "Projects (classic) is being deprecated" GraphQL error: the default field set asks for `projectCards`, which GitHub no longer serves. Naming the fields with `--json` skips that query and works.

`gh pr edit` fails the same way and changes nothing. If an edit exits 1 with that error, use the REST call instead:

- **Add a label**: `gh api -X POST repos/{owner}/{repo}/issues/<n>/labels -f "labels[]=<label>"`
- **Remove a label**: `gh api -X DELETE repos/{owner}/{repo}/issues/<n>/labels/<label>`
- **Edit a body**: `gh api -X PATCH repos/{owner}/{repo}/issues/<n> -F body=@<file>` (`pulls/<n>` for a PR)

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --json number,title,body,state,labels,author,comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, the REST label calls above, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42 --json number` and fall back to `gh issue view 42 --json number`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --json number,title,body,state,labels,comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
- **A map may live in another repo.** The AI-CRM program's map and its tickets live in a private planning repo (the owner's ruling of 2026-10-04), because its tickets weigh legal and security questions that do not belong in a public tracker. The owner gives the map's URL when invoking `/wayfinder`. Take `<owner>/<repo>` from that URL, pass `-R <owner>/<repo>` to every `gh` call for that map, and keep its research files there, never in this repo.
