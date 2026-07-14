---
name: improve
description: Run the instructor/worker improvement loop — Fable 5 instructs and grades, Sonnet 5 workers implement. Usage — /improve <area or goal> (e.g. "/improve audit.py fix coverage", "/improve frontend builders"); with no argument the instructor picks the highest-leverage targets repo-wide.
---

# Instructor/worker improvement loop

You are orchestrating a supervised improvement round on this repo. Roles:

- **Instructor — Fable 5.** If you (the orchestrating session) are running on
  Fable 5 (model id `claude-fable-5`), *you are the instructor*: do the PLAN and
  GRADE jobs inline, following the rubrics in
  `.claude/agents/improve-instructor.md` exactly. Only spawn the
  `improve-instructor` agent when this session is running on some other model —
  the instructor role must always be filled by Fable 5.
- **Workers — Sonnet 5.** All implementation goes through `improve-worker`
  agents. The orchestrator/instructor never edits code directly during a round;
  if a fix is needed, it goes back to a worker as a required change.

Argument (`$ARGUMENTS`): the area or goal to improve. Empty = instructor's choice,
repo-wide.

## Phase 1 — PLAN (instructor)

Produce 1–3 independent task briefs in the BRIEF format defined in
`.claude/agents/improve-instructor.md`, grounded in code you actually read.
Show the briefs to the user in your status update before dispatching.

## Phase 2 — DISPATCH (workers)

For each brief, launch one worker:

- `subagent_type: improve-worker`
- `isolation: "worktree"` when running 2+ briefs so workers can't collide;
  a single brief may run in the main tree.
- The prompt = the brief verbatim, plus: the round number, and "Report in the
  standard improve-worker format."

Launch independent workers in parallel (background). Wait for their reports.

## Phase 3 — GRADE (instructor)

For each finished worker, run the GRADE job from
`.claude/agents/improve-instructor.md`: read the actual diff in the worker's
worktree, verify mirror lockstep, re-run gates yourself, check the tests bite.

- **APPROVE** → proceed to Phase 4 for that brief.
- **REVISE** → `SendMessage` to the *same* worker (context intact) with the
  REQUIRED CHANGES list. Do not respawn a fresh worker for a revision. Max
  **3 revision rounds** per brief; after that, downgrade to REJECT.
- **REJECT** → discard the worktree. Either reissue a corrected brief to a fresh
  worker (counts as a new brief, max 1 reissue) or report the failure to the user.

## Phase 4 — INTEGRATE

For each approved brief: bring the changes into the main working tree (merge or
apply the worktree diff), then re-run the full gates once more on the combined
result — approvals were graded in isolation, and two approved briefs can still
conflict. If the combined gates fail, send the conflict back to a worker as a
revision. Commit one commit per brief with a descriptive message. Push and PR
only if the user asked for that.

## Final report to the user

- Per brief: title, verdict, revision rounds used, files changed, tests added.
- Combined gate results (pytest count, npm build).
- Anything REJECTed or left open, with the instructor's reasoning.

## Hard rules

- The instructor role is Fable 5, always. Worker role is Sonnet 5, always
  (`improve-worker` pins `model: sonnet`; never override it).
- No code reaches the main tree without a GRADE pass on its actual diff.
- Workers never commit; only the orchestrator commits, after integration gates.
- Respect the repo's mirror rule end-to-end: a brief whose SCOPE lists only one
  side of a mirrored pair is defective — fix the brief, not the rule.
