# Druks reads the request and writes the answer over SFTP. An exe.dev box has
# no node, so this needs only sh.
run_dir=$(dirname "$0")

# One round per call: the agent's next end of turn is final.
if [ ! -e "$run_dir/after_agent.request" ]; then
  : > "$run_dir/after_agent.request"
  until [ -e "$run_dir/after_agent.answer" ]; do sleep 0.5; done
  cat "$run_dir/after_agent.answer"
fi
