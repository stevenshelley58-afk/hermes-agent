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
