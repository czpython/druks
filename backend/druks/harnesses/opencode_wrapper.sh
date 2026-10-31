# One structured opencode call: start `opencode serve`, open a session, POST
# the message with the contract schema, and print the POST response. Inputs
# ride the environment (DRUKS_*, OPENCODE_CONFIG_CONTENT); the prompt rides
# stdin. The message POSTs carry the deadline — an unsatisfiable schema loops
# forever server-side, so on expiry the wrapper aborts the session and exits 124.
# DRUKS_AFTER_AGENT's answer goes to the session as one more message. Each
# response prints on its own line.

run_dir="$DRUKS_RUN_DIR"

write_request() {
  jq -cn --rawfile text "$1" '{
    model: {providerID: env.DRUKS_PROVIDER, modelID: env.DRUKS_MODEL},
    parts: [{type: "text", text: $text}],
    format: {type: "json_schema", schema: env.DRUKS_SCHEMA | fromjson},
  }' > "$run_dir/request.json"
}

mkdir -p "$run_dir" &&
cat > "$run_dir/prompt.txt" &&
write_request "$run_dir/prompt.txt" || exit $?

opencode serve --hostname 127.0.0.1 --port 0 > "$run_dir/opencode.log" 2>&1 &
server_pid=$!
cleanup() { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

# `--port 0` prefers the default port and falls back to an ephemeral one, so
# the listening banner is the only source of the base URL.
base_url=""
attempt=0
while [ "$attempt" -lt 50 ]; do
  base_url=$(sed -n 's/.*listening on \(http:\/\/127\.0\.0\.1:[0-9]*\).*/\1/p' "$run_dir/opencode.log" | head -n 1)
  if [ -n "$base_url" ]; then break; fi
  if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
  sleep 0.1
  attempt=$((attempt + 1))
done
if [ -z "$base_url" ]; then
  cat "$run_dir/opencode.log" >&2
  exit 1
fi

curl --fail --silent --show-error -X POST \
  "$base_url/session?directory=$DRUKS_WORKSPACE_QUERY" \
  -H 'content-type: application/json' --data '{}' > "$run_dir/session.json" || exit $?
session_id=$(jq -r .id "$run_dir/session.json") || exit $?
deadline=$(($(date +%s) + DRUKS_DEADLINE_SECONDS))

send() {
  remaining=$((deadline - $(date +%s)))
  if [ "$remaining" -lt 1 ]; then remaining=1; fi
  curl --fail-with-body --silent --show-error --max-time "$remaining" \
    -X POST "$base_url/session/$session_id/message" \
    -H 'content-type: application/json' \
    --data-binary @"$run_dir/request.json" > "$run_dir/response.json"
  message_status=$?
  if [ "$message_status" -eq 28 ]; then
    curl --fail --silent --show-error -X POST \
      "$base_url/session/$session_id/abort" \
      -H 'content-type: application/json' --data '{}' > "$run_dir/abort.json" || exit 1
    echo 'opencode hit the agent deadline; session aborted' >&2
    exit 124
  fi

  cat "$run_dir/response.json"
  echo
  if [ "$message_status" -ne 0 ]; then exit "$message_status"; fi
  jq -e '.info.error | not' "$run_dir/response.json" > /dev/null || exit $?
}

send
if [ -n "$DRUKS_AFTER_AGENT" ]; then
  sh "$DRUKS_AFTER_AGENT" > "$run_dir/after_agent.txt" || exit $?
  if [ -s "$run_dir/after_agent.txt" ]; then
    write_request "$run_dir/after_agent.txt" || exit $?
    send
  fi
fi
