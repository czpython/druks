#!/usr/bin/env bash
# Restart an already configured Druks checkout via Docker Compose. This shortcut
# does not run setup or migrations; scripts/install.sh is the install and upgrade
# path documented in deploy/README.md.
#
# One image is published from CI (.github/workflows/build-release-images.yml)
# and pinned in compose.yaml via ${DRUKS_TAG:-latest}:
#
#   ghcr.io/czpython/druks:<tag> — Python backend + baked-in SPA build
#
# This path is pull-then-restart. The backend image carries the SPA; Compose
# bind-mounts the repository Caddyfile.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose --env-file .env -f deploy/compose.yaml)

echo "[deploy] pulling images"
"${COMPOSE[@]}" pull

echo "[deploy] (re)starting the stack"
"${COMPOSE[@]}" up -d

echo "[deploy] verifying web health"
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8001/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8001/api/build/work-items/history?limit=1 >/dev/null

echo "[deploy] done"
"${COMPOSE[@]}" ps
