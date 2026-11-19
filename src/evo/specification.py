"""The structured specification contract used by the workflow and Spec Lab."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

from agent.generate import INSTRUCTIONS, SCHEMA, check_draft, implementation_blockers, render_spec
from agent.learning import BASE_PROMPT, digest

from .common import ACTIVE, MAX_TEXT_BYTES, Conflict
from .store import Store, require, utc_now
from .workspaces import git

SPEC_SCHEMA_VERSION = "evo-spec-v1"
SPEC_ARTIFACT_KIND = "specification"
SPEC_DRAFT_INSTRUCTIONS = INSTRUCTIONS.replace(
    "the supplied GitHub issue", "the supplied implementation request"
).replace(
    "ISSUE is the issue report, COMMENT-N is a comment, and\nSRC-N is a repository excerpt.",
    "REQUEST is the local assignment, REQUEST-N is a user clarification, ISSUE is an issue report, "
    "COMMENT-N is a comment, and SRC-N is a pinned repository excerpt."
)


def drafting_instructions(guidance: str) -> str:
    return (
        f"{SPEC_DRAFT_INSTRUCTIONS}\n"
        "Project-approved guidance (subject to the evidence and validation rules above):\n"
        f"{guidance}\n"
        "Supply a JSON draft with the following schema. State blocking product decisions in "
        "unresolved_questions. Acceptance tests describe observable behavior.\n"
        f"{json.dumps(SCHEMA, ensure_ascii=False)}"
    )


def drafting_messages(context: dict, guidance: str) -> list[dict]:
    return [
        {"role": "system", "content": drafting_instructions(guidance)},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]


def _prompt_summary(record: dict | None) -> dict:
    content = record["content"] if record else BASE_PROMPT
    return {
        "id": record["id"] if record else None,
        "version": record["version"] if record else 1,
        "content": content,
        "hash": digest(content),
    }


def spec_prompt_snapshot(store: Store, project_id: str) -> dict:
    active = store.list("prompts", project_id=project_id, role="spec", status="active")
    return _prompt_summary(active[-1] if active else None)


def activate_spec_prompt(store: Store, tx, project_id: str, prompt_id: str, content: str, *,
                         expected: dict, lab_version: str, reviewer: str) -> dict:
    """Update the role registry inside the caller's Spec Lab state transaction."""
    current = spec_prompt_snapshot(store, project_id)
    if expected != current:
        raise Conflict("The active Spec prompt changed. Review the candidate against the active prompt.")
    prompts = store.list("prompts", project_id=project_id, role="spec")
    existing = tx.get("prompts", prompt_id)
    if existing and (existing["project_id"], existing["role"], existing["content"]) != (project_id, "spec", content):
        raise Conflict("The stored Spec prompt differs from the approved version.")
    for prompt in prompts:
        if prompt["status"] == "active" and prompt["id"] != prompt_id:
            tx.put("prompts", prompt["id"], {"status": "superseded"})
    history = [*(existing or {}).get("activations", []), {
        "at": utc_now(), "by": reviewer, "label": "approved in Spec Lab", "replaced_prompt_id": current["id"],
    }]
    return tx.put("prompts", prompt_id, {
        "project_id": project_id,
        "role": "spec",
        "status": "active",
        "content": content,
        "version": existing["version"] if existing else (
            1 if lab_version == "v1" and all(p["version"] > 1 for p in prompts)
            else max((p["version"] for p in prompts), default=1) + 1
        ),
        "spec_lab_version": lab_version,
        "approved_by": reviewer,
        "activation_label": "approved in Spec Lab",
        "activations": history,
    })


def validate_draft(draft: dict, context: dict) -> dict:
    try:
        check_draft(draft, context["sources"])
    except ValueError as exc:
        return {"valid": False, "ready_for_code": False, "status": "INVALID", "errors": [str(exc)], "blockers": []}
    blockers = implementation_blockers(draft)
    return {"valid": True, "ready_for_code": not blockers, "status": "BLOCKED" if blockers else "DRAFT",
            "errors": [], "blockers": blockers}


def _source_excerpt(workdir: Path, commit: str, ref: dict, number: int) -> dict:
    path = ref["path"]
    start, lines = ref.get("start", 1), ref.get("lines", 30)
    if (not isinstance(path, str) or not path or len(path) > 600 or any(c in path for c in "\\:\x00\r\n")
            or PurePosixPath(path).is_absolute()
            or any(p.lower() in {"..", ".git", ".evo"} for p in PurePosixPath(path).parts)):
        raise ValueError("Use a repository-relative Git path with forward slashes.")
    if type(start) is not int or type(lines) is not int or start < 1 or not 1 <= lines <= 100:
        raise ValueError("Use a positive start line and between 1 and 100 lines.")
    path = PurePosixPath(path).as_posix()
    object_name = f"{commit}:{path}"
    if git(workdir, "cat-file", "-t", object_name, check=False) != "blob":
        raise ValueError(f"No file {path!r} at the pinned specification commit.")
    if int(git(workdir, "cat-file", "-s", object_name)) > MAX_TEXT_BYTES:
        raise ValueError(f"File {path!r} exceeds the text evidence limit.")
    content = git(workdir, "cat-file", "blob", object_name, trim=False)
    excerpt = content.splitlines()[start - 1:start - 1 + lines]
    if not excerpt or "\0" in content or not any(line.strip() for line in excerpt):
        raise ValueError(f"Use a nonempty text excerpt from {path!r}.")
    text = "\n".join(excerpt)
    if len(text) > 4000:
        raise ValueError(f"Choose fewer lines from {path!r}; excerpts are limited to 4,000 characters.")
    end = start + len(excerpt) - 1
    return {"id": f"SRC-{number}", "kind": "repository", "commit": commit, "path": path, "start": start,
            "end": end, "url": f"git:{object_name}#L{start}-L{end}", "text": text}


class Specification:
    def __init__(self, store: Store, artifacts, lock):
        self.store = store
        self.artifacts = artifacts
        self.lock = lock

    def _scope(self, agent_id: str) -> tuple[dict, dict]:
        agent = require(self.store, "agents", agent_id)
        run = require(self.store, "runs", agent["run_id"])
        if agent["role"].lower() != "spec" or run.get("phase") != "spec":
            raise PermissionError("Publishing and validating specifications require the active Spec role.")
        return agent, run

    def _user_origin(self, run: dict, event: dict | None) -> dict | None:
        if not event or event["kind"] != "message" or event["agent_id"] != run.get("root_agent_id"):
            return None
        data = event["data"]
        if data.get("role") != "user" or data.get("sender_id"):
            return None
        if data.get("visible") is True:
            return event
        routing = data.get("routing") or {}
        origin_id, origin_run_id = routing.get("source_event_id"), routing.get("source_run_id")
        if not origin_id or not origin_run_id:
            return None
        supervisor = self.store.get("runs", origin_run_id)
        if not supervisor or supervisor.get("kind") != "supervisor":
            return None
        origin = self.store.events_by_id([origin_id], run["project_id"], origin_run_id).get(origin_id)
        if (origin and origin["kind"] == "message"
                and origin["agent_id"] == supervisor.get("root_agent_id") == routing.get("source_agent_id")
                and origin["data"].get("role") == "user" and origin["data"].get("visible") is True
                and not origin["data"].get("sender_id")):
            return origin
        return None

    def _event_source(self, run: dict, event_id: str, counts: dict[str, int]) -> dict:
        event = self.store.events_by_id([event_id], run["project_id"], run["id"]).get(event_id)
        data = event["data"] if event else {}
        origin = self._user_origin(run, event)
        if origin:
            counts["request"] += 1
            return {"id": f'REQUEST-{counts["request"]}', "kind": "request", "event_id": event_id,
                    "origin_event_id": origin["id"], "url": f'evo:event:{origin["id"]}', "text": data.get("text", "")}
        issue = data.get("result")
        if (event and event["kind"] == "tool.finished" and data.get("name") == "github.issue"
                and isinstance(issue, dict) and isinstance(issue.get("body"), str)
                and isinstance(issue.get("url"), str) and issue.get("title")):
            if counts["issue"]:
                raise ValueError("Cite a single issue report per specification.")
            counts["issue"] += 1
            return {"id": "ISSUE", "kind": "issue", "event_id": event_id, "url": issue["url"],
                    "text": f'{issue["title"]}\n\n{issue["body"]}'}
        raise ValueError("Cite a visible user message or a successful github.issue event from this run.")

    def _context(self, run: dict, sources: list[dict] | None) -> dict:
        commit = run.get("spec_base_commit") or run["base_commit"]
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("Specification evidence requires a pinned Git commit.")
        project = require(self.store, "projects", run["project_id"])
        request = run.get("spec_request")
        if not request and run.get("root_agent_id"):
            request = require(self.store, "agents", run["root_agent_id"]).get("task")
        if not request:
            raise ValueError("This run has no saved implementation request.")
        evidence = [{"id": "REQUEST", "kind": "request", "url": f'evo:run:{run["id"]}', "text": request}]
        if sources is not None and (not isinstance(sources, list) or len(sources) > 12):
            raise ValueError("Provide at most 12 evidence references.")
        counts = dict(request=0, repository=0, issue=0)
        request_events = run.get("spec_request_event_ids", [])
        for event_id in request_events:
            evidence.append(self._event_source(run, event_id, counts))
        for ref in sources or []:
            if not isinstance(ref, dict):
                raise ValueError("Each evidence reference needs a Git path or a same-run event ID.")
            if "path" in ref and not ref.keys() - {"path", "start", "lines"}:
                counts["repository"] += 1
                evidence.append(_source_excerpt(Path(run["workdir"]), commit, ref, counts["repository"]))
            elif (set(ref) == {"event_id"} and isinstance(ref["event_id"], str)
                  and re.fullmatch(r"[a-f0-9]{32}", ref["event_id"])):
                if ref["event_id"] not in request_events:
                    evidence.append(self._event_source(run, ref["event_id"], counts))
            else:
                raise ValueError("Each evidence reference needs a Git path or a same-run event ID.")
        context = {"repo": project["name"], "title": run["title"], "commit": commit, "sources": evidence,
                   "request_revision": len(request_events)}
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 220_000:
            raise ValueError("Specification evidence exceeds 220 KB; reduce the referenced excerpts.")
        return context

    def context(self, agent_id: str, sources: list[dict] | None = None) -> dict:
        _, run = self._scope(agent_id)
        return self._context(run, sources) | {"prompt": run.get("spec_prompt") or _prompt_summary(None)}

    def validate(self, agent_id: str, draft: dict, sources: list[dict] | None = None) -> dict:
        return validate_draft(draft, self.context(agent_id, sources))

    def revise_request(self, agent_id: str, event_id: str) -> dict:
        """Record an explicitly scoped user update and invalidate outstanding Spec drafts."""
        agent = require(self.store, "agents", agent_id)
        with self.lock:
            run = require(self.store, "runs", agent["run_id"])
            if run.get("root_agent_id") != agent_id:
                raise PermissionError("Only the main agent can revise the implementation request.")
            if not isinstance(event_id, str) or not re.fullmatch(r"[a-f0-9]{32}", event_id):
                raise ValueError("Select the same-run user message containing the scope change.")
            source = self._event_source(run, event_id, dict(request=0, issue=0))
            if source["kind"] != "request":
                raise ValueError("Select the same-run user message containing the scope change.")
            revisions = run.get("spec_request_event_ids", [])
            phase = run.get("phase")
            if event_id not in revisions:
                revisions = [*revisions, event_id]
                updates = {
                    "spec_request_event_ids": revisions, "specification_artifact_id": None,
                    "specification_status": None,
                }
                reopen = phase not in {"triage", "spec"} and not (
                    phase == "blocked" and run.get("blocked_from") == "spec"
                )
                if reopen:
                    workers = self.store.list("agents", run_id=run["id"])
                    if any(a["id"] != agent_id and a.get("status") in ACTIVE for a in workers):
                        raise Conflict("Pause or finish the workflow workers before revising the request.")
                    workdir = Path(run["workdir"])
                    base = run.get("implementation_commit") or run["base_commit"]
                    if git(workdir, "rev-parse", "HEAD") != base or git(workdir, "status", "--porcelain"):
                        raise Conflict("Publish or restore the implementation worktree before revising the request.")
                    updates.update(phase="spec", workflow_round=run.get("workflow_round", 1) + 1,
                                   spec_base_commit=base, previous_implementation_commit=run.get("implementation_commit"),
                                   tested_commit=None, verified_commit=None, writer_agent_id=None, blocked_from=None)
                self._context(run | updates, None)
                self.store.put("runs", run["id"], updates)
                self.store.event(run["id"], agent_id, "spec.request_revised", {
                    "request_event_id": event_id, "request_revision": len(revisions),
                })
                if reopen:
                    self.store.event(run["id"], agent_id, "workflow.phase", {"previous": phase, "phase": "spec"})
                    phase = "spec"
            return {"request_event_id": event_id, "request_revision": len(revisions), "phase": phase}

    def publish(self, agent_id: str, draft: dict, sources: list[dict] | None = None) -> dict:
        with self.lock:
            return self._publish(agent_id, draft, sources)

    def _publish(self, agent_id: str, draft: dict, sources: list[dict] | None) -> dict:
        _, run = self._scope(agent_id)
        context = self._context(run, sources)
        validation = validate_draft(draft, context)
        if not validation["valid"]:
            raise ValueError(validation["errors"][0])
        prompt = run.get("spec_prompt") or _prompt_summary(None)
        identity = {
            "schema_version": SPEC_SCHEMA_VERSION,
            "run_id": run["id"],
            "workflow_round": run.get("workflow_round", 1),
            "base_commit": context["commit"],
            "request_revision": context["request_revision"],
            "prompt": {key: prompt[key] for key in ("id", "version", "hash")},
        }
        content = json.dumps({**identity, "status": validation["status"], "sources": context["sources"],
                              "draft": draft}, ensure_ascii=False, indent=2)
        artifact = self.artifacts.write(run["id"], agent_id, "specification.json", content, SPEC_ARTIFACT_KIND)
        rationale = "BLOCKED — see unresolved decisions and missing fields." if validation["blockers"] else "DRAFT"
        markdown = render_spec(draft, context, "workflow", rationale)
        if validation["blockers"]:
            markdown += "\n## Implementation blockers\n\n" + "".join(f"- {b}\n" for b in validation["blockers"])
        preview = self.artifacts.write(run["id"], agent_id, "specification.md", markdown, "markdown")
        def register(tx):
            tx.put("spec_publications", artifact["id"], {
                **identity, "agent_id": agent_id, "content_hash": sha256(content.encode("utf-8")).hexdigest(),
                "markdown_artifact_id": preview["id"],
            })
            tx.put("runs", run["id"], {
                "specification_artifact_id": artifact["id"], "specification_status": validation["status"],
            })
        self.store.transact(register)
        result = {**validation, **identity, "artifact_id": artifact["id"], "markdown_artifact_id": preview["id"]}
        self.store.event(run["id"], agent_id, "spec.published", result)
        return result

    def verify_handoff(self, run: dict, agent_id: str, artifact_ids: list[str]) -> None:
        artifacts = [require(self.store, "artifacts", artifact_id) for artifact_id in artifact_ids]
        contracts = [artifact for artifact in artifacts if artifact["kind"] == SPEC_ARTIFACT_KIND]
        if not contracts and not (run.get("spec_schema_version") or run.get("specification_artifact_id")):
            return  # Legacy runs accepted a free-text specification.
        if len(contracts) != 1:
            raise ValueError("Attach exactly one structured specification artifact from spec.publish.")
        artifact = contracts[0]
        publication = self.store.get("spec_publications", artifact["id"])
        prompt = run.get("spec_prompt") or _prompt_summary(None)
        if (not publication or artifact["run_id"] != run["id"] or publication["run_id"] != run["id"]
                or publication["agent_id"] != agent_id
                or publication["workflow_round"] != run.get("workflow_round", 1)
                or publication["schema_version"] != SPEC_SCHEMA_VERSION
                or publication["base_commit"] != (run.get("spec_base_commit") or run["base_commit"])
                or publication["prompt"] != {key: prompt[key] for key in ("id", "version", "hash")}):
            raise ValueError("Attach this Spec agent's publication for the current run, round, base and prompt.")
        if publication["request_revision"] != len(run.get("spec_request_event_ids", [])):
            raise ValueError("The implementation request was revised. Publish a specification for the updated request.")
        content = self.artifacts.read(artifact["id"])["content"]
        if sha256(content.encode("utf-8")).hexdigest() != publication["content_hash"]:
            raise ValueError("The structured specification artifact changed after publication.")
        envelope = json.loads(content)
        sources = envelope["sources"]
        if any(source.get("commit") != publication["base_commit"]
               for source in sources if source["kind"] == "repository"):
            raise ValueError("Repository evidence must match the pinned specification commit.")
        result = validate_draft(envelope["draft"], envelope)
        if not result["ready_for_code"] or envelope["status"] != "DRAFT":
            raise ValueError("Specification blocks implementation: " + " ".join(result["blockers"] + result["errors"]))
        if artifact["id"] != run.get("specification_artifact_id"):
            raise ValueError("Attach the current publication. A newer specification superseded this draft.")
