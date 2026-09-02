# One structured opencode call: write config and schema, start `opencode
# serve`, open a session, POST the message with the contract schema, and
# print the POST response. Inputs ride the environment (DRUKS_*,
# OPENCODE_AUTH_CONTENT); the prompt rides stdin. The message POST carries
# the deadline — an unsatisfiable schema loops forever server-side, so on
# expiry the wrapper aborts the session and exits 124.

run_dir="$DRUKS_RUN_DIR"
mkdir -p "$run_dir" &&
printf '%s' "$DRUKS_OPENCODE_CONFIG" > "$run_dir/opencode.json" &&
printf '%s' "$DRUKS_SCHEMA" > "$run_dir/schema.json" &&
cat > "$run_dir/prompt.txt" &&
node -e '
  const fs = require("fs");
  const [schemaPath, promptPath, requestPath, provider, model] = process.argv.slice(1);
  const request = {
    model: {providerID: provider, modelID: model},
    parts: [{type: "text", text: fs.readFileSync(promptPath, "utf8")}],
    format: {type: "json_schema", schema: JSON.parse(fs.readFileSync(schemaPath, "utf8"))},
  };
  fs.writeFileSync(requestPath, JSON.stringify(request));
' "$run_dir/schema.json" "$run_dir/prompt.txt" "$run_dir/request.json" \
  "$DRUKS_PROVIDER" "$DRUKS_MODEL" || exit $?

OPENCODE_CONFIG="$run_dir/opencode.json" opencode serve \
  --hostname 127.0.0.1 --port 4096 > "$run_dir/opencode.log" 2>&1 &
server_pid=$!
cleanup() { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

# The first call doubles as the readiness wait: retry only refused
# connections while the server boots (~0.5s).
if ! curl --fail --silent --show-error --retry 15 --retry-delay 1 --retry-connrefused \
    -X POST "http://127.0.0.1:4096/session?directory=$DRUKS_WORKSPACE_QUERY" \
    -H 'content-type: application/json' --data '{}' > "$run_dir/session.json"; then
  cat "$run_dir/opencode.log" >&2
  exit 1
fi
session_id=$(node -e '
  process.stdout.write(JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")).id)
' "$run_dir/session.json") || exit $?

curl --fail-with-body --silent --show-error --max-time "$DRUKS_DEADLINE_SECONDS" \
  -X POST "http://127.0.0.1:4096/session/$session_id/message" \
  -H 'content-type: application/json' \
  --data-binary @"$run_dir/request.json" > "$run_dir/response.json"
message_status=$?
if [ "$message_status" -eq 28 ]; then
  curl --fail --silent --show-error -X POST \
    "http://127.0.0.1:4096/session/$session_id/abort" \
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
