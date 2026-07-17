---
name: improve-opus
description: Run the instructor/worker improvement loop with Opus 4.8 (high effort) as instructor and Sonnet 5 workers at high effort. Usage — /improve-opus <area or goal>; with no argument the instructor picks the highest-leverage targets repo-wide. Same protocol as /improve, different brains.
---

# Instructor/worker improvement loop — Opus 4.8 edition

You are orchestrating a supervised improvement round on this repo. This is the
/improve protocol with the roles re-cast:

- **Instructor — Opus 4.8, high effort, ALWAYS the `improve-opus-instructor`
  agent.** Unlike /improve (where a Fable 5 session plays instructor inline),
  the orchestrating session NEVER does the planning or grading itself here,
  regardless of what model it runs on — every judgment is delegated. Spawn
  `improve-opus-instructor` once at round start for PLAN, then continue THAT
  SAME agent via `SendMessage` for every GRADE and revision decision, so its
  context carries the whole round. The orchestrator does mechanics only:
  branch housekeeping, dispatching workers, relaying reports to the
  instructor, applying approved diffs, running the final combined gates, and
  committing.
- **Workers — Sonnet 5 at high effort.** All implementation goes through
  `improve-opus-worker` agents (`model: sonnet`, `effort: high` — never
  override either). The orchestrator/instructor never edits code during a
  round; a needed fix goes back to a worker as a required change.

Argument (`$ARGUMENTS`): the area or goal to improve. Empty = instructor's
choice, repo-wide.

## Phase 1 — PLAN (instructor agent)

Spawn `improve-opus-instructor` with the round's goal (plus any repro evidence
the orchestrator gathered). It returns 1–3 briefs in the BRIEF format defined
in `.claude/agents/improve-opus-instructor.md`, marking any briefs that
conflict on files. Show the briefs to the user in your status update before
dispatching.

## Phase 2 — DISPATCH (workers)

For each brief, launch one worker:

- `subagent_type: improve-opus-worker`
- `isolation: "worktree"` when running 2+ briefs so workers can't collide
  (briefs the instructor marked as conflicting run sequentially instead);
  a single brief may run in the main tree.
- The prompt = the brief verbatim, plus: the round number, and "Report in the
  standard improve-opus-worker format."

Launch independent workers in parallel (background). Wait for their reports.

## Phase 3 — GRADE (instructor agent)

For each finished worker, `SendMessage` the worker's report and its diff
location (worktree path or main tree) to the SAME `improve-opus-instructor`
agent. It reads the diff, verifies mirror lockstep, re-runs gates itself,
checks the tests bite, and returns a VERDICT.

- **APPROVE** → proceed to Phase 4 for that brief.
- **REVISE** → relay the instructor's REQUIRED CHANGES to the *same* worker
  via `SendMessage` (context intact). Do not respawn a fresh worker for a
  revision. Max **3 revision rounds** per brief; after that, downgrade to
  REJECT.
- **REJECT** → discard the worktree. Either have the instructor reissue a
  corrected brief to a fresh worker (counts as a new brief, max 1 reissue) or
  report the failure to the user.

## Phase 4 — INTEGRATE (orchestrator mechanics)

For each approved brief: bring the changes into the main working tree (merge
or apply the worktree diff), then re-run the full gates once more on the
combined result — approvals were graded in isolation, and two approved briefs
can still conflict. If the combined gates fail, route the conflict through the
instructor back to a worker as a revision. Commit one commit per brief with a
descriptive message. Push and PR only if the user asked for that.

## Final report to the user

- Per brief: title, verdict, revision rounds used, files changed, tests added.
- Combined gate results (pytest count, npm build).
- Anything REJECTed or left open, with the instructor's reasoning.

## Hard rules

- The instructor role is the `improve-opus-instructor` agent (Opus 4.8, high
  effort), always — the orchestrating session never plans or grades inline,
  and never overrides the agent's model or effort.
- Worker role is Sonnet 5 at high effort, always (`improve-opus-worker` pins
  `model: sonnet`, `effort: high`; never override them).
- No code reaches the main tree without the instructor's GRADE pass on its
  actual diff.
- Workers never commit; only the orchestrator commits, after integration
  gates.
- Respect the repo's mirror rule end-to-end: a brief whose SCOPE lists only
  one side of a mirrored pair is defective — the instructor fixes the brief,
  not the rule.
