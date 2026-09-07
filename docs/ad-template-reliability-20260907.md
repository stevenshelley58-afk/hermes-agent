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

## Release gate

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
