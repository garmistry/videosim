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

Verified worker and OIDC operator subjects longer than 512 characters are
rejected before they can enter durable audit storage.

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
4. Put certificates and an independent Fernet worker-spool key in
   `deploy/certs/` using the names documented there. Keep every private key mode
   `0600` and never commit it.
5. Copy `.env.production.example` to a protected environment file and replace
   every placeholder. Generate the proxy secret with at least 32 random bytes,
   plus distinct URL-safe PostgreSQL owner, app, publisher, pruner, and NATS
   credentials. The three runtime-role passwords must differ from each other
   and from the owner/migration password. Keep the environment file out of
   source control.
6. Restrict VM/container networking so only Nginx reaches app TCP 8080, only
   approved worker networks reach Nginx TCP 9443, and PostgreSQL 5432/NATS 4222
   remain private to approved control-plane services. The single-host Compose
   broker uses credentials on its private bridge; cross-VM broker/database TLS
   remains an F4 requirement.

The F5 worker-domain Compose file drops Linux capabilities, enables
`no-new-privileges`, uses a read-only root filesystem, and mounts only the
domain certificate and spool directories. Its 11 certificate CNs must match
`<failure-domain>-worker-01` through `worker-11`; private keys and spool data
must remain local to that domain host.

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

Structured authorization events are always written to stdout with the
`[videosim-audit]` prefix. In PostgreSQL mode, selective durable audit records
also cover denied proxy/worker/operator authentication or authorization and
successful persistent feed/profile mutations. Successful persistent writes commit
with their immutable audit/outbox event or fail closed with HTTP 503; denial
persistence is best-effort so an audit-store outage leaves the request denied and
emits a stdout audit-gap event. Allowed reads/workers and local runtime actions
remain stdout-only to avoid poll-volume audit traffic.

Migration 004 makes `audit_events` append-only and protects outbox identity and
content while retaining publisher delivery-state updates. Migration 005 preserves
feed generations across deletion to prevent stale configuration ABA updates.
Compose runs the GUI, publisher, and history pruner as separate non-owner roles.
The owner-only role
initializer removes unexpected role memberships, grants, and ownership before
migrations; the post-migration grant service reapplies only each service's
required rights. This is tamper resistance against routine runtime credentials,
not a WORM archive or protection against a PostgreSQL owner/superuser.

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
--report-spool-dir
--report-spool-key-file
--report-spool-max-bytes
```

Transient connection failures and HTTP 429/500/502/503/504 responses use bounded
exponential jittered retries. HTTP 409 is not transport-retried; it invokes the
assignment refetch/fencing path. On PostgreSQL, worker v2 binds the verified
worker ID to a process-incarnation UUID and durable offered/acknowledged leases;
a different incarnation is rejected while the current one is heartbeat-fresh,
and a post-expiry replacement revokes prior leases. Certificates and
keys must still be rotated by the VM secret/certificate manager. The production
worker writes report ciphertext to a persistent volume before delivery and
requires its Fernet key from the read-only certificate/secret mount. Do not
rotate that key until the spool is empty; automatic multi-key rotation and
corrupted-entry repair are not implemented.

## Known boundaries

This slice authenticates the deployed proxy boundary but does not yet provide:

- Durable tenant records or resource-level multi-tenant authorization.
- An external/WORM audit archive, audit retention policy, and durable audit
  coverage for allowed reads/workers/local runtime actions. Denial persistence is
  intentionally best-effort when the database/outbox is unavailable.
- Managed worker certificate revocation/rotation evidence and multi-node lease
  failover (durable incarnations/leases exist in PostgreSQL worker v2).
- Automated certificate issuance/rotation.
- OIDC-provider integration tests against a real organization tenant.
- A network firewall implementation; deployment operators must enforce egress.
