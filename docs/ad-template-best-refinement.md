# Historical: Best-candidate refinement (6 September 2026)

This dated evidence records a prior implementation. Use /projects/frank/docs/AD_TEMPLATE_GENERATOR.md for current cross-system procedures and live status.

This change extends the existing exact-clone controller; it does not introduce a
second generator or change Blockwise activation/publishing permissions.

## Runtime and policy

The gateway imports the immutable template release selected by
`/etc/systemd/system/hermes-gateway.service.d/only-ad-template-process.conf`.
The old `/projects/only-process-hermes` main checkout is not deployment evidence.
This work starts from the actual production revision `f5992a681d`.

New runs use policy seed 15. Ordinary builder/comparator/final-review routes are
unchanged. The optional quality-escalation route is
`concentrate/gpt-6-astra`, used only for one stall diagnosis, not routine repairs.
Historical runs retain their frozen model policies.

The Astra route was qualified against the configured Responses endpoint with a
real image and the strict diagnosis schema. It returned a correct description
and a completed structured response. This verifies transport/capability, not ad
quality. The request used 1,592 input and 86 output tokens.

The live ordinary comparator also accepted the updated strict response schema
with a real saved production candidate and attached source/current/best images.
When current and best were identical, it correctly returned `comparisonToBest:
same` (24,351 input and 407 output tokens). The serialized pairwise request was
890,519 bytes, below the existing 1.5 MB transport bound. This checks request
transport and tie recognition, not a claim that the existing ad is ready to ship.


## Refinement behaviour

- Supply the editable candidate and its rendered evidence alongside the source.
- Reject equal/worse attempts and restore the immutable best before repairing.
- Count consecutive non-improvements; after five, request one frontier diagnosis.
- Persist the diagnosis request before making the provider call so retries cannot
  silently charge for a second diagnosis after an uncertain result.
- Continue ordinary builder work using the diagnosis and best candidate.
- Keep the total comparison bound at 16 and preserve existing durable cost,
  cancellation, renderer validation, final review and quarantine controls.
- Failure/exhaustion preserves the checkpoint; it does not approve a template.

The combined affected gateway suite passed 137 tests, with 2 existing skips.
The forced-stall tests cover both valid and malformed frontier responses,
interruption after diagnosis, same-workspace resume without a second frontier
call, tie rollback, and recovery excluding discarded attempts. A real shared
renderer smoke produced 1080x1350 Feed and 1080x1920 Story outputs from a saved
candidate without modifying or activating its original run.

Layer repairs attach the source, BEST Feed/Story, and up to three detailed crop
pairs; every group's deterministic contract and text-ink checks still run. The
existing total transport bound remains enforced rather than dropping core images.


## Verification and release

Run the affected gateway tests through `scripts/run_tests.sh`, including the
exact-clone controller, layer refinements, structured transport, frozen policy,
source durability and usage-budget tests. Verify actual shared-renderer output
against a saved candidate separately from mocked model-control tests.

Before deployment, back up the gateway override and the SQLite state using its
online backup API. Release only the committed revision in a detached immutable
worktree; update the existing override and restart `hermes-gateway.service`.
Do not restart unrelated services or restore an entire old state database over
new user activity. Verify the running process import path, authenticated
`/v1/health`, the new default model policy, and the public Frank route.

Rollback uses the saved override and previous immutable release. If new policies
were appended, append the saved compatible default policy using the existing
policy API before rolling the runtime back; preserve all existing runs and
policy history. Use a full database restore only when no intervening state
would be lost or under a separately verified recovery procedure.

## Cost accounting

Live provider pricing remains preferred. When its catalogue lacks Astra prices,
the generator records a standard-list-rate **estimate**, not a claimed invoice:
$10 input, $1 cached input and $50 output per million tokens. Diagnosis payloads
are bounded well below the long-context pricing threshold.

Sources verified 6 September 2026:
[OpenAI model pricing](https://developers.openai.com/api/docs/models/gpt-6-astra)
and [Concentrate's no-markup pricing](https://concentrate.ai/pricing).
