#!/usr/bin/env bash
#
# Mint a TLS certificate for the SUSE VM, signed by the existing local
# "Kassim Local CA" — so devices that already trust that CA need no changes.
#
# Run this on the machine that HOLDS the CA private key, i.e. the box where
# server/rootCA.key + server/rootCA.pem live (your Windows dev machine, in
# Git Bash). It never needs the CA key to leave that machine.
#
#   bash deploy/make-vm-cert.sh <VM_IP> [hostname]
#   e.g.  bash deploy/make-vm-cert.sh 10.3.2.50 zstm
#
# It writes server/vm-cert.pem and server/vm-key.pem. Copy those to the VM as
# /opt/zstm/server/cert.pem and /opt/zstm/server/key.pem (see DEPLOY_SUSE.md).
#
set -euo pipefail

IP="${1:?usage: make-vm-cert.sh <VM_IP> [hostname]}"
HOST="${2:-zstm}"
DIR="$(cd "$(dirname "$0")/../server" && pwd)"
export MSYS_NO_PATHCONV=1   # keep openssl -subj from being mangled on Git Bash
cd "$DIR"

[ -f rootCA.pem ] && [ -f rootCA.key ] || {
  echo "ERROR: rootCA.pem / rootCA.key not found in $DIR" >&2
  echo "Run this on the machine that generated the local CA." >&2
  exit 1
}

openssl genrsa -out vm-key.pem 2048
openssl req -new -key vm-key.pem -subj "/CN=$HOST" -out vm.csr
printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:%s,DNS:%s,DNS:localhost,IP:127.0.0.1\n' "$IP" "$HOST" > vm.ext
openssl x509 -req -in vm.csr -CA rootCA.pem -CAkey rootCA.key -CAcreateserial \
  -days 800 -sha256 -extfile vm.ext -out vm-cert.pem
rm -f vm.csr vm.ext

echo
echo "Created:"
echo "  $DIR/vm-cert.pem  ->  copy to VM as server/cert.pem"
echo "  $DIR/vm-key.pem   ->  copy to VM as server/key.pem   (keep private, mode 600)"
echo "SAN: IP:$IP DNS:$HOST"
openssl verify -CAfile rootCA.pem vm-cert.pem
