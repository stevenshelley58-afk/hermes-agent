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

## Current seed-14 process (5 September 2026)

The source is durable before renderer preflight. Each run freezes independent routes:
Contributor for analysis/build/repair, Gemini 3.8 Flash for comparison and final review A, Contributor for final review B, and Muse Image 1.0 for the reciprocal aspect reference and one generated example per eligible photo slot. The lifetime allowance is six comparisons (four normal plus two escalation); automatic retries do not reset it. A manual Frank revision may receive one new bounded allowance.

The quality gate is 9.8 across the required likeness dimensions. Targeted JSON patches preserve the best candidate; OCR is source evidence only and never rewrites candidate text. No automatic geometry expansion is performed. Final reviewers inspect both source-filled comparisons and the actual neutral production render. The same run-local demo bytes are used by QA, the final production render and quarantined Blockwise import.

A passing run stops at Ready for Review. It is not active in Blockwise until Frank approves it.

## Pilot status

Pilot trun_5ee434b975ac4f188ff0736694f98af7 is running; no pass or completion is claimed. Live Hermes revision is b2775efed9286dc9b7d6325108114a21e796b35a; renderer/Blockwise revision is d39771a94134c28081185bcf93a8d5e1947a39a4.

## Qualified low-cost profile (5 September 2026)

Seed 14 uses Meta Direct Muse Spark 1.3 Contributor for building and bounded
repair, Concentrate Gemini 3.8 Flash for comparison, and one of each for the
two completion reviews. Image references and demonstration photos use Meta
Direct Muse Image 1.0. These are independent of the main Hermes chat selection.
Each run freezes its routes; there is no hidden expensive fallback.

Raw image plus strict-JSON probes succeeded on both vision routes. The models
also identified address overflow, duplicated postcode, missing contacts and
stray punctuation in a damaged source006 render. Model scores are evidence,
not proof of pixel identity: renderer checks and manual publication review remain.

QA preserves authored text. OCR provides initial source evidence only; moving
a text box cannot add words or silently expand its geometry. Final reviewers
receive the actual neutral production render as well as source comparisons.

After likeness passes, photo-default slots receive run-local generated examples
once. A persisted plan reuses completed images on retry. The exact same PNG bytes
go through the shared renderer and quarantined Blockwise import. Logos remain
editable brand-kit inputs. Unknown interrupted image-call outcomes stop for
receipt inspection rather than risking duplicate charges. Approval still happens
in Frank's Ready for Review section before customer gallery activation.
