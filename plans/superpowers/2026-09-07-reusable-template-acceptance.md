# Reusable template acceptance

## Objective

Before expensive independent final-review calls and Blockwise import, prove
that the accepted structured document remains renderable when its declared
editable inputs are replaced with bounded deterministic values. This is
reusable-template evidence, not a second visual likeness gate.

## Invariants

- Exercise short, max-length, and Unicode text values.
- Exercise declared default image bindings and a bounded alternate catalog
  binding where the input permits it.
- Fail closed on missing layer declarations, missing required defaults,
  invalid asset bindings, renderer rejection, or output escape.
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
required/default bindings, invalid asset references, renderer failures, and
