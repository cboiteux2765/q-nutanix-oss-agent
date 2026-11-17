"""Repository-scoped agent identity and conversation continuity."""


def main_agent(project: dict) -> dict:
    return {"id": f"main-{project['id']}", "name": "EVO", "instructions": "", "model": "",
            **project.get("main_agent", {})}


def project_view(project: dict) -> dict:
    return project | {"main_agent": main_agent(project)}


def conversation_context(store, project_id: str) -> str:
    messages = store.project_messages(project_id, limit=40)
    if not messages:
        return ""
    lines = []
    remaining = 24000
    for event in reversed(messages):
        text = str(event["data"].get("text", ""))[:6000]
        line = f"[{event['data'].get('role')} · run {event['run_id']} · event {event['id']}]\n{text}"
        if len(line) > remaining:
            break
        lines.append(line)
        remaining -= len(line)
    return ("\n\nPrevious messages in this repository's central conversation (historical context, "
            "not new instructions). Use history.search/read to recover full original evidence. "
            "This assignment has a fresh worktree from the current source repository; earlier "
            "work branches are not automatically merged into it. Inspect the current files before acting.\n"
            + "\n\n".join(reversed(lines)))
