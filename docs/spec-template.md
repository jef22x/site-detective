# Spec template & authoring checklist

How to write a spec that any implementer — human or model, large or small —
can execute faithfully. Modeled on `spec-healing-tiers.md`, the reference
example of the structure below. Copy the skeleton in §2, then check your
draft against §3 before handing it off.

## 1. Why this structure

A spec fails in two ways: the implementer misunderstands what exists, or
guesses where the spec is silent. Every section below closes one of those
gaps. The guiding rule: **decisions made, not deferred; gaps labeled, not
implied.** An explicitly listed open question gets flagged during
implementation; an unlabeled gap gets silently guessed.

## 2. Skeleton

```markdown
# Spec: <Title — noun phrase, what ships>

**Status:** Draft v1 — not started (0/n phases)
**Date:** YYYY-MM-DD
**Depends on:** <modules/files this touches, with paths and line refs>
**Related:** <sibling/predecessor specs and how they relate (supersedes, extends)>

## 1. Overview
What's wrong today and what this changes. Include 1–2 user stories
(> As a …, I want …, so that …) for the behaviors that matter most.

## 2. Goals
Bulleted, each one observable/testable — not quality adjectives.

### Non-goals
What is deliberately out of scope. Prevents scope creep and "helpful" extras.

## 3. Current behavior (inventory)
| # | Behavior | Where |
|---|---|---|
| 1 | <what happens today> | `file.py:lines` |
Numbered so later sections can say "changes current behavior #2".

## 4..N. Design
The decisions: schemas verbatim, function names and homes, precedence
orders, config keys with defaults, prompt/UI wording, exact log lines.
Flag every intentional behavior change by inventory number.

## N+1. Rollout & sequencing
| Phase | Ships | Depends on | Rough size |
|---|---|---|---|
Each phase sized S–M — independently shippable, testable, one session of work.

## N+2. Testing
Per phase or per component: the fixture, the scenario, what it asserts.
Name the headline test (the one that proves the spec's core promise).

## N+3. Acceptance criteria
Numbered, each a checkable behavior with concrete inputs and expected
outcomes. These become the checkboxes ticked as phases land.

## N+4. Open questions
Decisions genuinely deferred to implementation, each with a lean/default
and what evidence would settle it.
```

## 3. Authoring checklist

Before handing a spec to an implementer, verify:

- [ ] **Anchored:** every reference to existing behavior cites a file (and
      line range where stable) — `executor.py:336-405`, not "the healing
      orchestration". The inventory table (§3) covers everything the spec
      touches.
- [ ] **Decided:** schemas, names, orderings, defaults, and user-facing
      wording are given verbatim. If you catch yourself writing "some kind
      of", "appropriate", or "as needed" — decide it or move it to Open
      questions.
- [ ] **Bounded:** non-goals are stated; anything a reasonable implementer
      might add unprompted is either in scope explicitly or excluded
      explicitly.
- [ ] **Flagged:** every intentional change to current behavior points at
      its inventory row. Silent behavior changes read as bugs.
- [ ] **Phased:** phases are S–M, each independently testable, dependencies
      explicit. No monolith phases.
- [ ] **Testable:** each acceptance criterion could be turned into a test
      without further interpretation; the test plan names fixtures and
      scenarios, not just "add tests".
- [ ] **Degradation stated:** what happens when a dependency is absent,
      errors, or times out (offline services, missing config, partial data).
- [ ] **Status wired:** carries the `**Status:**` line and a row in
      `docs/SPECS.md` (per the rule in `CLAUDE.md` and `SPECS.md`).

## 4. Handoff protocol

When asking a model to implement a spec:

1. One phase per session/branch, in dependency order.
2. First instruction: *"Restate the phase's plan file-by-file and list any
   ambiguities before writing code."* Spec gaps surface while they're still
   cheap to fix.
3. Standing rule: *"Where the spec is silent, flag it — don't guess."*
4. Each phase commit updates the spec's `Status:` line, its
   acceptance-criteria checkboxes, and the `docs/SPECS.md` table (per
   `CLAUDE.md`).
