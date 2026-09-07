# Ad template reliability verification

## Failure classes and intended safeguards

The saved meta044 review put geometry values before the layer name. The old
parser discarded those values and failed before it could request a repair.
New nonempty review `targets` specify the layer, property and value explicitly;
legacy reviews retain bounded compatibility. Invalid IDs, values and bounds
are rejected, and every authoritative target must be satisfied or already
current. Empty targets represent structural work for the guarded general
repair path, not an approval or permission to ignore the issue.

The final-review branch previously validated a patch's shape inside its retry
but applied it afterward. Build-time asset repair likewise checked asset
bindings after its repair callback had completed. These validation boundaries
now include the actual candidate application and pure binding checks so a
bad correction reaches the existing bounded retry with a useful error.

Preserve immutable best candidates, lifetime comparison and spending limits,
source durability, frozen model policies, independent final review, reusable
template checks, quarantine and explicit approval. Do not reset historical
runs or remove their failure evidence.

## Pre-release checks

- Saved meta044 event 53 replay now derives the correct targets for its first
  four geometry/typography groups. Its shared CTA-colour issue remains an
  explicit structural repair case, not an unsafe shared-role colour change.
- Saved meta039 is missing the `brand_logo` image-input declaration. Adding
  that declaration in memory passes binding validation without changing asset
  definitions or bytes. No original run file was changed by this diagnostic.
- Layer refinement suite: 31 passed. Preserved source, spending, provider
  routing and reusable-template guard suites: 14 passed.

## Observed production diagnostics

- Authenticated `GET http://172.16.1.1:8642/v1/health` returned HTTP 200 and `{"status":"ok","version":"0.20.1"}`. The API key was read in memory from `frank-window` and never printed.
- Authenticated `GET /v1/tool-runs/trun_26468241dd624778bccc976b2b313444` returned HTTP 200 with `status: failed`.
- The pinned runtime was `/opt/releases/hermes-template-a11673d048`, using Hermes v021 Python. No active runs were observed.

## Saved-candidate renderer smoke

The saved candidate was loaded read-only. Persisted inline asset bytes were supplied as run-local overrides to pinned `run_renderer` in a temporary workspace. No original run files, imports, approvals, retries, or model calls were made.

- Result: passed. Template `open-house-modern-26468241`
- Feed: 1080x1350 PNG, 1,449,666 bytes, SHA-256 prefix `2e1d54d3f9214999`
- Story: 1080x1920 PNG, 1,792,676 bytes, SHA-256 prefix `3c2e6f8362951465`
- Resolved assets: 4

The initial direct replay rejected persisted `bytesBase64` declarations as invalid builder output. The successful smoke removed those bytes from declarations and passed them only as run-local overrides. This confirms renderer handling, not candidate approval.

## Reproduction and rollback

Read `HERMES_API_KEY` only inside `frank-window` for authenticated GET requests to `/v1/health` and `/v1/tool-runs/<id>`. For smoke, use pinned `PYTHONPATH=/opt/releases/hermes-template-a11673d048`, `/home/hermes/.hermes/hermes-agent-v021/venv/bin/python`, `gateway.ad_template_generator_process.run_renderer`, decoded in-memory asset overrides, and a temporary workspace.

Backup: `/srv/hermes/backups/ad-template-reliability-20260907-v1w8iel0`, created 09:44:13 UTC, containing online SQLite (90,390,528 bytes), gateway override, and meta044 checkpoint.

Rollback uses the saved override and prior immutable release documented in `docs/ad-template-best-refinement.md`. Preserve intervening state and policy history.

## First deployment and live canary

Release 07c4f3de5113602f5e3573c5d3135297952c917b was deployed through
an immutable release directory and verified in the gateway process environment.
The affected meta044 run was resumed using the normal retry API, with its
source, best draft, policy and lifetime budgets preserved.

The canary passed the original parsing failure and reached an absolute 9.6
comparison at iteration 8. It then exposed a control-flow defect: an accepted
draft rated the same as the saved best was discarded, after which the controller
asked for a repair with no issues. It ultimately failed at iteration 10 trying
to change coordinates already correct in the restored best. No approval or
publication was performed.

## Follow-up safeguards

A passing absolute review for the identical saved best now proceeds to the
independent final gates. A different passing draft that is not better than the
saved best causes a fresh comparison of the restored best, not an empty repair.

Fully measured corrections are compiled into exact bounded operations and pass
the existing patch, render and text-ink checks. Already-current targets trigger
fresh comparison without a builder call or inferred acceptance. Qualitative,
structural and unsafe dependency work still uses the bounded repair path.
Final reviewers likewise recheck an unchanged draft when all their measured
targets are already present, within the existing final-round limit.

Structured geometry targets no longer unlock text-input changes based only on
words such as text, copy or bullets in the explanation. The strict review schema
lists supported target properties instead of inviting unsupported fields.

The saved iteration-10 failure replay now returns zero operations across its two
groups and no inferred input dependencies. All ten coordinates already match
best iteration 6. This diagnostic did not mutate the original run.

The latest full affected suite passed **199 tests, 0 failures, 2 existing skips**
across 17 files in 14.0 seconds. Ruff and whitespace checks passed. Six new real
orchestrator scenarios cover same/worse identical-best acceptance, different
passing-draft restoration, rebased no-op targets, and final-review no-op rechecks.
They preserve lifetime comparison counts and verify independent final reviewers
and quarantine. Existing fallback-specific tests explicitly exercise model
repair; deterministic compilation is tested separately.

Deployment and canary results are recorded in
`/srv/hermes/deployment-records/ad-template-reliability-20260907.json`.
Quality limits, budget exhaustion and explicit approval remain real controls;
these changes do not guarantee that every source image will pass visual review.

## First-release gate

The final affected suite passed 186 tests with 2 existing skips across 17 files
in 14.5 seconds using the canonical hermetic runner. Ruff and whitespace checks
passed. The exact final-review missing-property path is exercised through the
orchestrator and succeeds on its bounded retry; the structural compare test
also proves re-comparison and intact acceptance gates.

A real shared-renderer replay under the new source produced byte-identical
Feed and Story outputs to the pre-change replay. No model calls or original
run writes were made for this smoke.

The validation-only environment was completed with dependencies already pinned
in uv.lock. An existing photo-import test also failed on the unmodified base
034ebb3b78 because its fixture omitted the already-required publication
objective; the fixture now supplies that field, retaining the exact-byte
assertions and the production validation guard.

Fresh pre-deploy SQLite and override backups are state.pre-deploy.db and
only-ad-template-process.pre-deploy.conf in the backup directory above.
The running release was verified unchanged and no Tool runs were active.


## Follow-up deployment result

Code release **33d59a1d9d6ccced7729f2d6ef74e0ce71e1c2e3** was deployed
at 2026-09-07 10:44:23 UTC. The actual gateway process environment referenced
`/opt/releases/hermes-template-33d59a1d9d`, its import probe passed, and its
authenticated health endpoint returned HTTP 200. No active runs were interrupted.
Fresh pre-second-deploy database, override and checkpoint backups are in the
same restricted backup directory noted above.

The normal retry API resumed meta044 at lifetime comparison 10. The live run
successfully used deterministic measured-target patches at iterations 12-14.
At iteration 15, all requested coordinates already matched saved best 6; the
new controller rechecked without a builder call, then reached comparison 16.
The original parsing, invalid empty-repair and no-material-change faults did
not recur on this retry.

**The canary did not pass the visual quality gate.** It stopped with
`exact-clone quality loop exhausted 16 comparisons below 9.5`; its final
comparison scored 9.2. Best iteration 6 and the failure evidence remain intact.
The controller's cost/comparison limits, frozen routes, independent final
review and explicit approval were not changed. No approval or publication
was performed, and no fresh run was started to bypass the exhausted budget.

This is verified remediation of the software failure classes, not evidence
that the entire visual-generation outcome is solved. The remaining task is a
focused design/review decision for the saved draft; repeating an unchanged
retry cannot supply more comparison budget or establish a quality pass.


## Measured reconstruction and explicit revision cycle

The user explicitly requested continued correction until templates pass, followed
by the first 50 templates. This authorizes a new directed revision, not silent
retry-budget resets or approval. Failed/cancelled/blocked build runs now support
the same bounded request-changes operation when a candidate checkpoint exists.
The API atomically claims a terminal run before checkpoint mutation, preserves
frozen policy and durable usage, discards any imported quarantine via the normal
path, and restores terminal status/error if setup fails. Concurrent requests
cannot revise the same checkpoint twice.

Visual inspection found major defects that scalar comparison scores had missed:
the hero ended near630 instead of674; the gallery extended behind the footer;
body text and headline were displaced; the CTA text was not centered. An offline
source-measured correction uses Bodoni Moda400 for the high-contrast headline,
correct panel bounds and slightly negative body tracking at the unchanged24px
readability floor. All four real shared-renderer reusable scenarios passed.
This is render/reusability evidence, not a live quality pass.

Comparison evidence now pairs high-confidence source/render OCR words with their
declared editable text input and layer box. Ambiguous, low-confidence, nonfinite
or insufficiently matched words are omitted. Bounded ink offsets and thin
source-edge bands are advisory only. They cannot patch, score or approve a draft.
All four comparison/restoration paths supply this evidence. Text-free sources
avoid a redundant OCR pass. Build, ordinary review and stall diagnosis share the
same photo-neutrality, font-substitution and measurement constraints.

The generator previously advertised7 fonts despite118 shipped renderer faces.
It now discovers the active renderer manifest, verifies safe filenames, licence
metadata and SHA256 bytes, and exposes only verified font paths. Missing legacy
manifests retain the established fallback; invalid present manifests fail closed.
No font was downloaded or immutable renderer release modified.

The final affected coverage totals231 passing tests and2 existing skips across
20 files. The initial full run exposed one legacy text-free-source assertion;
avoiding unproductive render OCR preserved that original assertion unchanged.
The affected process/measurement files then passed64/64 in13.4s. Ruff and
whitespace checks passed. A read-only actual-source probe found118 fonts,
completed render OCR, body21 matched tokens with80.77% coverage, offset(-1,+1),
and source boundary bands near674 and1142. No quality or approval gate changed.
Live revision results will be recorded after deployment.


### Live event-boundary correction

Release de46a2e7cd deployed at12:18 UTC and the explicit meta044 revision
returned202. The39-operation directed correction applied; three unsupported
stroke fields were repaired by the existing renderer-contract recovery. The
first render then exposed a persistence-boundary bug: matchedTokenCount was
rejected as secret-bearing. No new comparison had been consumed; the corrected
candidate remained durable with manualRevision1 and comparisonBudgetUsed0.

The advisory field is now matchedWordCount. A real ToolRunStore.append_event
regression persists the actual helper evidence and confirms api_token is still
rejected. No secret guard was relaxed. Effective nonfinite and exact16-entry
bound tests were added. Focused helper/event tests passed10/10; combined with
ToolRunStore,33 passed and2 existing skips. The next attempt uses normal retry
of the same saved revision, not another comparison-budget reset.


## Passing reviewers exposed a reporting-only handoff failure

On the directed meta044 revision, comparisons23,24 and25 passed all six9.5
quality floors. Final-review event339 independently recorded Gemini accepting
at9.6 overall and Muse accepting at9.7 overall, both with no issues and all
effects matching or absent. The run nevertheless stopped at event340 with
"final reviewers requested revision without actionable issues".

The controller compared the reviewers' free-text font-substitution descriptions
for exact equality with the comparator's description. One reviewer described a
substitute font by its shipped file path; the other reported no substitution.
That descriptive disagreement was not an actionable visual rejection, but it
overrode both validated accept decisions. The correction removes only that
free-text equality condition. Raw reviewer reports, all score/effect gates,
independent routes, verified font files, reusable checks, quarantine and explicit
approval remain required. A normal retry of this same saved revision is the
next live check; this does not create new comparison budget.

The first50 queue requires compact reusable-validation evidence in the normal
success monitor output. That projection retains the actual validated scenario
names, identities and statuses; it must not synthesize passing evidence.

The final six-file affected suite passed105 tests with0 failures in14.0s.
Real orchestrator coverage now passes differing font descriptions and a null
second report through generation-review validation, quarantine import, bounded
output and the first50 quality checker. Separate real cases verify typography
9.4 and duplicate reviewer routes never import. Missing, empty, partial, failed
or overlong reusable evidence is rejected by the success projection. Ruff and
whitespace checks passed. The obsolete equality-only helper/test were removed
in the focused simplification pass.
