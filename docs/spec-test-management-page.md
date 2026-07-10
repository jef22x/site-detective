# Spec: Test Management Page

**Status:** Implemented
**Date:** 2026-07-04
**Depends on:** Existing test schema (`app/schemas.py`), test files in `tests/*.yaml`

## 1. Overview

A user-friendly web page where a user can create a test, add/edit/reorder test steps through a form UI (no YAML editing required), and save the test with a single button. Saved tests are written as YAML files in the `tests/` directory using the existing `TestDefinition` schema (`schema_version: 1`), so they are immediately runnable by the existing runner.

## 2. Goals

- Non-technical users can author a test without touching YAML or code.
- Tests created in the UI are 100% compatible with `app.schemas.TestDefinition` and `load_test()`/`save_test()`.
- Existing tests in `tests/` can be opened, edited, and re-saved without data loss.

### Non-goals (v1)

- Multi-user collaboration, auth, or permissions.
- Editing app config (`{{variable}}` values) — variables are referenced, not defined, here.
- Step types beyond the existing eight.

## 3. Users & primary flow

**Persona:** QA tester or store owner comfortable with a browser, not with YAML.

1. User opens the Test Management page and sees a list of existing tests.
2. Clicks **New Test**, fills in name/description (ID auto-generated from name).
3. Adds steps one at a time via **Add Step**; picks a step type and fills only the fields relevant to that type.
4. Reorders or deletes steps as needed.
5. Clicks **Save Test**. The test is validated and written to `tests/<id>.yaml`. A success toast confirms; validation errors are shown inline.

## 4. UI specification

### 4.1 Test list view (`/tests`)

- Table of all tests found in `tests/` (`*.yaml`, `*.json`): columns **Name**, **ID**, **# Steps**, **Last modified**.
- Actions per row: **Edit**, **Duplicate**, **Delete** (delete requires confirmation dialog).
- **New Test** button (primary, top right).
- Files that fail schema validation appear grayed out with a warning badge and tooltip showing the error; they can't be opened in the editor.
- **Run** button per row: starts the test via the existing runner. Only one run may be active at a time (existing constraint); while a run is active all Run buttons are disabled and the live status bar shows progress. When the run finishes, the row links to the run detail page.

### 4.2 Test editor view (`/tests/new`, `/tests/{id}/edit`)

**Header section**

| Field | Control | Rules |
|---|---|---|
| Name | text input | required, 1–120 chars |
| ID | text input | auto-slugified from Name (`mock-shop-purchase`); editable on new tests; read-only when editing an existing test; must match `[a-z0-9-]+` and be unique |
| Description | textarea | optional |
| Default timeout (ms) | number input | default 10000, min 100 |
| Default retries | number input | default 1, min 0 |
| Healing enabled | toggle | default on |

Defaults are collapsed under an "Advanced defaults" disclosure so the common case stays simple.

**Steps section**

- Vertical list of step cards, numbered. Each card shows a one-line summary when collapsed (e.g. `3. Click — "The Add to Cart button"`), and the full field form when expanded.
- **Add Step** button at the bottom and an insert affordance (`+`) between cards.
- Each card: drag handle for reordering, **Duplicate**, **Delete**.
- Step type is chosen from a dropdown with friendly labels:

| Type | Label | Visible fields |
|---|---|---|
| `navigate` | Go to URL | url (required) |
| `click` | Click element | intent*, selector, context |
| `type` | Type text | intent*, selector, value (required), clear_first |
| `select` | Select dropdown option | intent*, selector, value (required) |
| `wait` | Wait for condition | condition (visible/hidden/navigation/delay), intent*, selector, timeout_ms |
| `assert_element` | Check element | intent*, selector, exists, text_contains |
| `screenshot` | Take screenshot | label (required) |
| `login` | Log in | context (customer/admin) |

\* **Intent rule (existing schema constraint):** if a selector is filled in, intent is required. The UI enforces this live — the intent field shows "Required when a selector is set" and Save is blocked until satisfied. Intent is presented as *"Describe this element in plain English"* with a placeholder example.

- Per-step **Advanced** disclosure exposes `timeout_ms`, `retries`, and `capture_as` overrides.
- Fields supporting `{{variable}}` placeholders (url, value, selector) show a hint chip; unknown-variable checking is out of scope for v1.
- A test must contain at least 1 step to save.

**Save behavior**

- **Save Test** button, fixed/sticky at the bottom of the editor. Disabled while there are blocking validation errors (each error also flagged inline on the offending field/card, with a click-to-scroll error summary).
- On save: serialize to the `TestDefinition` shape, validate server-side with Pydantic, write via `save_test()` to `tests/<id>.yaml` (`exclude_none`, unsorted keys — matching existing file style).
- Success: toast "Test saved" and return to (or stay on) the editor with a clean dirty-state.
- Unsaved-changes guard: navigating away with edits prompts "Discard changes?".

**Run behavior**

- **Save & Run** button next to Save in the editor: saves (same validation path), then starts the test via `POST /api/tests/{id}/run`.
- Returns 409 with a friendly message if a run is already in progress.
- While running, the existing live status poller (`/api/status`) shows step-by-step progress in the nav bar; on completion the user is offered a link to the run detail page.

## 5. API specification

Small JSON API served by the existing Python app (FastAPI suggested):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tests` | List tests: id, name, step count, mtime, valid flag |
| `GET` | `/api/tests/{id}` | Full `TestDefinition` as JSON |
| `POST` | `/api/tests` | Create; 409 if id exists; 422 with field-level Pydantic errors |
| `PUT` | `/api/tests/{id}` | Update existing test |
| `DELETE` | `/api/tests/{id}` | Delete the test file |
| `POST` | `/api/tests/{id}/duplicate` | Copy with new id (`<id>-copy`, deduplicated) |
| `POST` | `/api/tests/{id}/run` | Start a run via the existing executor; 409 if a run is active |

- All write endpoints validate with `TestDefinition.model_validate` before touching disk; validation errors return the Pydantic error list so the UI can map them to fields.
- `id` in the path must match the id in the body on `PUT`.
- Path safety: ids are validated against `[a-z0-9-]+` before being used in file paths (no traversal).

## 6. Data format

No new storage. Tests remain YAML files in `tests/` conforming to the existing schema, e.g.:

```yaml
schema_version: 1
test:
  id: my-new-test
  name: "My new test"
  defaults: { timeout_ms: 10000, retries: 1, healing: true }
  steps:
    - type: navigate
      url: "{{store_url}}/product.html"
    - type: click
      intent: "The Add to Cart button"
      selector: "button.single_add_to_cart_button"
```

## 7. Validation rules (summary)

1. Name required; ID required, unique, slug format.
2. ≥ 1 step.
3. Per-type required fields as in §4.2 table.
4. Selector ⇒ intent (mirrors `Step._require_intent_with_selector`).
5. Numeric fields: timeout_ms ≥ 100, retries ≥ 0.
6. Server-side Pydantic validation is the source of truth; client-side checks are a convenience mirror.

## 8. UX quality bar

- Keyboard: Enter in header fields doesn't submit; Ctrl+S triggers Save.
- Empty state on the editor: "No steps yet — add your first step" with a prominent Add Step button.
- Step summaries use the intent text when present, so the list reads like a plain-English script.
- Responsive down to ~768px; desktop-first.

## 9. Acceptance criteria

- [ ] User can create a test with ≥ 1 step and save it; resulting file loads cleanly via `load_test()`.
- [ ] User can open `tests/mock-shop-purchase.yaml`, edit a step, save, and no fields are dropped or reordered destructively.
- [ ] Saving with a selector but no intent is blocked with an inline message.
- [ ] Duplicate IDs are rejected with a clear error.
- [ ] Deleting a test requires confirmation and removes the file.
- [ ] Steps can be reordered by drag-and-drop and the saved order matches the UI.
- [ ] Run button starts a run, live progress is visible, and a second run attempt while active is rejected with a clear message.

## 10. Open questions

- Read-only raw YAML preview tab in the editor — useful for power users?
- Should `login` steps get credential hints, given secrets masking in `app/config.py`?
