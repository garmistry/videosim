# TLS certificate directory

The production Compose overlay expects:

- `server.crt` / `server.key`: proxy server certificate, with DNS SAN `proxy`
  and the operator-facing hostname.
- `server-ca.crt`: CA workers use to verify the proxy server.
- `worker-ca.crt`: CA Nginx uses to verify worker client certificates.
- `worker-1.crt` / `worker-1.key`: sample worker identity whose certificate CN
  is exactly `worker-1`.

Do not commit certificates, private keys, or CA keys. Use the organization's
certificate manager in production. `scripts/generate-dev-mtls-certs.sh` creates
short-lived local-only certificates for a Compose smoke test.
