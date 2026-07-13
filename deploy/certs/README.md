# TLS certificate directory

The production Compose overlay expects:

- `server.crt` / `server.key`: proxy server certificate, with DNS SAN `proxy`
  and the operator-facing hostname.
- `server-ca.crt`: CA workers use to verify the proxy server.
- `worker-ca.crt`: CA Nginx uses to verify worker client certificates.
- `worker-1.crt` / `worker-1.key`: sample worker identity whose certificate CN
  is exactly `worker-1`.
- `worker-spool.key`: URL-safe base64 Fernet key for the encrypted local report
  spool, readable only by the worker account.

`docker-compose.worker-domain.yml` expects 11 certificate/key pairs named for
the configured domain, for example `candidate-zone-a-worker-01.crt` and
`candidate-zone-a-worker-01.key` through `worker-11`. Every certificate CN must
exactly match its filename stem and worker ID. Provision a separate set on each
failure-domain host; do not copy worker private keys between domains.

Do not commit certificates, private keys, or CA keys. Use the organization's
certificate manager in production. `scripts/generate-dev-mtls-certs.sh` creates
short-lived local-only certificates for a Compose smoke test.
