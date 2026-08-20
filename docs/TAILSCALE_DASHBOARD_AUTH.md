# Tailscale Serve dashboard authentication

Hermes can create its normal dashboard session from a Tailscale Serve identity.
This is an operator convenience, not a replacement for the Hermes session
cookie: Tailscale authenticates the request, then Hermes mints its own
HttpOnly/Secure/SameSite session cookies.

## Trust boundary

The dashboard service MUST listen on `127.0.0.1` (or `::1`) when this mode is
enabled. Tailscale Serve proxies the HTTPS `*.ts.net` URL to that local
listener and strips client-supplied `Tailscale-*` identity headers before
injecting the authenticated identity. A non-loopback Hermes listener must not
trust those headers; the provider rejects every non-loopback peer.

Do not put a Tailscale identity header in Frank, Caddy, a browser request, or a
shared secret. Frank only links to the Hermes Serve URL. Hermes remains the
owner of dashboard sessions and knowledge secrets.

## Configuration

In the Hermes profile `config.yaml`:

```yaml
dashboard:
  tailscale_auth:
    allowed_users:
      - operator@example.com
    session_ttl_seconds: 43200
```

The provider is inactive when `allowed_users` is empty. Values are matched
case-insensitively after strict header validation; there is no wildcard mode.
The process-local signing key is regenerated on restart, so dashboard cookies
are invalidated on restart and users fall back to the normal login page.

## Rollout

1. Review the exact configured Tailscale login name and add it to the profile
   allowlist. Never copy it from an untrusted browser header.
2. Install a systemd drop-in that changes Hermes Serve to `--host 127.0.0.1`.
3. Restart Hermes Serve and verify the local health check.
4. Configure persistent Tailscale Serve for the local port, for example:

   ```text
   tailscale serve --bg 9119
   ```

   The command must report an HTTPS `*.ts.net` URL and proxy to
   `http://127.0.0.1:9119`. Do not use Funnel.
5. Open the HTTPS Serve URL. The first request should redirect through
   `/auth/login?provider=tailscale` and land at `/knowledge` with a normal
   Hermes session cookie. A direct request to the old `100.x.x.x:9119` URL must
   not establish a Tailscale session.
6. Keep the existing password provider enabled as the recovery path.

The rollout is fail-closed if Serve is absent, the identity is not on the
allowlist, the peer is not loopback, or the header is malformed. To disable
the convenience login, clear `dashboard.tailscale_auth.allowed_users`; the
ordinary Hermes password/OAuth login remains available.

## Verification checklist

- `tailscale serve status` shows only the intended HTTPS Serve mapping.
- Hermes listens on localhost, not the Tailscale interface.
- The served URL is HTTPS and uses a top-level browser navigation.
- A forged `Tailscale-User-Login` header on a direct/non-loopback request is
  rejected.
- An unlisted or malformed identity falls back to `/login` without setting a
  session cookie.
- A listed identity receives the usual protected Hermes session cookies and a
  safe same-origin landing path only.
- Frank never stores, forwards, or renders the Hermes session cookie or any
  Tailscale identity header.
