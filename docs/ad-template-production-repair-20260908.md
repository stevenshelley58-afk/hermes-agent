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
