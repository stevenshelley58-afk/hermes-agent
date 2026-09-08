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

The feedback release `a5c6a795673352ad9f8ba5349c3bf122fc00ab9b` deployed
at 05:12:37 UTC. The same canary reused its saved candidate/images, reported
both invalid corrections together and corrected the Story font target, but
repeated the invalid colour target. No batch was started.

The follow-up aligns reviewer vocabulary with Blockwise's actual semantic
colour contract: locked targets now support colourRole and existing stroke/
shadow colourRole fields, restricted to the six contract roles and layers
that expose the property. The review prompt explicitly distinguishes these
from nonexistent solid fill/colour fields and supplies the complete stroke
object. No invalid target is silently converted or discarded.

Follow-up verification: all 220 tests in 20 ad-template files passed; Ruff
and diff whitespace checks passed. Live acceptance remained a separate gate.

## Final live outcome

Release `2fb98a5a0f125cfa82d9be6f2b6bdf0ad0fc46e0` deployed at
05:19:11 UTC, with authenticated health and live import path verified.
Renderer remains `cbc3f92e061477f5f2162ef816d26e130ec16fcf`.
Protected backups are `/srv/hermes/backups/ad-template-feedback-20260908-a5c6a79567`
and `/srv/hermes/backups/ad-template-feedback-20260908-2fb98a5a0f`.
The two prior immutable releases and existing rollback releases are retained.

The same canary progressed to five normal comparisons; the fifth scored 9.9
in every section. All four reusable scenarios passed at that point. Two final
repair comparisons followed (seven comparison events total; artifact numbering
reached eight because a reverted candidate also receives a number).
After three independent final-review rounds, it failed closed at `final-check`.
No final acceptance, import, smoke test, approval or publishing occurred.
All 34 recorded provider calls across this canary's lifetime have a combined
estimated cost of USD 0.406361, including earlier failed attempts. This is
not a claim about actual billing or successful end-to-end convergence.

Both final reviewers still rejected it. Residual defects included distorted
Story gallery images, source corner/mask mismatches and text footprint/position.
Manual inspection of iteration-08 Story confirmed severe photographic stretching.
The renderer's renderImageSlot maps the selected source crop directly onto the
destination rectangle; nonmatching aspect ratios therefore distort. A high
comparator score did not establish image correctness. The first-50 batch remains
unstarted, and the live run collection has no active generation runs.
