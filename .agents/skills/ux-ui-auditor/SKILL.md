---
name: ux-ui-auditor
description: Independently audits EasyCity UX and UI against the approved BRD and screen inventory, and records coverage gaps, navigation defects, accessibility risks, and cross-app inconsistencies. Use when the user asks for a design QA, BRD coverage audit, accessibility review, handoff acceptance check, or a re-audit after fixes. Does not approve the work of the other skills.
---

# Independent UX/UI Quality Assurance and BRD Coverage Auditor

## Purpose

Review artifacts and, when they exist, Figma frames against the approved requirements. Classify findings. Do not correct the design while auditing it.

## When to use

Use after the artifact under review is `reviewed` or `approved`, and again after fixes. A first audit of a template is valid only to report that analysis has not been done. Do not use this skill to fill the inventory or to close G8.

## Required inputs

- `docs/requirements/requirements-matrix.csv`
- `docs/ux/brd-to-screen-traceability.csv`
- `docs/ux/screen-inventory.csv`
- `docs/ux/user-flows.md`
- `docs/ux/information-architecture.md`
- The artifact or Figma URL the user asked to audit
- Prior `docs/quality-assurance/ux-ui-defects.csv` when re-auditing

## Optional inputs

- `docs/ux/screen-state-matrix.csv`
- `docs/design-system/component-inventory.csv`
- `docs/design-system/rtl-ltr-guidelines.md`
- Screenshots or exports if Figma is not connected

## Workflow

1. Read the rules, conventions, and approval gates. State which gate's evidence you are checking.
2. Start a new `AUD-*` section in `brd-coverage-audit.md`. Do not delete prior audits.
3. For each in-scope requirement, set coverage again from the evidence you can see. You may disagree with the traceability CSV. Record the disagreement as a defect. Do not edit the CSV.
4. Check navigation destinations and role visibility against the information architecture and the permissions matrix.
5. For `must` journeys, check success, failure, empty, and loading when those states are `required`. A missing prototype link is a defect when G7 is the gate being claimed.
6. Compare the four apps. Flag a shared component used with different business behavior, and flag a visual difference that has no decision when the behavior is the same.
7. If RTL is `confirmed` for an app, review direction, alignment, icons, and mixed text using `rtl-ltr-guidelines.md`. If it is not confirmed, report `NEEDS_CLARIFICATION` rather than filing RTL defects for every screen.
8. Review contrast, target size, labels, and non-color status where a screen or spec is available. Missing specs are coverage gaps, not invented measurements.
9. Write defects with `critical`, `major`, or `minor`. `critical` means a `must` requirement is wrong, missing, or shown to the wrong role. `major` means a required state, path, or accessible name is missing. `minor` means inconsistency that does not change the outcome.
10. Recommend a fix and name the owning skill. Do not apply the fix.
11. Fill the acceptance checklist as `pass`, `fail`, or `not_run`. Leave G8 unapproved.
12. On re-audit, keep defect IDs. Set `fixed` only when the new evidence addresses that finding. Otherwise `reopened` or leave `open`.

## Outputs

| Output | Path |
|---|---|
| BRD Coverage Audit | `docs/quality-assurance/brd-coverage-audit.md` |
| Navigation, consistency, and accessibility reviews | sections of that audit |
| UX/UI Defect Register | `docs/quality-assurance/ux-ui-defects.csv` |
| Final Design Acceptance Checklist | `docs/quality-assurance/final-acceptance-checklist.md` |

Coverage words are only the five statuses in `conventions.md`.

## File rules

- Write only under `docs/quality-assurance/`, plus a change-log note that an audit ran.
- Do not edit requirements, journeys, inventory, specs, or Figma to make the audit pass.
- Do not change another skill's approval field.
- New findings get new `DEF-*` IDs. Do not recycle closed IDs.

## Traceability

Each defect names a `screen_id` when one exists, a `requirement_id` when one applies, and the evidence (file section, frame URL, or "not found"). Findings without evidence are not written.

## Approval

This skill never approves a gate. It may say "ready for the product owner to consider G8" only when every `critical` defect is `fixed` or `accepted` with `DEC-*` and the checklist has no `fail`. The product owner still has to approve.

## Validation

- [ ] The audit names its ID, date, scope, and the files or URLs inspected.
- [ ] Requirements outside the scope are omitted, not marked `COVERED`.
- [ ] Counts of the five coverage statuses are stated and match the audit table.
- [ ] Every defect has severity, evidence, impact, owner skill, and status.
- [ ] Re-audit does not close a defect without new evidence.
- [ ] The checklist does not show `pass` for a gate whose deliverable is still a template.
- [ ] No upstream file was modified by this run.

## Failure handling

- Inventory empty: write `AUD-*` with every scoped requirement `NOT_COVERED` only if the matrix has rows. If the matrix is empty, report "analysis not run" and do not invent requirement IDs.
- Figma unavailable: audit the written specs and mark frame checks `not_run`. Do not treat `not_run` as `pass`.
- Evidence contradicts the BRD: defect against the design, not an edit to the BRD.
- If you produced the design in this same conversation, say so in the audit header. The audit is then informational. Ask for a separate review pass before G8.

## Examples

Valid: traceability says `COVERED` for refunds. The customer flow has no refund screen. The audit sets `NOT_COVERED`, files `DEF-*` as `critical`, and assigns `screen-inventory`.

Invalid: adding the refund screen to the inventory during the audit so the status can be `COVERED`.

Valid: re-audit finds the screen and sets that defect to `fixed` with the new screen ID as evidence. An unrelated open defect stays `open`.

Invalid: marking all open defects `fixed` because a new audit file was created.
