# Ad-template generator provider routing

The ad-template generator uses a frozen, per-run model policy. Its provider
selection is independent of the main chat model and does not use the gateway
fallback chain. A route is usable only after it is present in the audited
generator capability catalogue and passes the preflight resolver.

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
