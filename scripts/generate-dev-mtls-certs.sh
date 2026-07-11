#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-deploy/certs}"
worker_id="${WORKER_ID:-worker-1}"
mkdir -p "$output_dir"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

openssl req -x509 -newkey rsa:3072 -nodes -days 30 \
  -subj "/CN=VideoSim development server CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -keyout "$work_dir/server-ca.key" -out "$output_dir/server-ca.crt" >/dev/null 2>&1
cat >"$work_dir/server.ext" <<'EOF'
subjectAltName=DNS:proxy,DNS:localhost,IP:127.0.0.1
extendedKeyUsage=serverAuth
keyUsage=digitalSignature,keyEncipherment
EOF
openssl req -newkey rsa:3072 -nodes -subj "/CN=proxy" \
  -keyout "$output_dir/server.key" -out "$work_dir/server.csr" >/dev/null 2>&1
openssl x509 -req -days 30 -sha256 -in "$work_dir/server.csr" \
  -CA "$output_dir/server-ca.crt" -CAkey "$work_dir/server-ca.key" -CAcreateserial \
  -extfile "$work_dir/server.ext" -out "$output_dir/server.crt" >/dev/null 2>&1

openssl req -x509 -newkey rsa:3072 -nodes -days 30 \
  -subj "/CN=VideoSim development worker CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -keyout "$work_dir/worker-ca.key" -out "$output_dir/worker-ca.crt" >/dev/null 2>&1
cat >"$work_dir/worker.ext" <<'EOF'
extendedKeyUsage=clientAuth
keyUsage=digitalSignature
EOF
openssl req -newkey rsa:3072 -nodes -subj "/CN=${worker_id}" \
  -keyout "$output_dir/${worker_id}.key" -out "$work_dir/worker.csr" >/dev/null 2>&1
openssl x509 -req -days 30 -sha256 -in "$work_dir/worker.csr" \
  -CA "$output_dir/worker-ca.crt" -CAkey "$work_dir/worker-ca.key" -CAcreateserial \
  -extfile "$work_dir/worker.ext" -out "$output_dir/${worker_id}.crt" >/dev/null 2>&1

chmod 600 "$output_dir/server.key" "$output_dir/${worker_id}.key"
echo "Generated local-only TLS material in $output_dir for worker CN ${worker_id}"
