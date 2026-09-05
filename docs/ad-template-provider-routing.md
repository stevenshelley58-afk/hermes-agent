# Ad-template generator provider routing

The ad-template generator uses a frozen, per-run model policy. Its provider
selection is independent of the main chat model and does not use the gateway
fallback chain. A route is usable only after it is present in the audited
generator capability catalogue and passes the preflight resolver.

The execution owner is Hermes: it performs source analysis, rendering, comparison, targeted patches, final checks, import and smoke testing. Frank's UI is the human control surface: only its Ready for Review approval permits Blockwise activation. The main chat model remains independent.

Named provider entries belong in `~/.hermes/config.yaml`; secrets remain in
`~/.hermes/.env`. For example, a Responses-compatible endpoint may be defined
without placing its key in the configuration file:

```yaml
providers:
  meta-direct:
    base_url: https://api.meta.ai/v1
    api_mode: codex_responses
    key_env: META_MODEL_API_KEY
    model: muse-spark-1.3-contributor
```

Hermes resolves named endpoints through its `custom` transport. Generator
preflight therefore verifies both the transport and the resolver's original
`requested_provider` identity. It never treats an arbitrary `custom` endpoint
as equivalent, and it never falls back to a different provider for a frozen
role.

Adding an entry does not make it selectable by the generator. A candidate must
first pass a small real image plus strict structured-output qualification within
the approved budget. Until then the audited policy and its defaults remain
unchanged.

## Current process (6 September 2026)

Each run freezes its routes independently of the main chat: Meta Direct Muse
Spark 1.3 Contributor builds/repairs; Concentrate Gemini 3.8 Flash compares;
one of each performs final review. Muse Image 1.0 generates demo photographs
only. There is no hidden expensive fallback.

The original source is the sole design authority. Its matching placement is
reconstructed as closely as editable layers allow. Vision plans the other native
aspect directly from that source. No generated whole-ad target is cropped,
stretched or treated as a second source. Pixel overlays apply only to the matching
aspect; the native adaptation is reviewed for faithful design preservation.

Source bytes survive preflight failures/retries. Structured output and Blockwise's
shared contract catch malformed documents before visual reviews. OCR is evidence,
not a mechanism for rewriting text. Patches preserve unaffected layers and the
best candidate. No automatic geometry expansion. Six comparisons are allowed
across automatic retries; explicit manual revision can start a new bounded cycle.

Both placements need the 9.8 gate, no obvious production defects and two accepted
completion reviews. Final reviewers inspect actual neutral renders. Scores are
evidence, not proof of pixel identity. Generated photos are reused on retry;
the same bytes are rendered, reviewed and imported into quarantine. Unknown image
call outcomes require receipt inspection, not blind repeated charges. Only
approval in Frank's Ready for Review activates the customer template.

## Verification status

Source006 current pilot: `trun_f2b0848ec6a14308a4c08cd513422d8c`. The source is
mapped, without cropping, onto the matching output canvas before OCR/comparison,
so model coordinates and editable layer coordinates use the same pixel units.
The first render measured 95.46% pixel similarity, but this is not a likeness
approval: its best visual score was 9.2 and the six-comparison cycle failed.
No template from this pilot was activated.

Restart checkpoint modes and lifetime budget now survive fresh stage snapshots.
Fallback patch calls use the operations schema, not the full builder schema.
Focused exact-clone tests: 41 passed; structured-runtime tests: 9 passed.
Runtime import root must contain both template and Mini policy code; the gateway
working directory must not shadow that immutable release with an old checkout.

Live VPS source: `b97c8553c2b68321dd4181fed71a3e73d79c3831`, deployed from
`/opt/releases/hermes-template-b97c8553c2`. Manual corrections establish a new
best-candidate boundary without deleting prior iterations; automatic retries do
not reset the comparison budget. Escalation uses the current cycle, not historical
iteration numbering. Rejected patches fail visibly instead of reporting a no-op
as a successful edit. Logo layers retain their neutral production asset during
QA, avoiding false clipping introduced by source-advertiser crops. Restored best
candidates receive matching saved visual evidence. The pilot still requires
visual acceptance; deployment of these fixes does not approve a template.
