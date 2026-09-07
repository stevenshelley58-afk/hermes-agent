# Ad Template First 50

The driver selects exactly 50 unique curated sources by numeric `meta_<id>.png` ID. Source roots are searched latest-first, then identical SHA-256 content is deduplicated. The default command is read-only and makes no batch, staging, or HTTP writes:

```sh
python3 scripts/ad_template_first50.py
```

To submit, run the bounded resumable queue explicitly:

```sh
HERMES_API_KEY=… python3 scripts/ad_template_first50.py --run \
  --batch-id first50-20260907 --max-active 2
```

The default endpoint is `http://172.16.1.1:8642`; override it only for a controlled test with `--endpoint`. The source copies are staged beneath `/srv/frank/data/window/uploads/ad-template-first50/<batch-id>/`, which is the configured Frank intake accepted by the gateway. No source bytes or credentials are printed.

`--run` takes an exclusive process lock for the batch and atomically creates an immutable `manifest.json`, resumable `state.json`, and append-only `ledger.jsonl` under `/srv/ad-template-generator/batches/<batch-id>/`. Existing state is reconciled on restart. Any source path, content hash, batch identity, or stable request identity change fails closed.

Each request has a batch-and-source scoped `request_id` and `idempotency_key`. A pending intent is durable before POST. Lost/5xx responses are reconciled through `GET /v1/tool-runs` and retried at most once with the same identity and payload. The queue polls the full ad-template collection, counts all active build statuses, and never schedules beyond `--max-active` (default 2). A failed, cancelled, blocked, malformed, or missing server state stops new scheduling and leaves already-running work untouched.

A `ready_for_review` run counts as passed only when its bounded monitor output includes a template summary, comparator scores at or above 9.5, two distinct final reviewers with complete passing scores/effects and no issues, a quarantined import plus passing smoke test, and `reusable_validation` evidence for all four scenarios (`counts.total == counts.passed == 4`). The driver never calls retry, request-changes, approve, publish, or cancel APIs. A nonzero exit means the batch is incomplete or failed and should be inspected from its durable state/ledger before any operator action.
