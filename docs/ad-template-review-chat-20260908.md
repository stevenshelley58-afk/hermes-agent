# Review chat implementation evidence, 8 September 2026

This is dated development evidence, not a claim of production deployment.
The current operator workflow is owned by Frank `docs/AD_TEMPLATE_GENERATOR.md`.

The candidate implementation adds structured feedback to the existing
authenticated request-changes controller and a scoped revision-history read.
Hermes owns a new `review_revisions` SQLite table beside ToolRunStore. Frank
only validates scope and forwards requests. No second agent loop or provider
route is introduced.

Revisions use the existing single-writer lifecycle claim, optimistic checkpoint
revision, payload-bound idempotency, and immutable snapshots of both final
production images. Undo restores a detached editable candidate into a new
revision, retaining history and running the normal comparison, reusable,
independent-review, quarantine-import and smoke checks. It never inherits
approval. Existing approved runs remain locked. Failed/uncertain remote discard
cannot advertise the old import as ready.

Tests: 278 generator tests passed across 24 files, including final-artifact
asset ordering/embedded-byte normalization, snapshot integrity, scope, stale
revision, duplicate/conflicting requests and the queued executor start.
A read-only copy of the real completed sample's final artifacts also passed
the snapshot contract in a temporary workspace. The production run was not
modified. Frank's isolated browser journey passed on desktop and mobile with
all provider/API writes intercepted by fixture handlers.

Before release, preserve an online SQLite backup and the gateway selector using
the existing owner runbook. The added table is created lazily without changing
existing ToolRunStore rows. Retain the previous Hermes release
`/opt/releases/hermes-template-4758d83a8c` for rollback. No new release has been
activated by this evidence record.
