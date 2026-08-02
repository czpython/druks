#!/usr/bin/env bash
# Druks installer — the only thing you fetch on a fresh box.
#
# Idempotent: re-run any time to pull a fresh compose.yaml + new images.
# Deployment configuration lives in druks.toml; ``druks setup`` (run from the
# backend image) creates it, prompts for required values, and renders .env.
# When everything needed to boot is present the same run migrates the DB (out
# of band, once — never on boot) and brings the stack up; otherwise it prints
# the remaining checklist and exits. Claude/Codex subscription auth is not a
# prerequisite: connect them from the dashboard after boot.
#
# Usage:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/main/scripts/install.sh)
#
# Env knobs:
#   DRUKS_INSTALL_DIR     default ~/druks
#   DRUKS_REF             default main — tag or full SHA to fetch deploy files from
#   DRUKS_TAG             image tag to pull/run; defaults to the v* DRUKS_REF,
#                         sha-<DRUKS_REF> for a full SHA, or latest for main
#   DRUKS_PROVIDER        default exe — sandbox provider on the first run, which
#                         picks the install shape: `docker` local, `exe` exe.dev
#                         + tailnet, any other name generic remote. Drukbox
#                         validates it. Ignored after druks.toml exists.
#
# Flags:
#   --apps                provision the operator + reviewer GitHub Apps via
#                         the manifest flow and exit. Run between
#                         the first run (druks.toml written) and the boot run.

set -euo pipefail

INSTALL_DIR="${DRUKS_INSTALL_DIR:-$HOME/druks}"
REPO="czpython/druks"
REF="${DRUKS_REF:-main}"
# Validated by ``druks setup`` (the single authority on provider names).
PROVIDER="${DRUKS_PROVIDER:-exe}"

if [ -n "${DRUKS_TAG:-}" ]; then
  IMAGE_TAG="$DRUKS_TAG"
elif [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  IMAGE_TAG="sha-$REF"
elif [[ "$REF" == v* ]]; then
  IMAGE_TAG="$REF"
else
  IMAGE_TAG="latest"
fi

# ---------------------------------------------------------------------------
# Prereqs
# ---------------------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing prerequisite: $1" >&2; exit 1; }; }
need docker
need curl
docker compose version >/dev/null 2>&1 \
  || { echo "missing prerequisite: docker compose plugin" >&2; exit 1; }
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 1. compose.yaml — single source of truth lives in the repo, always refresh
# ---------------------------------------------------------------------------
fetch_from_repo() {
  local path="$1" out="$2"
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/$path" -o "$out.tmp"
  mv "$out.tmp" "$out"
}

echo "→ fetching deploy/compose.yaml from $REPO@$REF"
fetch_from_repo deploy/compose.yaml compose.yaml

# The Caddyfile is bind-mounted by compose (stock caddy image, no baked
# config), so it refreshes together with compose.yaml on every re-run.
echo "→ fetching deploy/caddy/Caddyfile from $REPO@$REF"
mkdir -p caddy
fetch_from_repo deploy/caddy/Caddyfile caddy/Caddyfile

# ---------------------------------------------------------------------------
# 2. backend image — ``druks setup`` runs from it
# ---------------------------------------------------------------------------
BACKEND_IMAGE="ghcr.io/czpython/druks:$IMAGE_TAG"
echo "→ pulling $BACKEND_IMAGE"
docker pull -q "$BACKEND_IMAGE" >/dev/null

# ---------------------------------------------------------------------------
# --apps — provision the GitHub Apps through the single TOML writer
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--apps" ]; then
  [ -f druks.toml ] || {
    echo "no druks.toml yet — run the installer once first" >&2
    exit 1
  }
  need python3
  python3 -c 'import tomllib' >/dev/null 2>&1 || {
    echo "GitHub App setup requires Python 3.11+ (tomllib)." >&2
    exit 1
  }
  echo "→ fetching app-setup script + manifests from $REPO@$REF"
  mkdir -p manifests
  fetch_from_repo scripts/_github_app_setup.py _github_app_setup.py
  fetch_from_repo scripts/manifests/operator.json manifests/operator.json
  fetch_from_repo scripts/manifests/reviewer.json manifests/reviewer.json
  DRUKS_BACKEND_IMAGE="$BACKEND_IMAGE" exec python3 _github_app_setup.py
fi

# ---------------------------------------------------------------------------
# 3. TOML setup + .env render — the required-values brain is ``druks setup``
#    (exit 0 = boot-ready, 3 = gaps, 4 = pre-TOML hand migration required)
# ---------------------------------------------------------------------------
TTY_FLAGS=(-i)
SETUP_ARGS=(--provider "$PROVIDER" --install-dir "$INSTALL_DIR" --home "$HOME")
if [ -t 0 ]; then
  TTY_FLAGS=(-it)
else
  SETUP_ARGS+=(--non-interactive)
fi
set +e
docker run --rm "${TTY_FLAGS[@]}" --user "$(id -u):$(id -g)" \
  -v "$INSTALL_DIR:/bootstrap" "$BACKEND_IMAGE" \
  druks setup /bootstrap/.env "${SETUP_ARGS[@]}"
setup_rc=$?
set -e
case "$setup_rc" in
  0) ;;        # boot-ready — fall through to pull + boot
  3) exit 0 ;; # gaps remain — setup printed the checklist; re-run when done
  4) exit 4 ;; # pre-TOML .env — setup printed the hand-migration recipe
  *) echo "druks setup failed (exit $setup_rc)" >&2; exit "$setup_rc" ;;
esac

# setup rendered the provider from druks.toml — read the artifact so the
# shape branches below follow the authored configuration.
PROVIDER=$(sed -n 's/^DEFAULT_HOST_PROVIDER=//p' .env)
DATA_HOST_DIR=$(sed -n 's/^DRUKS_DATA_HOST_DIR=//p' .env)
if [ -z "$DATA_HOST_DIR" ]; then
  DATA_HOST_DIR=$(sed -n 's/^DRUKS_DATA_DIR=//p' .env)
fi
mkdir -p "$DATA_HOST_DIR"

# ---------------------------------------------------------------------------
# 3b. Pin the deploy user's uid/gid → the backend containers run as them, not
#     root, so everything written under the mounted data dir stays owned by
#     (and writable to) the deploy user.
# ---------------------------------------------------------------------------
set_env_var() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed "s|^${key}=.*|${key}=${value}|" .env > .env.tmp
    mv .env.tmp .env
    chmod 600 .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
set_env_var DRUKS_UID "$(id -u)"
set_env_var DRUKS_GID "$(id -g)"
set_env_var DRUKS_TAG "$IMAGE_TAG"
if [ "$(uname -s)" = "Darwin" ]; then
  set_env_var DRUKS_WEB_BIND_HOST "0.0.0.0"
else
  set_env_var DRUKS_WEB_BIND_HOST "127.0.0.1"
fi

# Compose profile → .env, so plain `docker compose` in this dir picks it up.
# `remote` brings up the drukbox control-plane + Caddy; local runs neither
# (drukbox lives on the host, dashboard is reached directly on :8001).
if [ "$PROVIDER" = "docker" ]; then
  set_env_var COMPOSE_PROFILES ""
else
  set_env_var COMPOSE_PROFILES "remote"
fi

# ---------------------------------------------------------------------------
# 4. Pull + migrate + boot
# ---------------------------------------------------------------------------
echo "→ docker compose pull"
docker compose pull

# The shared sandbox-keys volume: a fresh named volume mounts root-owned, but
# the backend runs as the deploy user and must write the per-VM SSH keys here.
# Chown it to the deploy uid:gid (a root-in-container chown, no host sudo).
# Idempotent; both shapes (web writes keys in either).
echo "→ chown sandbox-keys volume to $(id -u):$(id -g)"
docker run --rm -v "$(basename "$INSTALL_DIR")_druks_sandbox_keys:/keys" alpine \
  chown -R "$(id -u):$(id -g)" /keys

# drukbox runs as its non-root appuser (uid 1001); a fresh named volume mounts
# root-owned, so appuser can't create the SQLite DB. Chown it once, host-side.
# Remote only — a local install has no drukbox container (it's on the host).
if [ "$PROVIDER" != "docker" ]; then
  echo "→ chown drukbox SQLite volume to appuser (1001:999)"
  docker run --rm -v "$(basename "$INSTALL_DIR")_drukbox_data:/data" alpine \
    chown -R 1001:999 /data
fi

# Migrations run out of band, once, before the app serves — never on boot.
# `run --rm` starts the DB deps, applies the alembic schema, seeds, and exits.
# Idempotent, so it doubles as the upgrade step.
echo "→ druks init-db (idempotent)"
docker compose run --rm web druks init-db
if [ "$PROVIDER" != "docker" ]; then
  echo "→ drukbox alembic upgrade (idempotent)"
  docker compose run --rm sandbox-service .venv/bin/alembic upgrade head
fi

echo "→ docker compose up -d"
docker compose up -d

# ---------------------------------------------------------------------------
# 5. Done — surface the next steps
# ---------------------------------------------------------------------------
cat <<MSG

------------------------------------------------------------
Stack is up. Verify with:

  cd $INSTALL_DIR
  docker compose ps
  docker compose exec web druks doctor

Then connect the coding CLIs from the dashboard — Settings →
Harnesses → Connect for each of Claude and Codex. Agent runs
refuse to start on a harness that isn't connected.
MSG

if [ "$PROVIDER" = "docker" ]; then
  cat <<MSG

Dashboard: http://127.0.0.1:8001

Sandboxes run as local Docker containers — start drukbox on the host:
  git clone https://github.com/czpython/drukbox
  cd drukbox && DOCKER_SSH_USERNAME=druks make dev
------------------------------------------------------------
MSG
elif [ "$PROVIDER" = "exe" ]; then
  cat <<MSG

Public URLs (once exe.dev port-share is configured):
  https://<your-host>/webhooks/{github,linear}
  https://<your-host>/
------------------------------------------------------------
MSG
else
  cat <<MSG

Remote shape ($PROVIDER): configure public ingress and identity per:
  https://github.com/czpython/druks/tree/main/deploy
------------------------------------------------------------
MSG
fi
