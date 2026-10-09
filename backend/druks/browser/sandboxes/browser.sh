#!/bin/sh
# Drukbox builds this as root on the stock image of its docker provider.
set -eu
export DEBIAN_FRONTEND=noninteractive
arch="$(dpkg --print-architecture)"
pinchtab_version=0.15.1
playwright_version=1.62.0

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
# Real Google Chrome, so the login browser presents Chrome's fingerprint and not
# Chromium's. A login gate reads the difference.
curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
apt-get update
apt-get install -y --no-install-recommends \
    fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
    google-chrome-stable nodejs tzdata x11vnc xvfb
rm -rf /var/lib/apt/lists/*

install -d -m 0755 /opt/druks-browser
npm install --prefix /opt/druks-browser --no-package-lock --omit=dev "playwright@$playwright_version"

asset="pinchtab-linux-$arch"
release="https://github.com/pinchtab/pinchtab/releases/download/v$pinchtab_version"
curl -fsSLo /tmp/pinchtab "$release/$asset"
curl -fsSLo /tmp/pinchtab-checksums.txt "$release/checksums.txt"
grep "  $asset$" /tmp/pinchtab-checksums.txt \
    | sed 's#  .*#  /tmp/pinchtab#' \
    | sha256sum --check --strict -
install -m 0755 /tmp/pinchtab /usr/local/bin/pinchtab
rm -f /tmp/pinchtab /tmp/pinchtab-checksums.txt

# The browser runs unsandboxed, so Chrome shows the operator a "stability and
# security will suffer" banner. This policy turns off only that warning.
install -d -m 0755 /etc/opt/chrome/policies/managed
echo '{"CommandLineFlagSecurityWarningsEnabled": false}' \
    > /etc/opt/chrome/policies/managed/druks.json

useradd --create-home --shell /bin/bash druks
install -d -m 0700 -o druks -g druks /work/session
# Xvfb runs as druks and cannot make this directory sticky itself.
install -d -m 1777 /tmp/.X11-unix
