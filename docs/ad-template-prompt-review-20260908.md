# Ad-template prompt review, 8 September 2026

Dated evidence, not an alternate acceptance or deployment policy. The current operator guide is /projects/frank/docs/AD_TEMPLATE_GENERATOR.md.

## Captured baseline

Hermes 75bd842674 with renderer cbc3f92e0 ran meta_044 as trun_53e25d7e4023429d96a4b239b2c91209 using frozen project model policy 45. The run consumed 16 comparisons and 34 provider calls (estimated $0.8110325). Its comparator finally accepted at 9.82-9.85 per section, but the reusable maximum-text scenario failed before final independent reviewers or import. Visual inspection still showed a CTA label above its button and missing outlined checkbox enclosures, so the comparator's pass was not proof of production quality.

A first stop followed an invalid patch using replace on an absent cornerRadius property, then a transport failure on the format retry. The ordinary retry preserved usage and the 11/16 comparison count. The evidence does not establish why the remote provider failed; the retry subsequently progressed.

## Confirmed prompt and handoff defects

- Comparators received the full CURRENT and BEST contracts, but no bounded revision memory. Prior failed targets could be proposed again.
- Reviewer proposals included pill-button corners despite a rectangular source button. The frontier diagnosis rejected that proposal, yet the controller immediately compiled the stale proposal.
- The initial builder lacked the actual replacement-text stress cases and promised maximum input lengths that did not fit both placements.
- Layer-refinement guidance carried an unrelated hardcoded six-bullet example and told the model to choose a font before positional fixes, conflicting with the font-identity exclusion.
- A target score could be treated as a destination instead of a conclusion from visible defects; the final numerical pass missed obvious defects.

## Contained changes

- Add a first-pass construction inventory, full Feed/Story role coverage, actual short/max/Unicode/empty scenarios, usable capacity constraints, and explicit CTA glyph alignment.
- Require fact-first whole-frame review, source-grounded targets, complete issue lists, and explanations for reversals. Preserve the five 9.8 gates and exact-font exclusion.
- Send the last three review/repair records without prior scores, plus any current diagnosis. Omit oversized old records intact instead of breaking the request.
- Remove duplicate BEST JSON and the irrelevant font inventory from comparator prompts. CURRENT remains the sole patch target.
- Reconcile a newly completed diagnosis through the next normal budgeted comparison before compiling further repairs.
- Clarify add versus replace, current layer pointers, fixed-family positional repair, and actual source checklist count.
- Advance evaluation policy version; old accepted evidence cannot directly skip fresh comparison after the policy changes.

No model routing, threshold, comparison/cost budget, readability minimum, quarantine, approval or publishing control is relaxed.

## Verification

The affected 19-file ad-template suite passed 211 tests with no failures. Regression coverage exercises diagnosis-to-comparator propagation before repair, fewer stale repair calls in the same eight-comparison fixture, bounded score-free history, correct history field extraction, and unchanged low-score/obvious-defect rejection. Ruff and whitespace checks passed.

Prompt tests establish correct wiring, not faster live convergence. A separate changed-prompt canary is needed to measure initial-build and iteration performance; do not start the first-50 batch on test results alone.

## Deployment and changed-prompt canary

Hermes 502fafe9f8f8262e7c7e5aa68324a33b26eada21 deployed on 8 September at 04:30:39 UTC. Its immutable release is /opt/releases/hermes-template-502fafe9f8; the running process selected that path and the unchanged renderer /opt/releases/blockwise-template-renderer-cbc3f92e0. Authenticated health returned HTTP 200, status ok. No active runs were interrupted.

The pre-deploy online database and exact selectors are retained at /srv/hermes/backups/ad-template-prompts-20260908-502fafe9f8. The prior release /opt/releases/hermes-template-75bd842674 remains available for rollback; preserve newer run history instead of restoring an old database casually.

On the same captured CURRENT/BEST evidence, the iteration-review prompt decreased from 49,788 to 39,985 characters (19.7%) including the new revision memory and diagnosis. This is a prompt-size measurement, not a measured latency or convergence improvement.

Changed-prompt canary trun_d0564e0fcfa540b1b293d165796b25c9 used the same meta_044 source and frozen model policy 45. Both its initial source-analysis call and one normal retry failed with the generic message frozen structured Responses role failed. Neither reached the modified builder/reviewer prompts or consumed a comparison. No further retry, route change, top-up, import, approval or publication was performed.

Correction after checking the exact timestamp and process: the HTTP 402 Insufficient Balance entry at 04:30:38 UTC was a separate chat request to api.deepseek.com, logged by the previous gateway PID 1185799. The Meta canary failures at 04:30:59 and 04:32:19 were logged by PID 1376125, without their underlying provider exception. The earlier Meta balance diagnosis was unsupported.

Current result: implementation deployed and regression-tested; live speed/quality comparison remains blocked by an undiagnosed source-analysis failure, not a demonstrated Meta billing problem. The first-50 batch remains unstarted.
