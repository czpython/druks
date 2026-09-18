import { spawn, execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import { createServer } from "node:net";
import { homedir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { Readable, Writable } from "node:stream";
import { promisify } from "node:util";
import { ClientSideConnection, ndJsonStream, PROTOCOL_VERSION } from "/opt/druks-chat/node_modules/@agentclientprotocol/sdk/dist/acp.js";

const execute = promisify(execFile);
const home = homedir();
const root = path.join(home, "work", "chat");
const sessions = new Map();
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function conversationRoot(id) {
  if (!uuid.test(id)) throw new Error("Invalid conversation ID.");
  return path.join(root, id);
}

function readState(directory) {
  const filename = path.join(directory, "state.json");
  if (fs.existsSync(filename)) return JSON.parse(fs.readFileSync(filename, "utf8"));
  return { status: "missing", sequence: 0, messageId: "", sessionId: "", epoch: randomUUID(), archivePath: "" };
}

function readEvents(filename) {
  if (!fs.existsSync(filename)) return [];
  return fs.readFileSync(filename, "utf8").split("\n").filter(Boolean).map(line => JSON.parse(line));
}

class Conversation {
  constructor(id) {
    this.root = conversationRoot(id);
    this.state = readState(this.root);
    // Only the current turn's events: Postgres holds every finished turn.
    this.eventsFile = path.join(this.root, "turn.ndjson");
    this.events = readEvents(this.eventsFile);
    this.connection = undefined;
    this.starting = undefined;
    fs.mkdirSync(this.root, { recursive: true });
  }

  save() {
    const filename = path.join(this.root, "state.json");
    fs.writeFileSync(filename + ".tmp", JSON.stringify(this.state), { mode: 0o600 });
    fs.renameSync(filename + ".tmp", filename);
  }

  append(notification) {
    const event = {
      sequence: ++this.state.sequence,
      messageId: this.state.messageId,
      epoch: this.state.epoch,
      notification,
    };
    fs.appendFileSync(this.eventsFile, JSON.stringify(event) + "\n", { mode: 0o600 });
    this.events.push(event);
    this.save();
  }

  beginTurn(messageId, status, stopReason) {
    Object.assign(this.state, { messageId, status, stopReason, archivePath: "" });
    this.events = [];
    fs.writeFileSync(this.eventsFile, "", { mode: 0o600 });
    this.save();
  }

  async configure(connection, request, configOptions) {
    const sessionId = this.state.sessionId;
    await connection.setSessionMode({ sessionId, modeId: request.mode });
    // The session opened on its model through the adapter's _meta. The model option
    // takes only the models the CLI lists, so it switches a live session and no more.
    if (request.model !== this.state.model) {
      ({ configOptions } = await connection.setSessionConfigOption({ sessionId, configId: "model", value: request.model }));
      this.state.model = request.model;
    }
    this.configOptions = configOptions;
    // The adapter offers effort and fast mode only for models that support them.
    const offered = new Set(configOptions.map(option => option.id));
    if (request.effort && offered.has("effort")) {
      await connection.setSessionConfigOption({ sessionId, configId: "effort", value: request.effort });
    }
    if (offered.has("fast")) {
      await connection.setSessionConfigOption({ sessionId, configId: "fast", value: request.fastMode ? "on" : "off" });
    }
  }

  async start(request) {
    if (this.starting) await this.starting;
    if (this.connection) {
      await this.configure(this.connection, request, this.configOptions);
      return this.state;
    }
    this.starting = this.open(request);
    try {
      await this.starting;
      return this.state;
    } finally {
      this.starting = undefined;
    }
  }

  async open(request) {
    const projects = path.join(home, request.sessionFiles);
    const project = path.join(projects, this.root.replace(/[^a-zA-Z0-9]/g, "-"));
    if (!this.state.sessionId && request.archivePath) {
      const restored = path.join(this.root, "restored");
      fs.mkdirSync(restored, { recursive: true });
      await execute("tar", ["-xzf", request.archivePath, "-C", restored]);
      const saved = JSON.parse(fs.readFileSync(path.join(restored, "session.json"), "utf8"));
      this.state.sessionId = saved.sessionId;
      fs.mkdirSync(project, { recursive: true });
      fs.cpSync(path.join(restored, "files"), project, { recursive: true });
    }
    const child = spawn(request.command, [], { cwd: this.root, stdio: ["pipe", "pipe", "pipe"] });
    child.stderr.pipe(fs.createWriteStream(path.join(this.root, "adapter.log"), { flags: "a", mode: 0o600 }));
    const connection = new ClientSideConnection(() => ({
      sessionUpdate: async notification => {
        // session/load replays history that Postgres already holds.
        if (this.state.status === "running") this.append(notification);
      },
      requestPermission: async request => {
        const option = request.options.find(option => option.kind === "allow_once")
          ?? request.options.find(option => option.kind === "allow_always");
        if (!option) throw new Error("The adapter supplied no allow option.");
        return { outcome: { outcome: "selected", optionId: option.optionId } };
      },
    }), ndJsonStream(Writable.toWeb(child.stdin), Readable.toWeb(child.stdout)));
    const stopped = () => {
      if (this.connection === connection) {
        this.connection = undefined;
        if (this.state.status === "running") {
          this.state.status = this.state.stopReason === "cancelled" ? "cancelled" : "interrupted";
          this.save();
        }
      }
    };
    child.once("error", stopped);
    child.once("close", stopped);
    try {
      await connection.initialize({ protocolVersion: PROTOCOL_VERSION, clientInfo: { name: "druks-chat", version: "1" }, clientCapabilities: {} });
      const bearer = process.env[request.bearerVariable];
      if (!bearer) throw new Error("The Druks MCP placeholder is missing.");
      const setup = {
        cwd: this.root,
        mcpServers: [{ name: "druks", type: "http", url: request.mcpUrl, headers: [{ name: "Authorization", value: "Bearer " + bearer }, ...request.headers] }],
        _meta: request.meta,
      };
      const session = this.state.sessionId
        ? await connection.loadSession({ ...setup, sessionId: this.state.sessionId })
        : await connection.newSession(setup);
      this.state.sessionId ||= session.sessionId;
      this.state.model = request.model;
      await this.configure(connection, request, session.configOptions ?? []);
      this.connection = connection;
      this.projects = projects;
      this.state.status = "idle";
      this.save();
    } catch (error) {
      child.kill();
      throw error;
    }
  }

  async archive() {
    const directory = path.join(this.root, "archive");
    const files = path.join(directory, "files");
    fs.rmSync(directory, { recursive: true, force: true });
    fs.mkdirSync(files, { recursive: true });
    let found = false;
    for (const project of fs.readdirSync(this.projects)) {
      const transcript = path.join(this.projects, project, this.state.sessionId + ".jsonl");
      if (fs.existsSync(transcript)) {
        fs.copyFileSync(transcript, path.join(files, this.state.sessionId + ".jsonl"));
        const subagents = path.join(this.projects, project, this.state.sessionId);
        if (fs.existsSync(subagents)) fs.cpSync(subagents, path.join(files, this.state.sessionId), { recursive: true });
        found = true;
      }
    }
    if (!found) throw new Error("The Claude session transcript is missing.");
    fs.writeFileSync(path.join(directory, "session.json"), JSON.stringify({ sessionId: this.state.sessionId }));
    const archivePath = path.join(this.root, "session.tar.gz");
    await execute("tar", ["-czf", archivePath + ".tmp", "-C", directory, "session.json", "files"]);
    fs.renameSync(archivePath + ".tmp", archivePath);
    this.state.archivePath = archivePath;
  }

  async prompt(request) {
    if (request.messageId === this.state.messageId && this.state.status === "cancelled") return this.state;
    if (!this.connection) throw new Error("Start the conversation before a prompt.");
    if (this.state.status === "running") throw new Error("A turn is already running.");
    if (request.messageId === this.state.messageId) throw new Error("The bridge already accepted this message.");
    this.beginTurn(request.messageId, "running", "");
    const connection = this.connection;
    // The detached process owns the turn, not the requesting SSH channel.
    void (async () => {
      let timedOut = false;
      // Past its time limit the turn stops, and Druks saves no reply for it.
      const timer = request.timeout && setTimeout(() => {
        timedOut = true;
        connection.cancel({ sessionId: this.state.sessionId }).catch(console.error);
      }, request.timeout * 1000);
      try {
        const result = await connection.prompt({ sessionId: this.state.sessionId, prompt: [{ type: "text", text: request.body }] });
        await this.archive();
        this.state.stopReason = result.stopReason;
        if (timedOut) this.state.status = "interrupted";
        else this.state.status = this.state.stopReason === "cancelled" ? "cancelled" : "replied";
      } catch (error) {
        console.error(error);
        this.state.status = this.state.stopReason === "cancelled" ? "cancelled" : "interrupted";
      } finally {
        clearTimeout(timer);
        this.save();
      }
    })();
    return this.state;
  }

  eventsAfter(sequence) {
    return this.events.filter(event => event.sequence > sequence).slice(0, 128);
  }
}

async function handle(request) {
  if (request.method === "ping") return {};
  const directory = conversationRoot(request.conversationId);
  let conversation = sessions.get(request.conversationId);
  if (!conversation) {
    if (request.method === "status") return readState(directory);
    conversation = new Conversation(request.conversationId);
    sessions.set(request.conversationId, conversation);
  }
  switch (request.method) {
    case "start": return conversation.start(request);
    case "status": return conversation.state;
    case "prompt": return conversation.prompt(request);
    case "events": return { events: conversation.eventsAfter(request.after) };
    case "cancel":
      if (request.messageId !== conversation.state.messageId) {
        // Druks marked the message delivered, but its prompt never reached the bridge.
        if (conversation.state.status === "running") throw new Error("Another turn is running.");
        conversation.beginTurn(request.messageId, "cancelled", "cancelled");
      } else if (conversation.state.status === "running") {
        conversation.state.stopReason = "cancelled";
        conversation.save();
        await conversation.connection.cancel({ sessionId: conversation.state.sessionId });
      }
      return conversation.state;
    default: throw new Error("Unknown bridge method.");
  }
}

const server = createServer(socket => {
  socket.on("error", () => socket.destroy());
  const lines = createInterface({ input: socket });
  lines.once("line", line => {
    lines.close();
    Promise.resolve().then(() => handle(JSON.parse(line)))
      .then(result => socket.end(JSON.stringify({ ok: true, ...result }) + "\n"))
      .catch(error => {
        console.error(error);
        socket.end(JSON.stringify({ ok: false, error: "The bridge request failed. Read chat-bridge.log in the sandbox." }) + "\n");
      });
  });
});

// Bind first so a second launcher cannot change the active process's state.
server.listen(Number(process.argv[2]), "127.0.0.1", () => {
  fs.mkdirSync(root, { recursive: true });
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (entry.isDirectory() && uuid.test(entry.name)) {
      const conversation = new Conversation(entry.name);
      if (conversation.state.status === "running") {
        conversation.state.status = conversation.state.stopReason === "cancelled" ? "cancelled" : "interrupted";
        conversation.save();
      }
      sessions.set(entry.name, conversation);
    }
  }
});
