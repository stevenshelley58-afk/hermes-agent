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
