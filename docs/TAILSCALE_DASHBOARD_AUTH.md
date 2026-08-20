# Tailscale Serve dashboard authentication

Hermes can create its normal dashboard session from a Tailscale Serve identity. This is an operator convenience, not a replacement for the Hermes session cookie: Tailscale authenticates the request, then Hermes mints its own HttpOnly/Secure/SameSite session cookies.

## Trust boundary

The explicit Serve mode is enabled only when all of these are true:

- Hermes binds to 127.0.0.1 (or ::1).
- dashboard.tailscale_auth.allowed_users is a non-empty exact allowlist.
- dashboard.tailscale_auth.public_host is the exact HTTPS Serve hostname, without a scheme, path, port, wildcard, or userinfo.
- Tailscale Serve proxies to the localhost listener.

The provider accepts Tailscale-User-Login only when the actual transport peer is loopback and the Host header equals the configured Serve hostname. Hermes runs uvicorn without proxy-header peer rewriting in this mode, so X-Forwarded-For cannot turn a remote caller into a trusted local peer. X-Forwarded-Proto: https is used for Secure-cookie selection only when the actual peer is loopback and explicit trusted-localhost mode is active.

Do not put a Tailscale identity header in Frank, Caddy, a browser request, or a shared secret. Frank only links to the Hermes Serve URL. Hermes remains the owner of dashboard sessions and knowledge secrets.

## Configuration

In the Hermes profile config.yaml:

    dashboard:
      tailscale_auth:
        allowed_users:
          - operator@example.com
        public_host: srv1625369.tail3084c0.ts.net
        session_ttl_seconds: 43200

The provider is inactive when allowed_users or public_host is invalid. Values are matched case-insensitively after strict header validation; there is no wildcard mode. The process-local signing key is regenerated on restart, so dashboard cookies are invalidated on restart and users fall back to the normal login page.

### Configure the allowlist atomically

Use the committed helper after the reviewed Hermes tree is installed. It
requires an explicit Hermes home, runs as the `hermes` user, accepts the
operator login and public hostname as arguments, and updates only the
`dashboard.tailscale_auth` mapping. It refuses missing or malformed config,
symlinks, wrong ownership, and modes other than `0600`; repeated invocation
with the same values is a no-op. Do not use `hermes config set` for
`allowed_users`, because that command accepts a scalar value rather than a
YAML list.

    cd /home/hermes/.hermes
    sudo -n -u hermes -H env -i \
      HOME=/home/hermes \
      HERMES_HOME=/home/hermes/.hermes \
      PATH=/home/hermes/.hermes/hermes-agent/venv/bin:/usr/bin:/bin \
      /home/hermes/.hermes/hermes-agent/venv/bin/python \
      /home/hermes/.hermes/hermes-agent/ops/tailscale/configure_dashboard_auth.py \
      '<TAILSCALE_OPERATOR_LOGIN>' srv1625369.tail3084c0.ts.net

The helper prints only structural status (`allowed_users=1`, hostname
configured, mode `0600`); never place credentials or identity values in Git or
release logs.

## Staged rollout and evidence capture

Do this as a staged release. Do not change hermes-gateway.service; this mode changes only hermes-serve.service.

### 1. Capture the rollback point

Record the exact current Hermes revision/tree, clean status, effective serve unit and drop-ins, and Tailscale Serve configuration before changing anything:

    git -C /home/hermes/.hermes/hermes-agent rev-parse HEAD
    git -C /home/hermes/.hermes/hermes-agent write-tree
    git -C /home/hermes/.hermes/hermes-agent status --short
    systemctl cat hermes-serve.service
    systemctl is-active hermes-serve.service
    sha256sum /etc/systemd/system/hermes-serve.service /etc/systemd/system/hermes-serve.service.d/*.conf
    tailscale serve get-config
    tailscale serve status --json

Store this release evidence outside Git with the change ticket. Never store cookies, API keys, or identity tokens in the evidence.

### 2. Validate the candidate before cutover

Verify the candidate Hermes tree is clean and exactly the reviewed commit/tree. Verify the config has the exact operator login and exact public_host; reject wildcards, schemes, paths, ports, or empty values. Compile and run the focused auth/knowledge suites before installing the candidate.

Install a systemd drop-in that changes only Hermes Serve to --host 127.0.0.1. Keep the existing port and profile. The candidate must start with the auth gate active; ordinary localhost mode must not silently become unauthenticated. The versioned template is ops/tailscale/hermes-serve-localhost.conf.example; install it as a drop-in only after capturing the prior unit and checksum.

### 3. Restart only Hermes Serve

Reload systemd and restart only hermes-serve.service. Leave hermes-gateway.service untouched. Confirm:

- Hermes is listening on localhost only.
- should_require_auth(127.0.0.1, trusted_local_proxy=True) is true.
- A local health request succeeds.
- A password-login canary still reaches the fallback login path.
- A request with a forged/non-loopback transport or missing/unallowlisted identity receives no Hermes session cookie.
- tailscale serve status is still unchanged at this point.

### 4. Enable the HTTPS Serve mapping

Configure persistent Serve only after the local candidate passes:

    tailscale serve --bg 9119

The resulting mapping must be HTTPS and proxy to http://127.0.0.1:9119. Do not use Funnel. Capture the redacted tailscale serve get-config and tailscale serve status --json output and verify the backend target is exactly the localhost port.

### 5. Browser verification

Use a fresh browser session and open the Frank link:

    https://srv1625369.tail3084c0.ts.net/knowledge

The first request should pass through the internal /auth/login redirect, then return to /knowledge with normal Hermes session cookies. The user must not see the Hermes password form. Verify the Memory & Knowledge page can load, and verify that the authenticated knowledge setup API still enforces its Origin, Sec-Fetch-Site, CSRF, and idempotency checks.

Direct access to the old 100.x.x.x:9119 URL must not establish a Tailscale session. An unlisted Tailnet identity and a client-supplied identity header must fail closed.

## Rollback

Rollback in reverse order:

1. Disable or clear the Tailscale Serve mapping first; verify tailscale serve status no longer exposes Hermes.
2. Stop/revert the candidate hermes-serve.service drop-in and restore the captured prior unit/drop-in bytes and checksums.
3. Restore the captured prior Hermes revision/tree using the release lane; never patch the production checkout in place.
4. Reload systemd and restart only hermes-serve.service.
5. Verify the prior local/public endpoint behavior and password fallback.
6. Confirm hermes-gateway.service was not restarted or modified.

If any checksum, revision, tree, unit, or Serve-target check differs from the captured evidence, stop and leave the service fail-closed until the release lane reconciles it.
