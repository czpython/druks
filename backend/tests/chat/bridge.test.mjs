import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import { createConnection, createServer } from "node:net";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const sdk = pathToFileURL(process.env.CHAT_TEST_SDK).href;
const bridge = fileURLToPath(new URL("../../druks/chat/bridge.mjs", import.meta.url));
const bootstrap = "data:text/javascript," + encodeURIComponent(
  "import { registerHooks } from 'node:module'; registerHooks({ resolve(name, context, next) {" +
  "return next(name === '/opt/druks-chat/node_modules/@agentclientprotocol/sdk/dist/acp.js' ? " +
  JSON.stringify(sdk) + " : name, context); }});"
);

async function request(port, values, abandon = false) {
  const socket = createConnection({ host: "127.0.0.1", port });
  await once(socket, "connect");
  socket.write(JSON.stringify(values) + "\n");
  if (abandon) {
    socket.destroy();
    return;
  }
  let reply = "";
  for await (const chunk of socket) reply += chunk;
  return JSON.parse(reply);
}

const id = number => `00000000-0000-7000-8000-${String(number).padStart(12, "0")}`;
const message = number => id(100 + number);

async function until(action, matches) {
  for (let attempt = 0; attempt < 200; attempt++) {
    const result = await action();
    if (matches(result)) return result;
    await delay(25);
  }
  throw new Error("The bridge did not reach the expected state.");
}

test("The bridge streams detached turns, isolates archives, cancels, and reloads sessions", async t => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "druks-chat-test-"));
  const home = path.join(temporary, "sandbox");
  await fs.mkdir(home);
  const adapter = path.join(temporary, "adapter.mjs");
  await fs.writeFile(adapter, [
    "#!/usr/bin/env node",
    "import { AgentSideConnection, ndJsonStream, PROTOCOL_VERSION } from " + JSON.stringify(sdk) + ";",
    "import fs from 'node:fs'; import path from 'node:path';",
    "import { homedir } from 'node:os'; import { randomUUID } from 'node:crypto';",
    "import { Readable, Writable } from 'node:stream';",
    "let sessionId, cwd, release, memory = '';",
    "const transcript = () => path.join(homedir(), '.claude/projects', cwd.replace(/[^a-zA-Z0-9]/g, '-'), sessionId + '.jsonl');",
    "new AgentSideConnection(client => ({",
    "initialize: async () => ({protocolVersion: PROTOCOL_VERSION, agentCapabilities: {loadSession: true}}),",
    "newSession: async p => { cwd=p.cwd; sessionId=randomUUID();",
    " if (p._meta.harness !== 'options') throw Error('meta');",
    " if (p.mcpServers[0].headers[0].value !== 'Bearer placeholder') throw Error('token');",
    " return {sessionId, configOptions: [{id:'model',currentValue:'default',options:[{value:'default'},{value:'opus[1m]'},{value:'sonnet'}]},{id:'effort'},{id:'fast'}]}; },",
    "loadSession: async p => { cwd=p.cwd; sessionId=p.sessionId; memory=fs.readFileSync(transcript(), 'utf8');",
    " await client.sessionUpdate({sessionId,update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:'replay'}}}); return {}; },",
    "setSessionMode: async p => { if (p.modeId !== 'bypassPermissions') throw Error('mode'); return {}; },",
    "setSessionConfigOption: async p => { fs.appendFileSync(path.join(cwd, 'config.log'), p.configId + '=' + p.value + '\\n');",
    " return {configOptions: [{id:'model',currentValue:'default',options:[{value:'default'},{value:'opus[1m]'},{value:'sonnet'}]},{id:'effort'},{id:'fast'}]}; },",
    "cancel: async () => { release?.(); },",
    "prompt: async p => {",
    " const permission = await client.requestPermission({sessionId,toolCall:{toolCallId:'permission',title:'Read'},options:[{optionId:'no',name:'Deny',kind:'reject_once'},{optionId:'yes',name:'Allow',kind:'allow_once'}]});",
    " if (permission.outcome.optionId !== 'yes') throw Error('permission');",
    " const text = p.prompt[0].text;",
    " await client.sessionUpdate({sessionId,update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:memory+text}}});",
    " if(text==='wait') await new Promise(resolve => { release=resolve; });",
    " else await new Promise(resolve => setTimeout(resolve, 100));",
    " memory += text; fs.mkdirSync(path.dirname(transcript()),{recursive:true}); fs.writeFileSync(transcript(),memory);",
    " return {stopReason: text==='wait'?'cancelled':'end_turn'};",
    "}",
    "}), ndJsonStream(Writable.toWeb(process.stdout),Readable.toWeb(process.stdin)));",
  ].join("\n"), { mode: 0o755 });

  const listener = createServer();
  listener.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const port = listener.address().port;
  listener.close();
  await once(listener, "close");
  const processes = [];
  t.after(async () => {
    for (const process of processes) {
      try { globalThis.process.kill(-process.pid, "SIGKILL"); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    await fs.rm(temporary, { recursive: true, force: true });
  });
  async function launch(directory) {
    const child = spawn(process.execPath, ["--import", bootstrap, bridge, String(port)], {
      detached: true, stdio: ["ignore", "ignore", "pipe"],
      env: { ...process.env, HOME: directory, MCP_DRUKS_TOKEN: "placeholder" },
    });
    processes.push(child);
    child.stderr.on("data", chunk => t.diagnostic(chunk.toString()));
    await until(
      () => request(port, { method: "ping" }).catch(() => ({ ok: false })),
      result => result.ok,
    );
    return child;
  }
  const start = conversation => ({
    method: "start", conversationId: conversation, archivePath: "", command: adapter,
    sessionFiles: ".claude/projects", mode: "bypassPermissions", model: "claude-opus-4-7", meta: { harness: "options" },
    effort: "high", fastMode: true, bearerVariable: "MCP_DRUKS_TOKEN", mcpUrl: "https://hooks.example.com/mcp",
  });
  const status = conversation => request(port, { method: "status", conversationId: conversation });
  const first = await launch(home);
  assert.equal((await request(port, start(id(1)))).ok, true);
  assert.equal((await request(port, start(id(2)))).ok, true);
  assert.equal((await request(port, { ...start(id(1)), effort: "low", fastMode: false })).ok, true);
  const settings = await fs.readFile(path.join(home, "work", "chat", id(1), "config.log"), "utf8");
  assert.deepEqual(settings.trim().split("\n"), ["model=opus[1m]", "effort=high", "fast=on", "model=opus[1m]", "effort=low", "fast=off"]);

  await request(port, { method: "prompt", conversationId: id(1), messageId: message(1), body: "remember-one" }, true);
  await until(() => request(port, { method: "events", conversationId: id(1), after: 0 }), result => result.events.length > 0);
  const saved = await until(() => status(id(1)), result => result.status === "replied");
  await request(port, { method: "prompt", conversationId: id(2), messageId: message(2), body: "private-two" });
  await until(() => status(id(2)), result => result.status === "replied");

  await request(port, { method: "prompt", conversationId: id(1), messageId: message(3), body: "wait" });
  await until(() => status(id(1)), result => result.status === "running");
  assert.equal((await request(port, { method: "prompt", conversationId: id(1), messageId: message(4), body: "duplicate" })).ok, false);
  await until(() => request(port, { method: "events", conversationId: id(1), after: 0 }), result => result.events.some(event => event.messageId === message(3)));
  assert.equal((await request(port, { method: "events", conversationId: id(1), after: 0 })).events.some(event => event.messageId === message(1)), false);
  await request(port, { method: "cancel", conversationId: id(1), messageId: message(3) });
  assert.equal((await until(() => status(id(1)), result => result.status === "cancelled")).stopReason, "cancelled");
  const partial = await request(port, { method: "events", conversationId: id(1), after: 0 });
  assert.match(partial.events.find(event => event.messageId === message(3)).notification.update.content.text, /wait/);
  assert.equal((await request(port, { method: "prompt", conversationId: id(1), messageId: message(3), body: "again" })).status, "cancelled");

  await request(port, { method: "cancel", conversationId: id(1), messageId: message(4) });
  assert.equal((await request(port, { method: "prompt", conversationId: id(1), messageId: message(4), body: "never send" })).status, "cancelled");
  assert.equal((await request(port, { method: "events", conversationId: id(1), after: 0 })).events.some(event => event.messageId === message(4)), false);

  const restoredHome = path.join(temporary, "restored-sandbox");
  await fs.mkdir(restoredHome);
  const archive = path.join(temporary, "session.tar.gz");
  await fs.copyFile(saved.archivePath, archive);
  process.kill(-first.pid, "SIGKILL");
  await once(first, "exit");
  const second = await launch(restoredHome);
  assert.equal((await request(port, { ...start(id(1)), archivePath: archive })).ok, true);
  assert.equal((await request(port, { method: "events", conversationId: id(1), after: 0 })).events.length, 0);
  await request(port, { method: "prompt", conversationId: id(1), messageId: message(5), body: "continue" });
  await until(() => status(id(1)), result => result.status === "replied");
  const events = (await request(port, { method: "events", conversationId: id(1), after: 0 })).events;
  assert.match(events[0].notification.update.content.text, /remember-one/);
  assert.doesNotMatch(events[0].notification.update.content.text, /private-two|replay/);
  assert.equal((await status(id(2))).status, "missing");

  await request(port, { method: "prompt", conversationId: id(1), messageId: message(6), body: "wait" });
  await request(port, { method: "cancel", conversationId: id(1), messageId: message(5) });
  assert.equal((await status(id(1))).status, "running");
  assert.equal((await status(id(1))).stopReason, "");
  process.kill(-second.pid, "SIGKILL");
  await once(second, "exit");
  await launch(restoredHome);
  assert.equal((await status(id(1))).status, "interrupted");
});
