#!/usr/bin/env bash
# Install the stack that the E2E lane drives: scripts/install.sh on the docker provider,
# with a test CA and the E2E Compose override, then the identity and sign-in settings.
#
# It takes the env knobs of scripts/install.sh. Its arguments go to `druks setup`, for
# example --set sandbox.image=druks-sandbox:e2e.
set -euo pipefail

e2e="$(cd "$(dirname "$0")" && pwd)"
install_dir="${DRUKS_INSTALL_DIR:-$HOME/druks}"

# The mock serves api.anthropic.com with a certificate from this CA. Each extension is
# explicit, because OpenSSL and the LibreSSL of macOS add different ones by default.
mkdir -p "$install_dir/e2e"
cd "$install_dir/e2e"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Druks E2E CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" \
  -addext "subjectKeyIdentifier=hash" -keyout ca.key -out ca.pem
openssl req -newkey rsa:2048 -nodes -subj "/CN=api.anthropic.com" \
  -keyout api.anthropic.com.key -out api.anthropic.com.csr
openssl x509 -req -in api.anthropic.com.csr -days 2 -CA ca.pem -CAkey ca.key -CAcreateserial \
  -extfile <(printf 'subjectAltName=DNS:api.anthropic.com\nbasicConstraints=CA:FALSE\nauthorityKeyIdentifier=keyid\n') \
  -out api.anthropic.com.pem
echo "ssl_verify_upstream_trusted_ca: /e2e/ca.pem" > mitmproxy.yaml

# The installer keeps an override that exists.
cp "$e2e/compose.override.yaml" "$install_dir/compose.override.yaml"
DRUKS_PROVIDER=docker bash "$e2e/../../scripts/install.sh"

cd "$install_dir"
docker run --rm --user "$(id -u):$(id -g)" -v "$install_dir:/bootstrap" \
  "ghcr.io/czpython/druks:$(sed -n 's/^DRUKS_TAG=//p' .env)" \
  druks setup /bootstrap/.env --home "$HOME" \
  --set identity.mode=header --set identity.header=X-Forwarded-Email \
  --set providers.anthropic.sign_in=setup_token "$@"
docker compose restart web
# drukbox pulls the sandbox image inside the request that creates a template, and a first
# pull can take longer than that request may.
image="$(sed -n 's/^image = "\(.*\)"$/\1/p' druks.toml)"
docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
docker compose exec -T web druks sandboxes build druks.chat.bridge:sandboxes/chat.sh
