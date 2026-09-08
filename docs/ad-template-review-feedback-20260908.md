# Review feedback recovery, 8 September 2026

Dated evidence, not an alternate operator guide. The maintained acceptance
policy remains `/projects/frank/docs/AD_TEMPLATE_GENERATOR.md`.

On the user's request to generate, the existing improved-prompt canary
`trun_d0564e0fcfa540b1b293d165796b25c9` was retried through the supported API.
Meta source analysis succeeded, followed by four demo images and a rendered
candidate. This disproves a continuing blanket Meta access failure, but does
not establish why the earlier source-analysis requests failed.

The first comparator response failed an unavailable `fill/colour` target;
the sole format retry then failed a below-minimum font-size target. The
candidate visibly retained clipped date text, filled checklist squares and
a misaligned CTA. No comparison pass, final review, import or approval occurred.

The recovery change reports independent issue validation errors together,
includes the layer, placement and exact font floor in its rejection, and
asks the existing single retry to check all corrections and their matching
patch operations. Invalid issues still fail closed. Scores, thresholds,
comparison budgets, provider routes, approval and publishing remain unchanged.

Verification before deployment: 214 tests passed across all 20 ad-template
test files via `scripts/run_tests.sh`; Ruff and `git diff --check` passed.
