#!/bin/sh
# druks-sandbox — minimal in-VM helper for sandboxed agent runs.
#
# Uploaded by Druks to <home>/druks-sandbox (the SSH user's home; the
# previous /usr/local/bin path needed root and broke when the image
# SSHes as a non-root user like ``exedev``) at sandbox-acquire time and
# invoked over SSH by the runner. Four verbs:
#
#   exec-start --run-id ID --cwd PATH [--env-file PATH]
#               [--stdin-from PATH] -- CMD [ARGS...]
#       Spawn CMD detached. Writes pid + stdout.jsonl + stderr.log +
#       (eventually) exit_code under $DRUKS_SANDBOX_RUNS_ROOT/ID/.
#       Must be invoked under `nohup setsid ... </dev/null >/dev/null
#       2>&1 &` so the process survives the SSH session that started it.
#       ``--stdin-from PATH`` redirects the user command's stdin from
#       PATH (defaults to /dev/null). Used for prompts too large to
#       safely ride the SSH exec channel — the harness SFTPs the
#       prompt to a file in the run dir and points stdin at it.
#
#   exec-kill --run-id ID
#       SIGTERM the pid recorded for ID. Best-effort: succeeds even
#       when the process is already gone.
#
#   read-file <workspace> <reported-path> <max-bytes>
#       Stream a bounded regular file that resolves inside <workspace>
#       to stdout. Rejects missing paths, non-files, symlink escapes,
#       and oversized files with a distinct exit code.
#
#   git-credential <get|store|erase>
#       git credential helper. Configured via
#       `credential.https://github.com.helper '!druks-sandbox git-credential'`
#       so every in-VM git operation against github.com reads the
#       installation token from $DRUKS_GITHUB_TOKEN_FILE on demand.
#       The file is written once at acquire time and is NOT refreshed —
#       a run that outlives the ~60 min token expiry will 401 on a late
#       push. TODO: mint a fresh token here on demand instead of catting
#       a static file (see druks/sandbox/credentials.py).
#
# Env:
#   DRUKS_SANDBOX_RUNS_ROOT   default $HOME/work/runs
#   DRUKS_GITHUB_TOKEN_FILE   default $HOME/work/github-token
#
# Defaults derive from $HOME so the script resolves to the SSH user's
# home regardless of which user invokes it (nohup/setsid don't switch
# users, git's credential helper inherits env). Previously the
# defaults hardcoded /work/* which only root could create.

set -u

# Nested fallback so a stripped env (no HOME, no DRUKS_*) doesn't
# trip `set -u`. Real VMs always set HOME; tests that override
# DRUKS_SANDBOX_RUNS_ROOT or DRUKS_GITHUB_TOKEN_FILE don't need it.
runs_root="${DRUKS_SANDBOX_RUNS_ROOT:-${HOME:-/root}/work/runs}"

die() {
    echo "druks-sandbox: $1" >&2
    exit "${2:-64}"
}

verb="${1:-}"
[ -z "$verb" ] && die "usage: druks-sandbox <exec-start|exec-kill|git-credential|read-file> [options]"
shift

# read-file streams a bounded regular file that must resolve inside the given
# workspace — the transport for agent-declared output files:
#   druks-sandbox read-file <workspace> <reported-path> <max-bytes>
if [ "$verb" = "read-file" ]; then
    workspace=$(realpath "$1")
    reported="$2"
    limit="$3"
    case "$reported" in
        /*) candidate="$reported" ;;
        *) candidate="$workspace/$reported" ;;
    esac
    resolved=$(realpath "$candidate" 2>/dev/null) || die "reported file is missing: $reported" 2
    [ -e "$resolved" ] || die "reported file is missing: $reported" 2
    [ -f "$resolved" ] || die "reported path is not a regular file: $reported" 3
    case "$resolved" in
        "$workspace"/*) ;;
        *) die "reported file escapes the workspace: $reported" 3 ;;
    esac
    size=$(wc -c < "$resolved")
    [ "$size" -le "$limit" ] || die "reported file exceeds the $limit-byte limit: $reported" 4
    cat "$resolved"
    exit 0
fi

# git-credential is special: git invokes it as
#   druks-sandbox git-credential <get|store|erase>
# with the credential request on stdin. It has nothing to do with the
# run-oriented --run-id/--cwd options below, so handle it before the
# shared parser (which would reject the missing --run-id).
if [ "$verb" = "git-credential" ]; then
    op="${1:-}"
    # Only 'get' needs an answer. store/erase are deliberate no-ops:
    # the token file is managed out-of-band by Druks over SFTP, not
    # by git, so there's nothing to persist or clear git-side.
    [ "$op" = "get" ] || exit 0
    token_file="${DRUKS_GITHUB_TOKEN_FILE:-${HOME:-/root}/work/github-token}"
    [ -r "$token_file" ] || exit 0
    echo "username=x-access-token"
    printf 'password=%s\n' "$(cat "$token_file")"
    exit 0
fi

# Shared option parsing — --run-id / --cwd / --env-file / --stdin-from
# are recognised; -- ends option parsing and exec-start consumes the rest.
run_id=""
cwd=""
env_file=""
stdin_from="/dev/null"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --run-id) run_id="$2"; shift 2 ;;
        --cwd) cwd="$2"; shift 2 ;;
        --env-file) env_file="$2"; shift 2 ;;
        --stdin-from) stdin_from="$2"; shift 2 ;;
        --) shift; break ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -z "$run_id" ] && die "--run-id required"
run_dir="$runs_root/$run_id"

case "$verb" in
    exec-kill)
        pid_file="$run_dir/pid"
        if [ -r "$pid_file" ]; then
            kill "$(cat "$pid_file")" 2>/dev/null || true
        fi
        exit 0
        ;;
    exec-start)
        ;;  # fall through
    *)
        die "unknown verb: $verb"
        ;;
esac

# ---- exec-start ----------------------------------------------------------

[ -z "$cwd" ] && die "--cwd required for exec-start"
[ "$#" -eq 0 ] && die "command required after --"

mkdir -p "$run_dir"
: > "$run_dir/stdout.jsonl"
: > "$run_dir/stderr.log"

# Source env if provided. Empty / missing env_file means "no extra env"
# — the caller can omit --env-file entirely for runs that don't need
# any.
if [ -n "$env_file" ] && [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
fi

# Authenticate `gh` (and any GITHUB_TOKEN-aware tool) with the same
# installation token git's credential helper serves. Re-read per spawn
# so a token rotation between runs is picked up; mid-run rotations stay
# invisible to a long-running agent (env vars don't update post-exec).
token_file="${DRUKS_GITHUB_TOKEN_FILE:-${HOME:-/root}/work/github-token}"
if [ -r "$token_file" ]; then
    GH_TOKEN="$(cat "$token_file")"
    GITHUB_TOKEN="$GH_TOKEN"
    export GH_TOKEN GITHUB_TOKEN
fi

# cd is a fatal failure path because the agent obviously can't run if
# its working tree doesn't exist. Write a sentinel exit code so the
# orchestrator's wait() returns instead of hanging.
if ! cd "$cwd"; then
    printf '127' > "$run_dir/exit_code.tmp"
    mv "$run_dir/exit_code.tmp" "$run_dir/exit_code"
    echo "could not cd to $cwd" >> "$run_dir/stderr.log"
    exit 127
fi

# Spawn the user command in the background so we capture its pid
# before it has a chance to exit. stdin defaults to /dev/null (no
# typist) unless ``--stdin-from`` pointed it at a real file — that's
# how the harness ships multi-hundred-KB prompts without hitting the
# SSH exec-channel size limit. stdout/stderr append-redirected to
# the per-run files the orchestrator tails over SFTP.
"$@" <"$stdin_from" >> "$run_dir/stdout.jsonl" 2>> "$run_dir/stderr.log" &
child_pid=$!
printf '%s' "$child_pid" > "$run_dir/pid"

# Wait for the child, then write exit code atomically (temp + rename)
# so a reader never sees a partial file.
wait "$child_pid"
ec=$?
printf '%s' "$ec" > "$run_dir/exit_code.tmp"
mv "$run_dir/exit_code.tmp" "$run_dir/exit_code"
exit "$ec"
