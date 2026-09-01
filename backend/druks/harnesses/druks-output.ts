// Druks' terminating result tool: pi has no output-schema flag, so the run's
// contract rides as this tool's parameters and the agent ends its turn by
// calling it. pi loads this file by absolute path from the run directory,
// which is outside every node_modules — so it imports nothing from pi.
import { readFileSync, writeFileSync } from "node:fs";

const schemaPath = process.env.DRUKS_SCHEMA_PATH;
const resultPath = process.env.DRUKS_RESULT_PATH;

export default function (pi) {
  pi.registerTool({
    name: "submit_result",
    label: "Submit result",
    description: "Return the final result object for this task. Calling this ends the turn.",
    promptSnippet: "Return the final result object with submit_result",
    promptGuidelines: [
      "Finish by calling submit_result exactly once with the final result object.",
      "Do not send an assistant message after submit_result.",
    ],
    parameters: JSON.parse(readFileSync(schemaPath, "utf8")),
    async execute(_toolCallId, params) {
      writeFileSync(resultPath, JSON.stringify(params));
      return { content: [{ type: "text", text: "result recorded" }], terminate: true };
    },
  });
}
