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
   are discovery, verification, planning, and metadata sync.
2. Manual and agent mutations go through Frank's action/receipt contract. Use
   the Connections tool to send `plan` first, then `apply` only with the
   returned action id and the exact idempotency key. Include the literal
   profile `default` and preserve the actor/receipt fields returned by Frank.
3. A destructive revoke or delete requires a Frank-issued confirmation token
   and a provider receipt. Never synthesize either value. If either is absent,
   stop at a safe refusal.
4. Return only safe metadata: provider, capability, state, action id, receipt
   id, timestamps, and error class. Never return, quote, log, or persist a
   secret, bearer token, Infisical credential, request body, or MCP environment.
5. Completion outcomes are allowlisted: `created`, `updated`, `verified`,
   `synced`, `revoked`, `deleted`, or `failed`. A `failed` completion must
   contain only an opaque `provider_receipt`, an allowlisted `error_code`, and
   an allowlisted `error_category`; never forward provider error text, bodies,
   traces, or messages to Frank.

## Resend MCP first adapter

Resend remains `setup_needed` until the operator enters a newly rotated key in
Frank. Do not ask for, accept, or repeat the previously exposed key. Frank's
fixed rotate flow sends the new value to Hermes; Hermes writes it to the
configured Infisical CE project and records only safe rotation metadata.

After the rotation receipt is complete, activate the official Resend MCP
server with `npx -y resend-mcp`. Its runtime secret comes from Infisical on
Hermes and is injected only into the MCP subprocess. Expose only the approved
capabilities `email.send` and `email.status`; do not expose a general Resend
or generic secret proxy.

## Infisical CE boundary

Use only the fixed Infisical v4 Secrets API operations implemented by the
Connections broker: list metadata, create, rotate, and delete for the
configured project, environment, path, and `RESEND_API_KEY` name. Metadata
requests set `viewSecretValue=false` and
`expandSecretReferences=false`. No enterprise sync endpoint, secret reveal
route, generic proxy, arbitrary project/path, or credential forwarding is
allowed.

When a provider operation succeeds, send its safe provider receipt through
Frank's `apply` action before reporting completion. If the provider is
unavailable, keep the state unchanged and report `setup_needed` or a safe
verification failure.
