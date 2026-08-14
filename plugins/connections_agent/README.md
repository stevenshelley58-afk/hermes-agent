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

- `HERMES_CONNECTIONS_AGENT_KEY` authenticates Hermes-to-Frank action/receipt
  requests.
- `HERMES_VAULT_BROKER_KEY` authenticates Frank-to-Hermes broker requests.
- `HERMES_CONNECTIONS_INFISICAL_TOKEN` authenticates the fixed-scope
  Infisical CE v4 client.

The broker URL is:

`https://<hermes-host>/api/plugins/connections-agent/vault-broker`

Its fixed endpoints are `GET /health`, `POST /secrets/list-metadata`, and
`POST /secrets/create`, `/rotate`, and `/delete`. Mutation requests require an
`Idempotency-Key`; delete additionally requires a Frank confirmation token and
provider receipt. Responses are safe metadata receipts and never secret
values. Infisical project, environment, path, and secret name are fixed by
Hermes settings, not accepted from Frank.

The first adapter is the official Resend local MCP server. It is held at
`setup_needed` until the Frank rotation flow records a new rotation for
`RESEND_API_KEY`; activation uses `npx -y resend-mcp` with only `sendEmail` and
`getEmail` registered into Hermes's MCP tool surface.
