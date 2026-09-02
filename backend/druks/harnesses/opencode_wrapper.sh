# One structured opencode call: start `opencode serve`, open a session, POST
# the message with the contract schema, and print the POST response. Inputs
# ride the environment (DRUKS_*, OPENCODE_AUTH_CONTENT,
# OPENCODE_CONFIG_CONTENT); the prompt rides stdin. The message POST carries
# the deadline — an unsatisfiable schema loops forever server-side, so on
# expiry the wrapper aborts the session and exits 124.

run_dir="$DRUKS_RUN_DIR"
mkdir -p "$run_dir" &&
cat > "$run_dir/prompt.txt" &&
node -e '
  const fs = require("fs");
  const [promptPath, requestPath] = process.argv.slice(1);
  const request = {
    model: {providerID: process.env.DRUKS_PROVIDER, modelID: process.env.DRUKS_MODEL},
    parts: [{type: "text", text: fs.readFileSync(promptPath, "utf8")}],
    format: {type: "json_schema", schema: JSON.parse(process.env.DRUKS_SCHEMA)},
  };
  fs.writeFileSync(requestPath, JSON.stringify(request));
' "$run_dir/prompt.txt" "$run_dir/request.json" || exit $?

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
session_id=$(node -e '
  process.stdout.write(JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")).id)
' "$run_dir/session.json") || exit $?
curl --fail-with-body --silent --show-error --max-time "$DRUKS_DEADLINE_SECONDS" \
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
if [ "$message_status" -ne 0 ]; then exit "$message_status"; fi
node -e '
  const payload = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
  process.exit(payload.info && payload.info.error ? 1 : 0);
' "$run_dir/response.json"
