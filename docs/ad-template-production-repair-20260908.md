# Production repair step — 2026-09-08

Evaluation policy 8 repairs the editable candidate before production review:

- Fit supplied photo bytes into the slot by centre-covering inside the selected
  crop, never stretching pixels. Source-photo QA overrides are fitted separately.
- Honour positive image corner radius by using rounded-rectangle masks.
- Honour an explicit zero-radius pill repair as a rectangular vector.
- Keep generated assets, geometry, scores and acceptance thresholds unchanged.

After final-review rejection, persist the defect history and repair the candidate,
then rerender and rerun the comparator, reusable-content validation and both
independent final reviewers. Two rejected rounds trigger the already-frozen
diagnosis route for a coherent repair plan. Final review has at most six rounds;
the existing overall run cost limit remains unchanged. History is advisory,
bounded and score-free in reviewer prompts. Never manufacture acceptance.

Release verification: unit and process tests precede deployment. A fresh complete
live canary remains mandatory before first50 readiness; no live success is claimed
by this implementation note. Import remains quarantined; approval is separate.

## Deployment and initial-build recovery

Release `10eb8204dc2dfcfb28948fb1f9ac8f3f763c969a` deployed at 05:43:11 UTC
after 226 passing tests. Fresh canary `trun_5cfe715b6c954474b64b874ee5f2ca62`
failed before rendering: the initial builder returned four replacement references
but empty asset declarations, then the binding repair attempted immutable paths.
No acceptance or import occurred. This is not a photo-repair verdict.

Release `c237cfef18afc799691ed6760141419b5e72e989` deployed at 05:48:33 UTC
after 227 passing tests. It validates named asset references before initial
declarations are frozen, and strengthens the builder completeness prompt. All
missing references are reported together to the existing initial-output retry.
Fresh canary `trun_1c4ad23be565457ca919f830b63b0dd1` was submitted on policy 45.

## Subsequent repair-path corrections

`0670901a05` added canonical whole stroke/shadow targets and safe creation of
missing effects parents. `1363931b8a` enforced all measured targets even in the
generic repair path, checked effect objects against renderer limits, and added
safe provider exception type/status without logging response bodies or secrets.
`dc766637cc` decoded bounded JSON-encoded effect objects before those same strict
checks; it does not decode customer text or alter requested values or scores.
All 243 tests passed before that deployment at 06:19:51 UTC.

The canary passed main comparison 10 (all gated sections at least 9.82), but its
short-text reusable scenario failed the Story checklist readability floor. No
final acceptance or import occurred. This exposed a sequencing gap.
Evaluation policy 9 now runs all four reusable scenarios before each visual
comparison and validates final repairs likewise. Failures enter the existing
bounded contract-repair loop. Old visual baselines expire; iteration evidence,
comparison usage and spending bounds remain intact. Scenario cache version 2
uses candidate-specific output paths so different drafts cannot overwrite cached
evidence. Full renderer errors are bounded to 16,000 characters for repair.
