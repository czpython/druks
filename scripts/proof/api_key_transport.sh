#!/usr/bin/env bash
# Runs Codex, Pi, and OpenCode with a placeholder API key through a local
# mitmproxy that answers every provider request itself. Needs uvx and the three
# CLIs on PATH. Uses no provider credential and an empty home.
#
#   scripts/proof/api_key_transport.sh [output directory]
#
# The output directory gets the proxy log, each run's stdout and stderr, and
# requests.jsonl. The summary at the end lists the provider requests per run.
set -euo pipefail

OUT=${1:-$(mktemp -d)}
PORT=${PORT:-8880}
ADDON="$(cd "$(dirname "$0")" && pwd)/api_key_transport_addon.py"
HOME_DIR="$OUT/home"
CA="$OUT/conf/mitmproxy-ca-cert.pem"
export PROOF_LOG="$OUT/requests.jsonl"

mkdir -p "$HOME_DIR/work" "$HOME_DIR/.codex"
: > "$PROOF_LOG"
uvx --from mitmproxy mitmdump --listen-host 127.0.0.1 --listen-port "$PORT" \
    --set confdir="$OUT/conf" --set connection_strategy=lazy --set upstream_cert=false \
    -q -s "$ADDON" > "$OUT/mitmdump.log" 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID' EXIT
until [ -f "$CA" ]; do sleep 1; done
sleep 2

run() {
    local name=$1
    shift
    echo "{\"run\": \"$name\"}" >> "$PROOF_LOG"
    # The prompt rides stdin, as Druks sends it. Every CLI waits on stdin otherwise.
    (
        cd "$HOME_DIR/work" && printf 'say hi' | env -i PATH="$PATH" HOME="$HOME_DIR" TERM=dumb \
            HTTPS_PROXY="http://127.0.0.1:$PORT" https_proxy="http://127.0.0.1:$PORT" \
            NO_PROXY="localhost,127.0.0.1,::1,169.254.169.254" \
            SSL_CERT_FILE="$CA" NODE_EXTRA_CA_CERTS="$CA" \
            "$@" > "$OUT/$name.out" 2> "$OUT/$name.err"
    ) || true
}

PI="pi -p --mode json --no-session --no-extensions --no-skills --offline"
run pi-anthropic env ANTHROPIC_API_KEY=drk.proof.anthropic.KEY $PI --provider anthropic --model claude-sonnet-4-5
run pi-openai env OPENAI_API_KEY=drk.proof.openai.KEY $PI --provider openai --model gpt-5.5
run opencode-anthropic env ANTHROPIC_API_KEY=drk.proof.anthropic.KEY opencode run --model anthropic/claude-sonnet-4-5 "say hi"
run opencode-openai env OPENAI_API_KEY=drk.proof.openai.KEY opencode run --model openai/gpt-5.5 "say hi"
run codex env CODEX_HOME="$HOME_DIR/.codex" CODEX_API_KEY=drk.proof.openai.KEY codex exec --skip-git-repo-check

python3 - "$PROOF_LOG" <<'PY'
import json
import sys

run = None
for line in open(sys.argv[1]):
    entry = json.loads(line)
    if "run" in entry:
        run = entry["run"]
        print(f"\n## {run}")
    elif entry["kind"] == "request" and entry["host"] in ("api.anthropic.com", "api.openai.com"):
        print(f"  {entry['method']} {entry['host']}{entry['path']} {entry['headers']}")
PY
echo
echo "Details in $OUT"
