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
