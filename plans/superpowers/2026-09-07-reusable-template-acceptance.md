# Reusable template acceptance

## Objective

Before expensive independent final-review calls and Blockwise import, prove
that the accepted structured document remains renderable when its declared
editable inputs are replaced with bounded deterministic values. This is
reusable-template evidence, not a second visual likeness gate.

## Invariants

- Exercise short, max-length, and Unicode text values.
- Exercise declared default image bindings and a bounded alternate crop.
- Fail closed on missing layer declarations, missing required defaults,
  invalid asset bindings, renderer rejection, or missing render files.
- Keep scenarios bounded and deterministic, and reuse evidence when candidate,
  renderer identity, and scenario identity are unchanged.
- Persist `reusable_validation` evidence in the checkpoint/output without a
  database or changes to dual review, quarantine, approval, or smoke-test
  controls.

## Integration

Run after comparator acceptance and production defaults are materialized,
immediately before the independent final-review loop. A failure preserves the
normal checkpoint and best candidate and prevents final review/import.

## Verification

Unit tests cover valid substitutions, Unicode and bounded long text, missing
required/default bindings, invalid asset references, renderer failures, cache
invalidation and cancellation between scenarios.

Deployed gateway revision: `a11673d0486725c3758911eb657203cc3806eac3`.
The required hermetic runner passed 72 tests. Four real shared-CLI scenarios
passed during implementation; no new model-generation approval is claimed.
The renderer remains pinned to `39e51fedafbdc7a8ce189f3e944a4f3ab35b8058`;
both renderer and contract source trees match Blockwise's serving canonical
preview release. Prior gateway override is retained in
`/srv/hermes/releases/ad-template-20260907/only-ad-template-process.before.conf`.
