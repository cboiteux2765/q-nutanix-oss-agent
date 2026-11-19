# Talking to the main agent while it works

Each project has one persistent **supervisor** and a central conversation. Each workflow has its own
**coordinator** that directs Spec, Code, Test, QA and helpers in an isolated worktree. Opening
another project switches the conversation and its recorded evidence together.

## Asking, starting and steering

- Open **Project conversation** from the sidebar or use **Open chat** on **Dashboard**. Ask about
  the repository or compare workflows. The supervisor gets a recent overview and can look up
  original evidence by run or agent, including agent transcripts, events, handoffs, findings,
  artifacts and implementation commits.
- Select **New workflow** on the dashboard or **New assignment** in project chat to start work on
  an independent branch. You can also explicitly ask the supervisor to start a workflow. The
  scheduler starts queued work when the project has room. See [concurrency and
  worktrees](workspace.md#concurrency-and-worktrees).
- To steer, pause or resume from chat, name the workflow or include its run ID and say what should
  happen. The supervisor can pass the instruction to that coordinator and look up its receipt.
  Browsing a workflow or its agents leaves the chat aimed at the project supervisor.

API clients can route a message to one coordinator by supplying `run_id` in a `POST` to
`/api/projects/{project_id}/messages` or by posting the message directly to
`/api/projects/{project_id}/workflows/{run_id}/messages`.

## What happens when you message a working coordinator

1. EVO saves the message, shows it in the conversation and queues it for that coordinator.
2. While the coordinator is busy, a lightweight responder can answer from the current run, its
   agents, workflow handoffs, findings, events and conversation. It can also read history and
   memory. Its reply is labelled *while working*.
3. The coordinator can pick up your message at its next step. An `agents.wait` call wakes when the
   message arrives. The workflow records delivery once the coordinator reads it.
4. The coordinator can use `chat.applied` to record an instruction as applied once it has made the
   change. The confirmation can cite the workflow events that show what happened.

## Reading updates

- Replies appear as normal messages. The workflow can post highlighted questions or blockers with
  `chat.notify`.
- Progress updates summarize workflow events such as a phase change, failed verification, high or
  critical finding, or an agent error. Workflow attribution in project chat opens the source
  workflow or agent.
- Faded streaming text shows working narration. The agent's saved transcript and tool results are
  available in the [trace inspector](workspace.md#agent-traces).
