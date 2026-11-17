# Talking to the main agent while it works

Each repository has one main agent. One part of it, the **coordinator**, directs the team (Spec, Code, Test, QA, helpers) and is the only part that changes the work. You can write to it at any time.

## What happens when you write mid-assignment

1. **Accepted.** Your message is saved, shown in the conversation and queued for the coordinator right away. If you refresh or switch repositories, nothing is lost.
2. **Quick reply.** If the coordinator is busy (in a model call, running a tool, or waiting for its team), a lightweight responder answers you as the same main agent. It works from the recorded state: the run, the team and their statuses, workflow handoffs and findings, recent events and the conversation. It can also look up history, specific events and memory pages, all read-only. The reply is labelled *while working*.
3. **Read by the coordinator.** `agents.wait` returns as soon as a message arrives, so the coordinator picks it up at its next step without waiting for the team. Your message then shows *Read by <name>*. The coordinator is told about any quick reply already sent so it doesn't repeat it.
4. **Applied.** Only the coordinator can act on an instruction (redirecting Code, adding a task, stopping a direction). The responder may say an instruction was *received and passed on*, never that it was *applied*. The coordinator confirms with `chat.reply` once it has actually done it.

Identical wording in answer to two different questions is saved twice; only a repeated delivery of the same answer to the same message is dropped. A quick reply counts as an answer only once it is saved.

Answering a question never cancels or restarts work. The team keeps running throughout.

## What you see

- **Replies** to you are normal messages.
- **Updates** are compact lines with a flag icon:
  - phase changes, from the workflow records;
  - failed verification, from Test or QA handoffs;
  - high or critical findings;
  - a team member stopping with an error.

  They're batched over a couple of seconds and deduplicated, and each one links to the event that caused it. There are no "still working" messages.
- **Questions and blockers** the coordinator raises with `chat.notify` are highlighted.
- **Working text.** The coordinator's text next to tool calls is private narration. It shows as a faded preview while it streams and becomes a message only if it turns out to be the reply.

## Remaining delay

Measured live (`gpt-6.1-terra`, Responses API):
- Scheduling the quick reply took about 10 ms.
- The model's first text took 1.7–5.4 s, depending on whether the responder looked anything up.
- The coordinator read the message within 10–730 ms of `agents.wait` waking.

Nearly all of the remaining delay is model time. While the coordinator is inside a single long model call, it can't read your message until that call ends; the quick reply covers that gap.

## Isolation

The conversation, queue and replies are tied to the repository's run, so switching repositories while a request is pending can't mix replies. Repositories work independently; sends to the same repository are serialized.

## Tools added for the coordinator

- `chat.reply(text)` answers the user now.
- `chat.notify(text, kind)` raises progress, a blocker or a question.

Only the root main agent can use them; other agents report to their assigning agent.
