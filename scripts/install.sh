#!/usr/bin/env bash
# Druks installer — the only thing you fetch on a fresh box.
#
# Non-interactive and idempotent: re-run any time to pull a fresh
# compose.yaml + new images; re-running is also the upgrade path.
# Deployment configuration lives in druks.toml; ``druks setup`` (run from the
# backend image) creates it with generated secrets and renders .env. When
# everything needed to boot is present the same run migrates the DB (out of
# band, once — never on boot) and brings the stack up; otherwise it prints
# the remaining checklist and exits. GitHub and the coding CLIs connect from
# the dashboard after boot, never here.
#
# Usage:
#
#   curl -fsSL https://druks.ai/install.sh | bash
#
# Env knobs:
#   DRUKS_INSTALL_DIR     default ~/druks
#   DRUKS_REF             default main — tag or full SHA to fetch deploy files from
#   DRUKS_TAG             image tag to pull/run; defaults to the v* DRUKS_REF,
#                         sha-<DRUKS_REF> for a full SHA, or latest for main
#   DRUKS_PROVIDER        default docker — sandbox provider on the first run,
#                         which picks the install shape: `docker` local, `exe`
#                         exe.dev + tailnet, any other name generic remote. Drukbox
#                         validates it. Ignored after druks.toml exists.

set -euo pipefail

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing prerequisite: $1" >&2; exit 1; }; }

fetch_from_repo() {
  local path="$1" out="$2"
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/$path" -o "$out.tmp"
  mv "$out.tmp" "$out"
}

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

main() {
  INSTALL_DIR="${DRUKS_INSTALL_DIR:-$HOME/druks}"
  REPO="czpython/druks"
  REF="${DRUKS_REF:-main}"
  # Validated by ``druks setup`` (the single authority on provider names).
  PROVIDER="${DRUKS_PROVIDER:-docker}"

  if [ -n "${DRUKS_TAG:-}" ]; then
    IMAGE_TAG="$DRUKS_TAG"
  elif [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then
    IMAGE_TAG="sha-$REF"
  elif [[ "$REF" == v* ]]; then
    IMAGE_TAG="$REF"
  else
    IMAGE_TAG="latest"
  fi

  need docker
  need curl
  docker compose version >/dev/null 2>&1 \
    || { echo "missing prerequisite: docker compose plugin" >&2; exit 1; }
  mkdir -p "$INSTALL_DIR"
  cd "$INSTALL_DIR"

  # compose files — the base plus both shape overlays live in the repo, always
  # refresh. COMPOSE_FILE in .env (written below) selects which overlay loads;
  # the unused one sits inert.
  echo "→ fetching compose files from $REPO@$REF"
  fetch_from_repo deploy/compose.yaml compose.yaml
  fetch_from_repo deploy/compose.local.yaml compose.local.yaml
  fetch_from_repo deploy/compose.remote.yaml compose.remote.yaml

  # The Caddyfile is bind-mounted by the remote overlay (stock caddy image, no
  # baked config), so it refreshes on every re-run.
  echo "→ fetching deploy/caddy/Caddyfile from $REPO@$REF"
  mkdir -p caddy
  fetch_from_repo deploy/caddy/Caddyfile caddy/Caddyfile

  BACKEND_IMAGE="ghcr.io/czpython/druks:$IMAGE_TAG"
  echo "→ pulling $BACKEND_IMAGE"
  docker pull -q "$BACKEND_IMAGE" >/dev/null

  # TOML setup + .env render — the required-values brain is ``druks setup``
  # (exit 0 = boot-ready, 3 = gaps).
  set +e
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$INSTALL_DIR:/bootstrap" "$BACKEND_IMAGE" \
    druks setup /bootstrap/.env --provider "$PROVIDER" --home "$HOME"
  setup_rc=$?
  set -e
  case "$setup_rc" in
    0) ;;        # boot-ready — fall through to pull + boot
    3) exit 0 ;; # gaps remain — setup printed the checklist; re-run when done
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

  # Pin the deploy user's uid/gid → the backend containers run as them, not
  # root, so everything written under the mounted data dir stays owned by
  # (and writable to) the deploy user.
  set_env_var DRUKS_UID "$(id -u)"
  set_env_var DRUKS_GID "$(id -g)"
  set_env_var DRUKS_TAG "$IMAGE_TAG"
  if [ "$(uname -s)" = "Darwin" ]; then
    set_env_var DRUKS_WEB_BIND_HOST "0.0.0.0"
  else
    set_env_var DRUKS_WEB_BIND_HOST "127.0.0.1"
  fi

  # COMPOSE_FILE → .env, so plain `docker compose` in this dir loads the right
  # overlay. `local` drives sandboxes on the host Docker daemon (dashboard on
  # :8001, no Caddy); `remote` runs the cloud provider + Caddy. Both shapes
  # mount the Docker socket — sandboxes on local, browser-session containers
  # on remote — so the socket's gid rides along and drukbox's non-root appuser
  # may use it. On macOS the host path is a user-owned symlink, but the socket
  # Docker Desktop mounts into containers is group root, so the host gid would
  # grant nothing.
  if [ "$(uname -s)" = "Darwin" ]; then
    set_env_var DRUKS_DOCKER_GID "0"
  else
    set_env_var DRUKS_DOCKER_GID "$(stat -c '%g' /var/run/docker.sock)"
  fi
  if [ "$PROVIDER" = "docker" ]; then
    set_env_var COMPOSE_FILE "compose.yaml:compose.local.yaml"
  else
    set_env_var COMPOSE_FILE "compose.yaml:compose.remote.yaml"
  fi

  echo "→ docker compose pull"
  docker compose pull

  # The shared sandbox-keys volume: a fresh named volume mounts root-owned, but
  # the backend runs as the deploy user and must write the per-VM SSH keys here.
  # Chown it to the deploy uid:gid (a root-in-container chown, no host sudo).
  # Run it through compose so compose owns the volume — a raw `docker run -v`
  # creates it unlabeled and every later compose call warns. Idempotent; both
  # shapes (web mounts the volume in either).
  echo "→ chown sandbox-keys volume to $(id -u):$(id -g)"
  docker compose run --rm --user root web \
    chown -R "$(id -u):$(id -g)" /app/sandbox-keys

  # Migrations run out of band, once, before the app serves — never on boot.
  # `run --rm` starts the DB deps, applies the alembic schema, seeds, and exits.
  # Idempotent, so it doubles as the upgrade step.
  echo "→ druks init-db (idempotent)"
  docker compose run --rm web druks init-db

  # drukbox keeps its schema in its own database in the shared Postgres; alembic
  # creates the schema but never the database, so create it here — idempotent,
  # covering both a fresh install and an upgrade from the old SQLite volume.
  PG_USER=$(sed -n 's/^DRUKS_POSTGRES_USER=//p' .env)
  PG_USER=${PG_USER:-druks}
  echo "→ ensure drukbox database exists"
  docker compose exec -T postgres \
    psql -U "$PG_USER" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='drukbox'" \
    | grep -q 1 || docker compose exec -T postgres createdb -U "$PG_USER" drukbox

  echo "→ drukbox alembic upgrade (idempotent)"
  docker compose run --rm drukbox .venv/bin/alembic upgrade head

  echo "→ docker compose up -d"
  docker compose up -d

  cat <<MSG

------------------------------------------------------------
Stack is up. Verify with:

  cd $INSTALL_DIR
  docker compose ps
  docker compose exec web druks doctor

Then finish in the dashboard: Settings → Harnesses. Connect
Claude and Codex (agent runs refuse to start on a harness that
isn't connected) and connect the GitHub App druks acts as.
MSG

  if [ "$PROVIDER" = "docker" ]; then
    cat <<MSG

Dashboard: http://127.0.0.1:8001

Sandboxes run as local Docker containers, driven by the drukbox
service in this compose stack.
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
}

# Inside a function invoked on the last line, a truncated download can never
# execute half a script.
main "$@"
