# SiteDetective — project notes

## Spec tracking

Feature specs live in `docs/spec-*.md`; `docs/SPECS.md` is the status index.

- Every spec carries a `**Status:**` line near the top (Draft / In progress /
  Implemented / Partial (n/m phases) / Superseded / Proposal).
- **Any commit that implements or supersedes (part of) a spec must update that
  spec's `Status:` line and the `docs/SPECS.md` table in the same commit.**
- Multi-phase specs track progress via their phase tables and acceptance-criteria
  checkboxes; tick `- [ ]` items as they land.
- New specs follow `docs/spec-template.md` (skeleton + authoring checklist);
  when implementing a spec, flag anything it leaves ambiguous instead of guessing.
