# Richer Run Detail Page — Plain-Language Spec (PM Edition)

**Status:** Implemented (see technical companion spec-run-detail-page.md)
**Date:** 2026-07-06
**Technical companion:** `spec-run-detail-page.md` (same feature, engineering detail)

## What is this about?

When a test finishes running, SiteDetective shows a "run detail" page — the
place you go to answer *"what happened during this run?"*

Today that page is hard to read for anyone who didn't write the test:

- The page title is the test's internal ID (like `checkout-smoke`) instead
  of its human name (like "Checkout works end to end").
- Each step in the run shows only a code-style label (like
  `assert_element`), whether it passed, and how long it took. You can't see
  *what the step was trying to do*, *where on the page it acted*, or *what
  information it used*.
- There are no pictures of the page elements the test interacted with, so
  you can't visually confirm the test clicked or checked the right thing.

This project makes the run detail page tell the full story of a run in
plain terms, with pictures.

## What will change for users?

### 1. The page will be named after the test, not its ID

The big title becomes the test's friendly name. The internal ID moves to
the small print underneath, for anyone who needs it.

### 2. A "Test details" summary at the top

A new section shows, in a few short lines:

- **Description** — what this test is for, as written by its author.
- **Starting URL** — the web address where the run began.
- **Settings** — the test's timing and retry settings, e.g.
  "Timeout 10s · Retries 1 · Healing on".

### 3. Every step explains itself

Each step card gets:

- **A readable title** — "Assert Element" instead of `assert_element`,
  "Click" instead of `click`, and so on.
- **The intent** — the plain-English sentence the test author wrote about
  what the step should do (e.g. "The Add to Cart button on the product
  page"). This becomes the most prominent line on the card.
- **The relevant details for that kind of step** — only what applies,
  nothing left blank:
  - a *Navigate* step shows the address it went to;
  - a *Click* step shows what it clicked;
  - a *Type* step shows where it typed and what it typed;
  - an *Assert Element* step shows what it checked for and any text it
    expected to find;
  - a *Screenshot* step shows its label and whether it captured the whole
    page; and so on for each step type.

### 4. A picture of the element each step touched

For steps that interact with something on the page (clicking, typing,
checking an element exists), the run will now save a small photo of that
exact element and show it as a thumbnail on the step card. Click the
thumbnail to see it full size. This is the fastest way for a human to
confirm "yes, the test pressed the right button."

### 5. Self-healing becomes visible where it happened

SiteDetective can "heal" a test when a page changes (it finds the moved
button on its own). When that happens, the step card will now say so right
there: "Selector healed: *old* → *new*". Today this information only lives
in a separate audit section at the top of the page.

## An important behind-the-scenes decision (why this isn't just cosmetic)

The details above aren't currently *saved* anywhere when a run happens —
the system only records the bare minimum. And we can't just look up the
test's current definition when someone views the page, because **tests
change over time**: someone edits the test tomorrow, or self-healing
updates it automatically. The page would then show details that don't match
what actually ran — which is misleading exactly when it matters most
(investigating a past failure).

So the core of this work is: **at the moment a run executes, save a
snapshot of everything about the test as it was right then.** The run
detail page then always shows the truth about that specific run, forever,
even if the test is later edited or deleted.

Consequence to be aware of: **runs that happened before this change won't
have snapshots.** Old run pages will keep working and look the same as
today; only new runs get the richer view. We will not attempt to
reconstruct details for old runs (it can't be done reliably).

## What stays private and safe

- Tests sometimes use secret values (like an admin password) via
  placeholders. The page will always show the placeholder (e.g.
  `{{admin_password}}`), never the real secret.
- For typing steps that involve a secret, we deliberately **skip** the
  element photo — otherwise the picture could show the secret sitting in
  the input field.

## What is NOT included (this round)

- Reconstructing rich details for runs that happened before this change.
- Updating the separate downloadable/emailable HTML report to match (a
  natural follow-up).
- Showing how a test has *changed* since the run ("this test was edited
  after this run") — also a candidate follow-up.
- Videos or recordings of runs; this adds still images only.

## How we'll know it's done (acceptance checklist)

1. Open any new run: the title is the test's friendly name; the summary
   section shows description, starting address, and settings.
2. Every step reads in plain words: readable type, the author's intent,
   and the details relevant to that step — with no empty or "None" labels.
3. Steps that clicked/typed/checked something show a small photo of that
   element; steps that failed to find their element show no photo (there
   was nothing to photograph).
4. A step that self-healed says so on the card, showing old → new.
5. Pages for runs from before this change still open and work normally.
6. No secret value ever appears anywhere on the page or in any photo.

## Rough scope

Four areas of the product are touched: how run data is stored (three new
optional fields), the test runner (save the snapshot and take element
photos), and the run detail page itself (layout and wording). No changes to
how tests are written or scheduled; no data migration risk beyond adding
the new optional fields.
