#!/bin/sh
set -eu
# Docker templates build as root without sudo; exe boxes build as a user with sudo.
as_root=""
[ "$(id -u)" -eq 0 ] || as_root="sudo -E"
if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(+(Number(process.versions.node.split(".")[0]) < 22))'; then
    $as_root apt-get update
    $as_root apt-get install -y ca-certificates curl gnupg
    curl -fsSL https://deb.nodesource.com/setup_22.x | $as_root bash -
    $as_root apt-get install -y nodejs
fi
$as_root mkdir -p /opt/druks-chat
$as_root npm install --prefix /opt/druks-chat --omit=dev --no-audit --no-fund \
    @agentclientprotocol/claude-agent-acp@0.79.0 @agentclientprotocol/sdk@1.4.0
