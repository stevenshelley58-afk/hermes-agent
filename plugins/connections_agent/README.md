# Connections Agent plugin

This is the private Hermes-side runtime for the Connections workflow. It is
bundled in the canonical Hermes checkout and is disabled until the operator
enables its namespaced settings. It never creates another Hermes profile or
agent runtime.

Enable it in the single `default` profile with non-secret settings like:

```yaml
plugins:
  entries:
    connections-agent:
      settings:
        enabled: true
        frank_url: https://frank.example.invalid
        infisical_url: https://infisical.example.invalid
        infisical_project_id: project-id
        infisical_environment: dev
        secret_path: /connections
        resend_secret_name: RESEND_API_KEY
```

Hermes receives these credentials only from its runtime environment:

- `HERMES_CONNECTIONS_ENABLED`, `HERMES_CONNECTIONS_FRANK_URL`,
  `HERMES_CONNECTIONS_INFISICAL_URL`,
  `HERMES_CONNECTIONS_INFISICAL_PROJECT_ID`,
  `HERMES_CONNECTIONS_INFISICAL_ENVIRONMENT`,
  `HERMES_CONNECTIONS_INFISICAL_SECRET_PATH`, and
  `HERMES_CONNECTIONS_RESEND_SECRET_NAME` are the authoritative runtime
  settings. They take precedence over the namespaced plugin settings above;
  plugin settings are the fallback when the corresponding env var is absent.
- `HERMES_CONNECTIONS_AGENT_KEY` authenticates Hermes-to-Frank action/receipt
  requests.
- `HERMES_CONNECTIONS_BROKER_KEY` authenticates Frank-to-Hermes broker
  requests. `HERMES_VAULT_BROKER_KEY` is accepted only as a legacy fallback.
- `HERMES_CONNECTIONS_INFISICAL_TOKEN` authenticates the fixed-scope
  Infisical CE v4 client.

The broker URL is:

`https://<hermes-host>/api/plugins/connections-agent/vault-broker`

Its fixed endpoints are `GET /health`, `POST /secrets/list-metadata`, and
`POST /secrets/create`, `/rotate`, and `/delete`. Mutation requests require an
`Idempotency-Key`; delete additionally requires a Frank confirmation token and
provider receipt. Responses are safe metadata receipts and never secret
values. Action completion outcomes are limited to `created`, `updated`,
`verified`, `synced`, `revoked`, `deleted`, and `failed`; failed completions
carry only an opaque provider receipt plus safe error code/category. Infisical
project, environment, path, and secret name are fixed by Hermes settings, not
accepted from Frank.

The first adapter is the official Resend local MCP server. It is held at
`setup_needed` until the Frank rotation flow records a new create or rotation
for `RESEND_API_KEY`; activation uses pinned
`npx -y resend-mcp@2.13.0` with only the exact current `send-email` and
`get-email` tools registered into Hermes's MCP surface. Registration reports
`connected-awaiting-verification`; only a later authenticated provider
operation with an opaque receipt can produce `verified`.
