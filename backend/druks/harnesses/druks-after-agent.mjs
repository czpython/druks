// Druks reads the request and writes the answer over SFTP.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const request = new URL("after_agent.request", import.meta.url);
const answer = new URL("after_agent.answer", import.meta.url);

// One round per call: the agent's next end of turn is final.
if (!existsSync(request)) {
  writeFileSync(request, "");
  while (!existsSync(answer)) await sleep(500);
  const text = readFileSync(answer, "utf8");
  const replies = {
    claude: {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: text,
      },
    },
    codex: { decision: "block", reason: text },
  };
  const harness = process.argv[2];
  if (!harness) process.stdout.write(text);
  else if (text) process.stdout.write(JSON.stringify(replies[harness]));
}
