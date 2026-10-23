# Research-to-prototype design notes

The supplied PDFs are reference data. They do not authorize execution, network access, policy changes, or live self-modification. The user's pasted implementation request determines the prototype's scope and guardrails. This document records design adaptations, not a replication of the papers or their performance claims.

## AlphaEvolve (Google DeepMind, supplied PDF, May 14, 2025)

All nine supplied scan pages were visually inspected. The substantive architecture appears on pages 2–4; pages 7–9 mainly contain acknowledgements, links, and site navigation.

- Pages 2–3 separate model-generated code proposals from automated evaluators that execute and score programs. A program database retains candidates and results for later proposals. **Adaptation:** a proposal contains changes, a hypothesis, and evidence IDs only. pytest exit codes and parsed test cases determine observed outcomes. A model or optional Jev review cannot declare a failed run passed.
- Page 4 describes inspectable code and robust verification of functional correctness. **Adaptation:** an exact allowlist bounds source changes; test, assertion, CI, dependency, authentication, and policy surfaces are protected. Candidates are copied into independent workspaces, and their diffs and test evidence remain reviewable. Passing a candidate does not change the original PR assessment.
- AlphaEvolve's algorithm-discovery population, model ensemble, production deployments, and reported gains are not implemented or claimed here. The prototype uses a frozen provider interface and a small bounded repair loop.

## A-EVOLVE / “Position: Agentic Evolution is the Path to Evolving LLMs”

Source: supplied `2602.00359v2.pdf`, especially Sections 2–3, pages 3–5. The paper distinguishes model parameters from persistent non-parametric artifacts and proposes Diagnose → Plan → Update → Verify with an explicit commit/no-op gate. Solve-time state remains read-only, while evolution has its own evidence and validation process.

**Adaptation:** current-run self-correction is separate from offline improvement. `evolution_memory.py` writes redacted, versioned run, review, and feedback events with source identity and configuration. It can export an offline dataset for a future GEPA evaluator. It does not run GEPA, train weights, modify prompts, write SKILL.md rules, or activate memories. A future skill promotion is represented by a JSON plan requiring a named approver, held-out evaluation references, a small canary, and a rollback version. Operator activation and authenticated approver verification are deliberately outside this recorder.

The paper permits evolving validation assets in its general framework. This prototype deliberately protects tests and validators during a repair run, following the user's stricter governance requirements. The paper's experimental gains do not constitute prototype validation.

## FlashEvolve / “Accelerating Agent Evolution with Asynchronous Stage Orchestration”

Source: supplied `2605.08520v1.pdf`, especially Sections 3.1–3.4, pages 4–5. FlashEvolve overlaps evolution stages with queues, tracks artifact-pool versions, and supports guarded or reflective handling of stale artifacts. Its speculative artifacts still require full evaluation before becoming confirmed results.

**Adaptation:** keep source/spec/profile/environment/policy identities attached to evidence and offline learning records. A changed identity invalidates the old verification result instead of allowing a stale passing assessment to authorize QA. The promotion-plan helper rejects mismatched evaluated source identities. Partial results and proposed patches cannot become trusted passes. The prototype does not implement distributed evolution workers, speculative activation, reflective live updates, or throughput optimization; its asynchronous REST lifecycle is a service interface, not FlashEvolve replication. Reported 3.5×/4.9× proposal throughput in the paper is a research result, not a measured property of this prototype.

## EVO project vision and Test Agent PRD

Source: supplied `Q_ - Project Vision Draft (1).pdf` (Project Goals) and `Test Agent MVP PRD.pdf`. EVO coordinates SDLC phases while keeping responsibilities and dependencies visible. The vision describes testing the system on the team's project with humans reviewing suggestions and making final decisions.

**Adaptation:** one Test Agent orchestrator owns Verify & Test; environment blockers route to an environment owner, specification conflicts to the spec owner, and actionable regressions to the Code Agent/engineer. Human QA decisions bind the exact verification identity and artifact manifest. The project vision's longer-term autonomy does not override the explicit prohibition on autonomous merge or deployment.

## Evidence and validation limits

- The offline demo is deterministic and does not access GitHub or external model APIs. Live providers must be explicitly configured; missing providers preserve evidence and escalate.
- Source-scope and syntax checks are not proof that arbitrary candidate Python is safe. Untrusted repositories require the isolated execution backend; trusted local fixtures are the only appropriate use of the local execution mode.
- JSONL event hashes detect accidental edits to individual records. They are not a cryptographically signed, tamper-proof audit log, and the local recorder's thread lock is not a multi-process database.
- Secret redaction covers common credentials and token forms, not every possible private datum. Callers must minimize collected data and apply production retention/access controls before a wider pilot.
- Offline datasets leave train/validation splits unassigned. A future evaluator must split by project/task or temporal boundary, preserve held-out cases, measure regressions and cost, and prevent leakage before proposing a skill update.
