# Security and Identity

## Production boundary for Docker Compose / VMs

The production deployment uses two identity planes:

- **Workers:** TLS client certificates. Nginx verifies the certificate against
  `worker-ca.crt`, extracts the certificate common name, clears inbound identity
  headers, and injects `X-VideoSim-Worker-ID` plus a proxy-only shared secret.
  The application requires that identity to exactly match `worker_id`/`workerId`.
- **Operators:** oauth2-proxy performs OIDC login. Nginx copies the authenticated
  user and groups into trusted headers. The application permits reads to
  `videosim-viewer` or `videosim-admin` and mutations only to `videosim-admin`.
  Admin mutations also require the browser `Origin` to exactly match a trusted
  expected origin injected by Nginx; oauth2-proxy cookies are Secure and
  SameSite=Lax with per-request login CSRF cookies.

The app's plain HTTP port is internal-only in `docker-compose.production.yml`.
Identity headers are trusted only when `VIDEOSIM_SECURITY_MODE=trusted-proxy`
and the request also carries a constant-time validated, 32+ character
`VIDEOSIM_PROXY_SHARED_SECRET`. Do not publish the app port or reuse that secret
outside the proxy-to-app hop.

## Production Compose prerequisites

1. Register oauth2-proxy as an OIDC client. Ensure the provider emits a groups
   claim compatible with `X-Auth-Request-Groups`.
2. Provision a server certificate valid for the proxy's internal name and
   operator hostname.
3. Provision a worker client CA and one certificate per worker. Each certificate
   CN must exactly equal the worker's `--worker-id`.
4. Put certificates in `deploy/certs/` using the names documented there. Never
   commit private keys.
5. Copy `.env.production.example` to a protected environment file and replace
   every placeholder. Generate the proxy secret with at least 32 random bytes.
6. Restrict VM/container networking so only Nginx reaches app TCP 8080 and only
   approved worker networks reach Nginx TCP 9443.

Validate configuration:

```sh
set -a
. ./.env.production
set +a
docker compose -f docker-compose.production.yml config --quiet
```

Start:

```sh
docker compose --env-file .env.production \
  -f docker-compose.production.yml up --build -d
```

Operator UI defaults to `https://localhost:8443`. Worker mTLS defaults to
`https://localhost:9443`. The app is launched with an explicit
`--worker-base-url https://proxy:9443`, so generated DASH assignments never infer
or downgrade their origin from a caller-controlled Host header.

For a local-only smoke certificate set:

```sh
scripts/generate-dev-mtls-certs.sh
```

The generated CA keys are temporary and intentionally discarded. These
certificates are not suitable for production.

## Application security mode

Relevant environment variables:

| Variable | Meaning |
|---|---|
| `VIDEOSIM_SECURITY_MODE` | `off` or `trusted-proxy` |
| `VIDEOSIM_PROXY_SHARED_SECRET` | Required 32+ character proxy-to-app secret |
| `VIDEOSIM_VIEWER_GROUP` | OIDC group allowed to read; default `videosim-viewer` |
| `VIDEOSIM_ADMIN_GROUP` | OIDC group allowed to mutate; default `videosim-admin` |
| `VIDEOSIM_ALLOW_PRIVATE_FEEDS` | Explicitly allow private destinations when `1`; loopback/link-local/multicast/reserved/unspecified remain forbidden |
| `VIDEOSIM_ALLOWED_FEED_HOST_SUFFIXES` | Comma-separated explicit hostname suffix policy |

`/healthz` and `/readyz` are public for local orchestration health checks.
Generated DASH data requires the trusted proxy secret but no operator session.
All GUI/static/state/diagnostic reads require an OIDC viewer/admin identity, and
all non-worker POST actions require an admin identity plus a same-origin CSRF
check. Viewer feed-detail and root requests use request-local selection views
and do not mutate shared GUI state.

Structured authorization events are written to stdout with the
`[videosim-audit]` prefix. Durable tamper-resistant audit storage remains part of
the durable-control-plane gate.

## Input and egress controls

- App and Nginx reject request bodies over 1 MiB.
- Worker report arrays and identifiers have explicit hard limits.
- Trusted-proxy mode rejects inline URL credentials and, by default, external
  endpoints resolving to private, loopback, link-local, multicast, reserved, or
  unspecified addresses.
- Private media networks require an explicit suffix or private-feed policy plus
  VM firewall/egress rules. DNS policy in the app is defense in depth, not a
  substitute for network enforcement.
- Nginx rate-limits operator and worker paths independently.

## Worker TLS and retries

Workers accept:

```text
--tls-ca-file
--tls-cert-file
--tls-key-file
--retry-attempts
--retry-base-seconds
```

Transient connection failures and HTTP 429/500/502/503/504 responses use bounded
exponential jittered retries. HTTP 409 is not transport-retried; it invokes the
assignment refetch/fencing path. Certificates and keys must be rotated by the
VM secret/certificate manager; a durable worker incarnation and revocation
registry remain future gates.

## Known boundaries

This slice authenticates the deployed proxy boundary but does not yet provide:

- Durable tenant records or resource-level multi-tenant authorization.
- Database-backed immutable audit history.
- Durable worker incarnations, leases, or revocation state.
- Automated certificate issuance/rotation.
- OIDC-provider integration tests against a real organization tenant.
- A network firewall implementation; deployment operators must enforce egress.
