#!/usr/bin/env bash
# Seeds the non-root ``druks`` user, then the drukbox base entrypoint does the
# boot. Claude Code refuses ``bypassPermissions`` under uid 0, so agents must
# SSH in unprivileged.
set -euo pipefail

: "${DRUKBOX_AUTHORIZED_KEY:?DRUKBOX_AUTHORIZED_KEY is required}"

install -d -m 700 -o druks -g druks /home/druks/.ssh
printf '%s\n' "$DRUKBOX_AUTHORIZED_KEY" > /home/druks/.ssh/authorized_keys
chmod 600 /home/druks/.ssh/authorized_keys
chown druks:druks /home/druks/.ssh/authorized_keys

exec /usr/local/bin/drukbox-entrypoint
