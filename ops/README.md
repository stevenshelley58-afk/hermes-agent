# Hermes knowledge controls

Frank owns and installs the canonical root helper at
`/usr/local/sbin/frank-knowledge-deploy`. Hermes invokes only that fixed,
argument-less path; it accepts no path, image, namespace, environment override,
or shell fragment. Check / retry intentionally calls the same idempotent
Frank-owned action so its final deployment check is authoritative.

An idempotency key is reserved before an action starts; a failed action is not
replayed with the same key. The UI refreshes a session-bound CSRF token and
generates a fresh idempotency key for Check / retry.

Each helper verifies that `/projects/frank` is clean, that both `HEAD` and
`origin/main` equal the reviewed Frank revision, and that it invokes only the
committed absolute deploy/check script. Output is bounded to a single generic
status line so deployment logs and projection payloads never enter the Hermes
UI or logs. A non-blocking filesystem lock makes concurrent actions fail closed.

The Frank release lane installs the helpers and least-privilege sudo rule.
The existing broad `hermes NOPASSWD:ALL` grant is in the host-owned
`/etc/sudoers.d/hermes-full-access`; it must be removed with a root-owned
backup and `visudo` validation before production activation. Hermes never
adds a generic command endpoint or a second helper implementation.
