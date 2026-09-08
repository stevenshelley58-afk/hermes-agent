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
and diff whitespace checks passed. Live acceptance remains a separate gate.
test files via `scripts/run_tests.sh`; Ruff and `git diff --check` passed.
