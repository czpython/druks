#!/usr/bin/env bash
# First-boot entrypoint. Mirrors the drukbox base entrypoint but seeds the
# non-root ``druks`` user instead of root — Claude Code refuses
# ``bypassPermissions`` under uid 0, so agents must SSH in unprivileged.
set -euo pipefail

: "${DRUKBOX_AUTHORIZED_KEY:?DRUKBOX_AUTHORIZED_KEY is required}"

install -d -m 700 -o druks -g druks /home/druks/.ssh
printf '%s\n' "$DRUKBOX_AUTHORIZED_KEY" > /home/druks/.ssh/authorized_keys
chmod 600 /home/druks/.ssh/authorized_keys
chown druks:druks /home/druks/.ssh/authorized_keys

# Persist caller-supplied env for SSH sessions: pam_env reads /etc/environment.
for name in ${DRUKBOX_ENV_KEYS:-}; do
  printf '%s=%s\n' "$name" "${!name-}" >> /etc/environment
done

# A box with secrets gets the proxy's CA in SECRETS_PROXY_CA. Install it first.
# HTTPS_PROXY is already in the session env; without this file Claude (Node)
# rejects the proxy's cert and dies with SSL certificate verification failed.
if [ -n "${SECRETS_PROXY_CA:-}" ]; then
  printf '%s' "$SECRETS_PROXY_CA" | base64 -d > /usr/local/share/ca-certificates/drukbox.crt
  update-ca-certificates >/dev/null
fi

# git takes its credential from gh, and SSH remotes go over HTTPS.
if [ -n "${GH_TOKEN:-}" ]; then
  git config --system --replace-all credential.https://github.com.helper ''
  git config --system --add credential.https://github.com.helper '!gh auth git-credential'
  git config --system --replace-all url.https://github.com/.insteadOf git@github.com:
  git config --system --add url.https://github.com/.insteadOf ssh://git@github.com/
fi

ssh-keygen -A

exec /usr/sbin/sshd -D -e
