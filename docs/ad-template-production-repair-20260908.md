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

## Subsequent repair-path corrections

`0670901a05` added canonical whole stroke/shadow targets and safe creation of
missing effects parents. `1363931b8a` enforced all measured targets even in the
generic repair path, checked effect objects against renderer limits, and added
safe provider exception type/status without logging response bodies or secrets.
`dc766637cc` decoded bounded JSON-encoded effect objects before those same strict
checks; it does not decode customer text or alter requested values or scores.
All 243 tests passed before that deployment at 06:19:51 UTC.

The canary passed main comparison 10 (all gated sections at least 9.82), but its
short-text reusable scenario failed the Story checklist readability floor. No
final acceptance or import occurred. This exposed a sequencing gap.
Evaluation policy 9 now runs all four reusable scenarios before each visual
comparison and validates final repairs likewise. Failures enter the existing
bounded contract-repair loop. Old visual baselines expire; iteration evidence,
comparison usage and spending bounds remain intact. Scenario cache version 2
uses candidate-specific output paths so different drafts cannot overwrite cached
evidence. Full renderer errors are bounded to 16,000 characters for repair.

## Measured fit and contradictory correction recovery (07:30 UTC)

Dated progress only: no complete successful live handoff is established here.
Canary `trun_1c4ad23be565457ca919f830b63b0dd1` exhausted its preserved
16-comparison limit and failed. Its earlier comparator pass did not survive
policy-9 requalification. No score or budget was reset; it was not imported.

Hermes releases `8988cfca64`, `dc73adcb56`, `eea4ba0df7` and `e43fcc56ab`
added actual four-scenario text payloads to repair feedback, a once-per-run
optional final diagnosis with transport-failure recovery, redacted request
fingerprints/error categories, and source plus current-render repair evidence.
The intermittent Meta HTTP 500 cause remains unproven; a matching read-only
diagnostic request succeeded. This is not evidence of insufficient Meta balance.

Renderer `a009543dda9ccf47a3ebe56b69ee168828d31436`, deployed at 07:07:41 UTC,
reports measured required text lines and dimensions at the existing font floors.
It does not change rendering or layout algorithms. The renderer's 18 tests,
repository tests, NUL check, typecheck and production build passed. Its pinned
release is `/opt/releases/blockwise-template-renderer-a009543dd`.

Hermes `924d83ad13` (252 passing tests) tightened initial capacity planning,
distinguished text-box capacity from painted ink, and removed duplicated candidate
data in generic repair prompts. Fresh canary
`trun_df2ed27692b846fcbfd46de3fca12e13` still needed five contract repairs and
then failed because a reviewer requested rounding the opaque canvas background.
The first repair made transparent corners; its retry omitted the locked target.

Hermes `bb6e266e6d14af08a24b5bc98a8fd600320a231c`, deployed at 07:30:37 UTC
after all 253 generator tests, Ruff and whitespace checks passed, rejects that
invalid target before repair and directs review to the actual visible card layers.
The same sample resumed through the supported API with saved photos and budgets.
Acceptance, provider policy 45, quarantine and approval gates remain unchanged.
Protected release and online-backup paths retain their existing revision suffixes;
the latest are `/opt/releases/hermes-template-bb6e266e6d` and
`/srv/hermes/backups/ad-template-feedback-20260908-bb6e266e6d`.

## Evidence identity and draft selection (07:44 UTC)

The sample reached six comparisons, then optional diagnosis timed out. A subsequent
PNG-read error stopped it; independent verification and full decoding of all 147
saved PNGs succeeded. No saved image was replaced or deleted, and no permanent
corruption or root cause is claimed.

Release `df327de2876ae62df59bbbec5e0697213c1a453d` deployed at 07:39:22 UTC
after 254 passing tests. Each comparator/final-review image now has adjacent
source/current/diagnostic/baseline identity text. The run advanced its best draft
at comparison eight, but discarded comparison nine despite improved section
scores and fewer defects because its pairwise result was same. A subsequent Meta
repair call returned HTTP 500 with an internal category; no import occurred.

Release `b8e8d4d1b7fb9d1fabd54e3ac3e025c71830a5aa` deployed at 07:44:49 UTC
after 255 passing tests, Ruff and whitespace checks. A same pairwise result may
now retain a draft only if no gated score decreases, at least one improves,
the issue count decreases, and blocker/material/effect-mismatch counts do not
increase. This changes draft selection, not 9.8 acceptance or final reviews.
Saved discarded records remain untouched. The sample resumed with nine used
comparisons; a full live success and first50 readiness remain unproven.

## Reasoning-setting qualification

The sample's main comparison 11 met every section floor, with four reusable
checks passed. Independent final review rejected missing checkbox outlines and
button alignment; subsequent repair comparisons regressed and were reverted.
A final-review response error ended the attempt before import. Both optional
high-reasoning Astra diagnosis calls timed out at 150 seconds. This is not a
successful handoff or a reason to lower acceptance thresholds.

Small real image/strict-JSON probes on the unchanged providers qualified medium
reasoning: Meta Muse Spark returned the correct above-center CTA and absent
checkbox outlines in 54.31 seconds (2,581 tokens); Concentrate Gemini did so in
6.80 seconds (1,753 tokens), and Concentrate Astra in 6.38 seconds (1,001 tokens).
The initial Meta probe's 1,000-token allowance did not produce parseable output;
the 4,096-token probe completed. These probes are diagnostic, not template runs.
The Responses roles now request medium reasoning within unchanged time, output,
cost, route and acceptance bounds. A fresh full canary must establish readiness.

## Aggregated preflight repair (08:13 UTC)

Canary `trun_cf767d809b8c438497a1e9bc9676ea80` stopped before comparison:
the fourth medium-reasoning contract repair used its entire 8,192-output-token
allowance without returning output text. A read-only low-effort repair probe
returned a renderable patch but still failed reusable fit. These are failures,
not readiness evidence. Direct Astra patch qualification also failed strict
schema compatibility; the frozen provider routes were not changed.

Release `f9651d6c8e7d0b0d34699865fbd51f1ab1d9535b` deployed at 08:13:33 UTC
after 258 generator tests, Ruff and whitespace checks passed. It includes the
preceding aggregated four-scenario feedback change, plus one durable diagnosis
request using the existing Concentrate route to plan coordinated capacity fixes.
Patch calls use low reasoning; builders, reviewers and diagnosis remain medium.
No output/time/cost/score allowance was increased. Optional diagnosis transport
failure is recorded once; budget failures still propagate. The supported retry
resumed the same saved sample at policy 45. Health returned 200. Previous pinned
releases and an online database backup remain available. A complete accepted
run, quarantined import and matching smoke test remain required before first50.

## Complete review response allowance (08:24 UTC)

The preflight diagnosis completed in 149.166 seconds and its one Meta patch in
17.542 seconds cleared all four reusable scenarios. Both first-comparator
responses then failed structured parsing at approximately 8,178 output tokens.
A read-only reproduction showed 7,860 reasoning tokens at the old 8,192 cap.
Low reasoning returned quickly but contained contradictory coordinate advice.
Medium reasoning with compact instructions and 16,384 total output room returned
a valid review in 83.2 seconds (10,373 reasoning plus 1,064 result tokens), identifying
real URL wrapping and above-center CTA labels without claiming acceptance.

Release `5b72c07b4a82e94d21ae8226aadd0f227d9029b4`, deployed at 08:24:56 UTC,
keeps medium reviewers with 16,384 output room, concise result instructions and
unchanged time, total spend, provider and acceptance limits. Diagnosis flags now
survive later full stage snapshots, covered by restart tests. A source-canvas
label edge case is also guarded, but was not the live failure cause: the actual
process already deduplicates the normalized source. All 259 tests, Ruff and
whitespace checks passed; health returned 200. The same canary resumed using
saved photos and the passing reusable result. Full end-to-end success is pending.

## Reviewer/preflight conflict feedback (08:35 UTC)

The same sample reached comparison two at 9.62 overall, but visual review
repeatedly requested a 48px Story CTA box while Unicode needed 78px. The repair
restored capacity and the next visual review repeated the old reduction. The
attempt was cancelled through the supported API during comparison four, leaving
three used lifetime comparisons and all saved evidence intact.

Release `b024998097ec0de299b7baede1a0b6bf85a0d2a3` deployed at 08:35:13 UTC
after 260 passing tests, Ruff and whitespace checks. Reviewers now receive
bounded durable actual fit-failure feedback and the prior diagnosis as advisory
context, explicitly requiring a fit-safe coordinated correction without reducing
editable limits or suppressing defects. Health returned 200 and the same run
resumed. Comparison four passed all five sections at 9.85 or above and all four
reusable scenarios passed. Independent final reviews are still required.

## Painted controls and payload isolation (08:53 UTC)

Final review still confused invisible multiline capacity with painted glyphs,
moving an above-center Story CTA upward. Release `78f3e200ab` added advisory
pixel measurements for isolated high-contrast centered button labels. Real saved
pixels measured Feed +5px and Story +28px required downward movement. Synthetic
tests prove changing invisible box height does not change the measurement.
Unknown/clipped evidence emits no measurement; no score or gate is derived from it.
The full 262-test suite passed. Deployment was at 08:44:06 UTC.

Two supported retries then failed with Meta HTTP 500 during final review. An
exact smaller-output probe also failed; a small image probe succeeded. Removing
new measurement context succeeded, and retaining it while omitting only the two
byte-identical duplicate production images succeeded in 60.43 seconds with a
valid review requesting the correct +5/+28px corrections. This isolates a
payload-sensitive failure, not a proved billing problem or general outage.

Release `c253ab3f232e166ce9fd7382fdb36e614b84ecea` deployed at 08:53:35 UTC
after 263 passing tests, Ruff and whitespace checks. Distinct production renders
and all diagnostics remain attached; only production images proven byte-identical
to the corresponding QA render are omitted. Unknown identity retains evidence.
Health returned 200. The same saved sample resumed at final review; successful
quarantined import and smoke-test evidence are still pending.

Release a359763ad3099eb3435ed125468e543584561ec8 deployed at 09:06:22 UTC
after 265 passing tests. It retains a below-gate working repair only when
measured prior targets are all resolved, fewer unrelated defects remain, and
pairwise evidence is not worse. Retention never grants acceptance or import.
This fixes logs 220/221 rolling back corrected CTA labels and hero geometry
because the comparator discovered a different footer defect.

The resumed run failed on Meta final review with HTTP 500 in 4.5 seconds.
An isolated request without prior reviewers' history completed in 52.36 seconds
with a valid review and correctly measured CTA corrections. Independent final
judges now receive current evidence and measured fit, without previous model
criticisms; the repair/comparator loop retains its history. This reduces request
growth and stale-judgment anchoring, without changing routes or acceptance.

Release b47c1d5bae5ca78bac722a63a2a7d12403f154a0 deployed at 09:14:54 UTC.
The run then passed comparator and both independent final reviews (events
273 and 279) after one targeted CTA repair. Handoff failed before import on
the builder's singular objective LEAD. Release
671e11f70c3e183513d723e7457a9e69fba2a170 deployed at 09:21:28 UTC after
265 tests, Ruff and whitespace checks. It maps LEAD to OUTCOME_LEADS and
normalizes before the final artifact render, keeping imported metadata aligned.
The next attempt failed again with Meta HTTP 500, so history removal alone
did not eliminate the transport failure. No provider-side input limit is proven.

An isolated three-image final request retained source, current Feed/Story and
all measured fit evidence, completed in 55.83 seconds, and returned correct
CTA targets. Final judges now omit overlay/difference diagnostics; these remain
with the comparator and as saved evidence. Distinct production images remain.
Release d8533355682d445a1eaae6e861b42d8340c88df1 deployed at 09:26:18 UTC
after 266 passing tests, Ruff, whitespace checks and authenticated health 200.
It also persists comparator-approved final repairs before subsequent calls;
an injected handoff failure test proves recovery selects the repaired candidate
without inventing final-review/import/smoke acceptance. Full handoff is pending.

## Complete successful handoff, 8 September 2026

Hermes 78f6f3db8fcad7370ad65b95130eb4c0a6acd549 deployed at 09:33:12 UTC
after 267 passing tests, Ruff and whitespace checks. Fully measured final
repairs now use the existing patch compiler and the same renderer/reusable
validators without a redundant model rewrite. Invalid/qualitative cases retain
bounded model repair. Event 336 proves the compiled path ran live.

The visual checks passed, but Blockwise import returned invalid_template_artifact.
Live app 031bf62a76fe2aa44276296e76a9b2e5703f62a3 still accepted only the
old generationReview shape. The current policy contract was applied on top of
that exact live base, not by replacing unrelated newer application changes.
Blockwise e09a5d6f9b141c2613d914e293c1d4bfd9521a00 deployed at 09:42:34 UTC;
its full tests, NUL/type/build checks, isolated compiled-route probe and public
compiled-revision health passed. Renderer a009543dd remains pinned separately.

Run trun_cf767d809b8c438497a1e9bc9676ea80 reached ready_for_review. Events
363/365/367/368 record accepted final review, four-asset quarantined import,
passed matching smoke test and completed handoff. Template open-house-estate-1080
has every comparator/final-reviewer section >=9.8, no issues, all effects matched
or absent, no_obvious_errors=true and all four reusable scenarios passed.
The actual serving first50 _quality_pass predicate returned true. The dry
manifest contains 50 distinct IDs and hashes; its batch directory remains empty.
No template was activated/published and the 50-template batch has not started.


## Completed Meta-native CTA revision

Run trun_cf767d809b8c438497a1e9bc9676ea80 completed its directed CTA removal:
no embedded CTA layers or CTA-only editable inputs remain; native Meta CTA
metadata and informational website details are retained. The initial revised
candidate passed its first comparison (iteration 15). Retries retained that
candidate while recovering an upstream HTTP 500 and the storage-cleanup bug;
no further design changes were needed. Events 426/428/430/431 prove final
review accepted, four-asset quarantined import, matching smoke pass and
ready_for_review on Hermes dc5e088681 and Blockwise de606ac66.
Every comparator and both final-reviewer section scores are >=9.85, issues=[],
all effects match/not_present, no_obvious_errors=true and reusable tests 4/4.
The serving first50 quality predicate passes. Batch not started; nothing
activated or published. Temporary canary removed; backups retained.


### Full-bleed Meta revision passed 8 September 2026

Run trun_cf767d809b8c438497a1e9bc9676ea80 completed its square-corner revision
under Hermes 4758d83a8c1c703801a73112ce9c4c0e790e07f2 (evaluation policy 11),
with existing Blockwise de606ac66 and renderer a009543dd unchanged. Its first
revised candidate passed comparator iteration 18. A supported retry recovered
one Meta HTTP 500 without changing the artwork. Both independent final reviews,
all four reusable scenarios, quarantined four-asset import and matching smoke
passed; the final status is ready_for_review. Every scored section >=9.85,
issues=[], no_obvious_errors=true; the serving first50 quality predicate passes.
Pixel inspection confirms fully opaque Feed 1080x1350 and Story 1080x1920,
photo-filled top corners and dark footer-filled bottom corners, no white corner
cutouts, zero outer radii and no image CTA layers. The batch remains unstarted;
nothing was activated/published. Previous Hermes dc5e088681 release and selector
backup /srv/hermes/backups/ad-template-feedback-20260908-4758d83a8c are retained.
