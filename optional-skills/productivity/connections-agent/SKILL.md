---
name: connections-agent
description: Plan and execute safe provider connection changes.
version: 0.1.0
author: Steven Shelley (stevenshelley58-afk), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  category: productivity
  profile: default
  model: Luna
---

# Connections Agent

Use this skill for the private Connections Agent session inside Hermes's one
`default` profile. The session label is **Connections Agent**; it is not a new
Hermes profile, agent runtime, database, or memory store.

## Operating contract

1. Discover and verify provider state before proposing a change. Safe actions
   are discovery, verification, planning, and metadata sync. Start with the
   bounded private Frank inspect projection (`activity_limit` 1..50) when
   current connection, attention, or activity state is needed.
2. Manual and agent mutations go through Frank's action/receipt contract. Use
   the Connections tool to send an inner provider `action` (`discover`,
   `create`, `update`, `verify`, `sync`, `revoke`, or `delete`) inside the
   transport `plan` request, then apply with the returned nested
   `plan.plan_id`. Apply/completion must use a new idempotency key, distinct
   from the plan request key. Include the literal profile `default` and
   preserve the nested action/connection metadata returned by Frank. The
   model-facing apply body accepts only `plan_id` and an optional confirmation
   token. Provider receipts, outcomes, and provider error fields are never
   model-supplied; only an executed Hermes adapter may create server-bound
   evidence for completion.
3. A destructive revoke or delete requires a Frank-issued confirmation token
   and a provider receipt. Never synthesize either value. If either is absent,
   stop at a safe refusal.
4. Return only safe metadata: provider, capability, state, plan id, receipt
   id, timestamps, and error class. Never return, quote, log, or persist a
   secret, bearer token, Infisical credential, request body, or MCP environment.
5. Completion outcomes are allowlisted: `created`, `updated`, `verified`,
   `synced`, `revoked`, `deleted`, or `failed`. A `failed` completion must
   contain only an opaque `provider_receipt`, an allowlisted
   `provider_error_code`, and an allowlisted `provider_error_category`; never
   forward provider error text, bodies, traces, or messages to Frank.

## Resend MCP first adapter

Resend remains `setup_needed` until the operator enters a newly rotated key in
Frank. Do not ask for, accept, or repeat the previously exposed key. Frank's
fixed rotate flow sends the new value to Hermes; Hermes writes it to the
configured Infisical CE project and records only safe rotation metadata.

After the create/rotation receipt is complete, activate the official Resend
MCP server with pinned `npx -y resend-mcp@2.13.0`. Its runtime secret comes from Infisical on
Hermes and is injected only into the MCP subprocess. Expose only the approved
capabilities `email.send` and `email.status`, mapped to the exact MCP tools
`send-email` and `get-email`; do not expose a general Resend or generic secret
proxy. Registration is only `connected-awaiting-verification`; report
`verified` only after an authenticated provider operation returns an opaque
receipt, never after package registration alone.

## Infisical CE boundary

Use only the fixed Infisical v4 Secrets API operations implemented by the
Connections broker: list metadata, create, rotate, and delete for the
configured project, environment, path, and `RESEND_API_KEY` name. Metadata
requests set `viewSecretValue=false` and
`expandSecretReferences=false`. No enterprise sync endpoint, secret reveal
route, generic proxy, arbitrary project/path, or credential forwarding is
allowed.

The broker prefers Infisical Universal Auth using the Hermes environment
variables `HERMES_CONNECTIONS_INFISICAL_CLIENT_ID` and
`HERMES_CONNECTIONS_INFISICAL_CLIENT_SECRET`, with optional
`HERMES_CONNECTIONS_INFISICAL_ORGANIZATION_SLUG`. It exchanges them in
memory, refreshes the short-lived token once after expiry or HTTP 401, and
never persists or returns credentials. `HERMES_CONNECTIONS_INFISICAL_TOKEN`
is a deliberate static-token alternative.

When a provider operation succeeds, an internal Hermes adapter must bind its
safe provider receipt to the Frank plan/action before completion. Resend
verification, sync, revoke, and delete remain non-success until such an
adapter operation exists. If the provider is unavailable, keep the state
unchanged and report `setup_needed` or a safe verification failure.
