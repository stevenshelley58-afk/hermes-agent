# Ad Output QA development evidence, 8 September 2026

The maintained policy and release procedure are owned by
`/projects/frank/docs/AD_TEMPLATE_GENERATOR.md`. This dated record does not
establish deployment without the release receipt below.

## Scope

The new `skills/creative/ad-output-qa` skill preserves approved designs while
checking production defects. Its pinned compact checklist augments existing
reviews, rather than adding new model rounds. The executable validator reuses
painted-ink measurement and the existing bounded correction process. The skill
distinguishes measured evidence, visual findings and unknown/unsupported cases.
The script `scripts/review_output.py` is a read-only wrapper around that same
validator, not an alternate rendering or publishing implementation.

The initial mechanical scope is narrow: supported high-contrast container-label
centering, render dimensions/opaque corners, full-canvas mask signals and explicit
embedded CTA semantics. Clipping, contrast on photographs, semantic cropping,
annotation intent and other style-dependent checks still require the existing
renderer/reusable checks and visual reviewers. Do not claim every checklist item
is machine-enforced, or convert a mechanical pass into publishing approval.

## Reproduced defect

Saved revision `rrev_c5392c864a6647b580e1b702c1c90bd3` of run
`trun_5259af7fe12a44fd97963158f0473fe3` moved the Feed label up5px to centre its
invisible box. The old detector skipped both informational badges because their
shape was `pill`. The new detector measured actual glyph offsets: Feed before
4.5px above centre, Feed after9.5px above centre, Story before/after10.5px above
centre. The recorded6px tolerance is a conservative initial native1080px-canvas
mechanical threshold, not a universal optical-centering rule or a conversion of
the9.8 review score. The after renders fail. Original artifacts were only read.

## Skill evaluation

Three hypothetical evidence-only cases tested worsened corrections, supported
good alignment and unknown pixel evidence. Luna reviewed each with and without
the skill. Both configurations passed12/12 assertions. This is a smoke test, not
evidence of improved model accuracy, speed or visual-detection performance.
Timing/token telemetry was unavailable. A static review viewer was generated
in the local skill's sibling evaluation workspace.

## Verified release

Hermes `57d8ce6eb3410516cbdfefe6c68976d83c2bb8e5` was deployed on 8 September
2026 to `/opt/releases/hermes-template-57d8ce6eb3`. The settled gateway process
selected that exact import path and authenticated health returned 200. The
default Hermes profile installed the bundled skill at startup; its skill and
checklist match the release bytes. The shared skill link and generator entry
point also route to this review process.

The final test run passed 302 tests across 27 files. It includes a real
orchestrator-path regression where both final reviewers return 9.9 but a defect
in the final render prevents the import call. Other tests cover valid controls,
horizontal/vertical displacement, missing/corrupt renders, unsupported effects,
informational badges, explicit CTAs, stale repair evidence and review clearance
bound to the same candidate and render hashes. No new provider calls were needed.

The deployed interpreter rejected the saved example's Feed and Story alignment
errors. Candidate and image hashes were unchanged, and the existing run remained
`ready_for_review`; this release does not rewrite historical readiness or apply
a correction to the current ad. Future generation/revision handoffs use the new
gate. No template was published and the first-50 batch was not started.
Backup and private receipts: `/srv/hermes/backups/ad-output-qa-20260908-57d8ce6eb3`.
Rollback retained: `/opt/releases/hermes-template-00097034cb` and its selector.
