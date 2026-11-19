# An exe.dev box has no node, jq or python, so this needs only sh and awk.
LC_ALL=C awk '
BEGIN {
  before = "(^|[^A-Za-z0-9_.-])"
  after = "([^A-Za-z0-9_-]|$)"
  unit["s"] = 1
  unit["m"] = 60
  unit["h"] = 3600
  unit["d"] = 86400
  reason_base = "Druks denied this command under its no-waiting rule: an agent does not block in its turn on a command that waits. "
  reason_tail = " If the match is only quoted text you are passing to another command, write that text to a file with a file-edit tool and pass the file instead, or rephrase it."
}

{ json = json $0 "\n" }

# Print the deny reply, naming why the command waits.
function deny(trigger) {
  print "{\"hookSpecificOutput\": {\"hookEventName\": \"PreToolUse\", \"permissionDecision\": \"deny\", \"permissionDecisionReason\": \"" reason_base trigger reason_tail "\"}}"
}

# The seconds one sleep with the arguments in "args" lasts, or -1 when a length
# is not a number (an unbounded wait Druks cannot measure).
function sleep_seconds(args,    words, count, i, word, suffix, total) {
  count = split(args, words)
  for (i = 1; i <= count; i++) {
    word = words[i]
    suffix = substr(word, length(word))
    if (word ~ /^([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?[smhd]?$/)
      total += word * (suffix in unit ? unit[suffix] : 1)
    else if (word ~ /^[A-Za-z#]/ && tolower(word) !~ /^inf(inity)?$/)
      break
    else if (word !~ /^-/)
      return -1
  }
  return total
}

# Whether the sleeps in "text" last over 2 minutes in total.
function sleeps_long(text,    args, seconds, total) {
  while (match(text, before "sleep[ \t]")) {
    text = substr(text, RSTART + RLENGTH)
    args = text
    if (match(args, /[;&|<>()\n]/))
      args = substr(args, 1, RSTART - 1)
    seconds = sleep_seconds(args)
    if (seconds < 0)
      return 1
    total += seconds
  }
  return total > 120
}

END {
  if (!match(json, /"tool_input"[ \t\r\n]*:[ \t\r\n]*\{/))
    exit
  json = substr(json, RSTART + RLENGTH)
  if (!match(json, /"command"[ \t\r\n]*:[ \t\r\n]*"/))
    exit
  json = substr(json, RSTART + RLENGTH)
  # With escaped backslashes and quotes set aside, the first quote ends the command.
  gsub(/\\\\/, "\001", json)
  gsub(/\\"/, "\002", json)
  command = substr(json, 1, index(json, "\"") - 1)
  gsub(/\\n/, "\n", command)
  gsub(/\\\//, "/", command)
  # JSON.stringify writes \u only for control characters.
  gsub(/\\([bfrt]|u[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f])/, " ", command)
  # The shell drops quotes, backslashes, and a line break after a backslash.
  gsub(/\001\n/, "", command)
  gsub(/[\001\002\047]/, "", command)
  # --watch=false runs once and exits.
  gsub(/--watch=false/, "", command)
  if (command ~ (before "--watch" after))
    deny("This command uses `--watch`, which runs until it is interrupted.")
  else if (sleeps_long(command))
    deny("This command sleeps for over 2 minutes in total, or for a length that is not a number.")
}
'
