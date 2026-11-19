# Working in the EVO workspace

Open **Dashboard** for a project's workflows, agents and GitHub activity. Select a workflow to see
its phase, branch, implementation commit, handoffs and findings. Use project chat to ask the
supervisor about several workflows, or send instructions to a single workflow. See [talking to the
main agent](main-agent.md).

## Concurrency and worktrees

Use **Workflow limits** on the dashboard to change these project settings.

| Project setting | Default | Effect |
| --- | --- | --- |
| Maximum concurrent workflows (`max_concurrent_workflows`) | 3 | Accepts 1–20. Workflows wait their turn when all slots are in use. |
| Project token cap (`max_project_tokens`) | Unset | Optional positive integer. Limits total recorded project usage and reservations. |
| Project cost cap (`max_project_cost_usd`) | Unset | Optional positive USD amount. Limits total recorded project cost and reservations. |

Project caps include supervisor and production workflow model calls across recorded project
history. EVO counts past usage, pending reservations and unresolved charges. It checks that the
next model call can fit before sending it. Existing per-run limits still apply. Workflow settings
can be read or changed at `/api/projects/{project_id}/workflow-settings` using `GET` or `PATCH`.

An assignment takes its own snapshot and receives its own branch and implementation worktree when it
is queued. Paused workflows and workflows waiting for input free their slots once their running
tasks finish stopping. A message sent while a workflow is paused is saved for the coordinator to
pick up after resume. Resuming a paused workflow joins the queue again. The queue persists across
server restarts. Resume interrupted workflows explicitly.

Git worktrees start from the connected checkout's current commit and include eligible working-tree
changes. Each workflow has one Code writer. Test and QA work from separate checkouts of the recorded
implementation commit.

Workflow commits stay on their local branch by default. After verification, an explicit request can
publish that branch as a draft GitHub PR if authenticated Git and `gh` access are available. Review
the branch and its verification evidence before integrating it using Git. Resolve any merge
conflicts in the integration checkout and rerun verification against the resulting commit. A new
checkout requires the source repository's existing merge conflicts to be resolved first.

## Agent traces

Expand an agent card and select **View full thread** to open the read-only trace. **Transcript**
shows saved model messages in order, including tool calls and results. **Activity** shows execution
events, including nested Python and host tool calls and links to saved artifacts when available.
Search and filter the loaded pages, or use **Download trace** to export all saved transcript and
execution pages plus available referenced artifacts as JSON. The browser export is limited to
64 MiB. Exports can contain repository contents and tool output, so review them before sharing.

New runs save an append-only transcript in SQLite alongside the execution log. Compacting model
context leaves the saved transcript intact. Reasoning entries appear only when the provider returns
readable reasoning fields, such as Chat Completions `reasoning_content` or Responses API reasoning
summaries and text. Some turns have no such fields. Encrypted provider state is omitted. Migrated
histories carry a partial-history notice because earlier compaction may have discarded messages.
Some older tool events have limited call linkage.

The trace endpoints accept `GET` requests at
`/api/projects/{project_id}/runs/{run_id}/agents/{agent_id}/trace/transcript` and the corresponding
`/trace/events` route. Use the returned `next_cursor` as `after` for the next page. The project, run
and agent must match.

## Specifications

New workflows use the same drafting instructions and validator as the **Workflow** harness in **Spec
Lab**. The Spec agent gathers the assignment, pinned repository excerpts and eligible events from
its run through `spec.context`. It validates and publishes `specification.json` and a readable
`specification.md` through `spec.validate` and `spec.publish`. Each requirement traces to cited
evidence and observable acceptance behavior.

Before handing the JSON publication to Code, the workflow checks its run, round, base commit, prompt
version and content hash. A `BLOCKED` specification records unresolved decisions and stays in Spec.
A valid `DRAFT` is eligible for the Code handoff. Workflows bound to the Test-stage verifier also
use an operator-approved document and executable test mapping. See [Test-stage
tools](test-stage-tools.md).

If you change the implementation scope, tell the coordinator which requirement changed. Once it
records the scope change, the previous specification is superseded. Changes after the Spec phase
require the other agents to stop and the implementation worktree to be clean before Spec can reopen.

Spec Lab uses frozen evidence for reproducible comparisons. The **Workflow** harness supports
fixture and live model runs. Bundled recordings cover the mini-swe-agent and Prime-RLM harnesses. To
activate a prompt for production workflows, run a paired live benchmark on the **Workflow** harness
against the active prompt. Both versions must complete the full frozen benchmark. The candidate
must pass the regression and blocker checks and receive approval from a named reviewer. Fixture
and recorded promotions rehearse the approval process within Spec Lab. A workflow pins its prompt
when it is created. Production promotion or rollback applies to newly created workflows. Legacy
workflows can still finish with their original handoff format. See [governed
self-evolution](GOVERNED_EVOLUTION.md).

## GitHub activity

Polling starts disabled for each project. Open **Repository activity settings** in **Dashboard**,
select a repository in `owner/name` form or use the connected `github.com` origin, then turn on
**Enable repository activity**. The server reads `GH_TOKEN` or `GITHUB_TOKEN` from its environment,
or uses a `github.com` credential already available to `gh`. The credential needs read access to the
monitored repository, issues, pull requests and checks.

The initial baseline records recent state quietly. After that, EVO looks for issue and PR changes,
comments, reviews, completed checks, and changes to watched branch heads and their commits. Coverage
includes the default branch, up to three additional branches and up to eight recently updated open
PRs. Bot authors and configured ignored accounts are filtered.

Polls run about every five minutes, use conditional requests and back off at GitHub rate limits.
Select **Refresh repository activity** to request a poll while monitoring is enabled. The activity
inbox, suggestions and poll checkpoints persist in SQLite. **Pause activity checks** stops polls.
After you clear it, the next poll can catch up from the saved checkpoints. The project API exposes
`GET` and `PATCH` at `/api/projects/{project_id}/activity/settings` and `POST` at
`/api/projects/{project_id}/activity/poll`.

The default mode, `suggest`, groups related activity and posts suggestions in project chat. In
**Needs attention**, open a suggestion to review its sources and edit the workflow instructions
before selecting **Start workflow**. `local_auto` is a saved mode for future local automation. In
this release it also produces suggestions and waits for your direction. The monitor uses read-only
GitHub calls. You review and integrate local work yourself.

Polling runs in the EVO server process. The browser can be closed while the server is running. If
the computer sleeps or EVO stops, polling resumes on the next server start subject to the saved
settings and cursors. For continuous operation, keep one EVO process running on an always-on machine
with access to the checkout, data directory and credentials. The local UI and API bind to
`127.0.0.1` by default. Keep the service on loopback because agents can run local commands and the
workspace can expose repository data.

## Upgrade and rollback

1. Stop EVO and any other process using its data directory. Back up the full directory at `.evo/` or
   `EVO_DATA_DIR`, including the SQLite database and any SQLite sidecar files, artifacts and
   worktrees. Preserve connected checkouts and local workflow branches separately.
2. Start the new release on the same data directory. Startup migrates the database and recovers
   retained legacy trace entries in place. Confirm project chat, workflow history and recent
   artifacts before resuming work or enabling GitHub polling.
3. To return to an older release, stop EVO and preserve the upgraded data directory and workflow
   branches so you can recover post-upgrade work. Restore the pre-upgrade backup with the old
   release. Restore the original data path if you need existing worktree paths to resolve. Keep
   separate backups for the two releases.

## Development checks

Run from the repository root with the development dependencies installed. Give each concurrent test
command its own `--basetemp` path.

```powershell
uv sync --frozen
uv run pytest tests/test_workspace_acceptance.py --basetemp=.pytest-tmp-workspace
uv run pytest --basetemp=.pytest-tmp-full
npm --prefix frontend ci
npm --prefix frontend run build
```

The workspace acceptance suite uses local fixtures and scripted responses. It exercises project and
workflow boundaries, the persisted trace, the Spec handoff and GitHub suggestions without live
provider or GitHub credentials.
