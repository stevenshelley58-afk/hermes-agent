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
