/**
 * bmem native DeepSeek Harness (DSH) plugin.
 *
 * Mounted as an absolute file-URL Cordis row by `bmem connect dsh`. The plugin
 * uses only documented DSH surfaces:
 *
 *  - `session/event` captures committed user/assistant/tool messages.
 *  - `systemPrompt.context()` registers the recall contribution in the native
 *    runtime-context registry, and `agent/pre-step` performs the async recall
 *    that refreshes the per-agent cache.  `dsh-agent-loop` re-reads registered
 *    providers through `systemPrompt.refreshContext(assembly, {agent, scope, …})`
 *    immediately before it admits the loop-owned runtime-context snapshot, so
 *    the recall text reaches the model without the plugin forging a message.
 *  - `ctx.tools.register` exposes explicit recall/save tools.
 *
 * Recall is deliberately NOT appended to the pre-step decision messages: the
 * installed loop (`dsh-agent-loop/lib/index.js` step()) replaces every
 * `decision.messages` entry whose `source.kind === "runtime-context"` with its
 * own host snapshot, so such a message is discarded.  The registry above is the
 * supported channel.
 *
 * It intentionally has no package imports so an absolute-file plugin row works
 * without installing another package into the DSH profile.
 */

import { createHash, randomUUID } from "node:crypto";

export const name = "bmem-native";
export const inject = ["tools", "systemPrompt"];

const MAX_BODY = 100_000;
const TRUNCATION_SUFFIX = "\n[bmem truncated]";
const MARKER_RE = /\[bmem-injection:([A-Za-z0-9._:-]+)\]/;
const REMEMBER_RE = /(?:^\s*(?:请|务必)?记住|^\s*remember(?:\s+that)?\b|^\s*(?:please\s+)?remember\s*:|请记住)/i;
const USER_SOURCE_KINDS = new Set(["user", "user-rpc"]);
const CONTEXT_NAME = "bmem:recall";
const CONTEXT_ORDER = 220;
const RECALL_STATES = 64;

function apiBase(config) {
  const raw = String(
    config?.url ||
    process.env.BMEM_URL ||
    `http://127.0.0.1:${process.env.BMEM_PORT || "8790"}`,
  ).trim();
  const withScheme = raw.includes("://") ? raw : `http://${raw}`;
  return withScheme.replace(/\/+$/, "").replace(/\/(?:mcp|api)$/, "");
}

function isoTime(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    const ms = value > 10_000_000_000 ? value : value * 1000;
    return new Date(ms).toISOString();
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  return new Date().toISOString();
}

/**
 * Durable text blocks of one message, in the same order and with the same
 * filtering as `bmem.adapters._content_parts`, so a live capture and a later
 * JSONL re-import of the same message agree on the whole-message body.
 */
function contentParts(content) {
  const parts = [];
  const push = (text, kind) => {
    if (typeof text === "string" && text.trim()) parts.push({ text, kind });
  };
  if (typeof content === "string") {
    push(content, "text");
    return parts;
  }
  if (Array.isArray(content)) {
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const type = String(block.type || "");
      if (["text", "input_text", "output_text"].includes(type)) {
        push(block.text, "text");
      } else if (["tool_result", "tool-output", "tool_output"].includes(type)) {
        const nested = block.content === undefined ? block.output : block.content;
        if (typeof nested === "string") {
          push(nested, "tool_result");
        } else {
          for (const part of contentParts(nested)) parts.push({ text: part.text, kind: "tool_result" });
        }
      } else if (["image", "file", "audio", "thinking", "reasoning"].includes(type)) {
        continue;
      } else if (typeof block.text === "string") {
        push(block.text, type || "text");
      }
    }
  }
  return parts;
}

/** Whole-message body: every durable text block joined by newlines. */
function messageBody(message) {
  return contentParts(message?.content).map((part) => part.text).join("\n").trim();
}

function messageKind(message) {
  const parts = contentParts(message?.content);
  return parts.some((part) => part.kind === "tool_result") ? "tool_result" : "text";
}

function bodyOf(value) {
  if (typeof value !== "string") return "";
  const text = value.trim();
  if (!text) return "";
  if (text.length <= MAX_BODY) return text;
  return `${text.slice(0, MAX_BODY - TRUNCATION_SUFFIX.length)}${TRUNCATION_SUFFIX}`;
}

function markerId(text) {
  const match = MARKER_RE.exec(text);
  return match ? match[1] : null;
}

/**
 * Canonical DSH identity shared with the JSONL importer:
 * `dsh:<session-id>:<seq>:<message-id>` over the whole-message body.  A missing
 * message id falls back to a body digest, which both sides can compute.
 */
function seqToken(seq) {
  if (typeof seq === "number" && Number.isSafeInteger(seq)) return String(seq);
  if (typeof seq === "string" && seq.trim()) return seq.trim();
  return "-";
}

function messageToken(id, body) {
  if (typeof id === "string" && id.trim()) return id.trim();
  return `body:${createHash("sha256").update(body).digest("hex").slice(0, 16)}`;
}

function nativeEventId(sessionId, seq, id, body) {
  return `dsh:${sessionId}:${seqToken(seq)}:${messageToken(id, body)}`;
}

function postJson(base, path, payload, timeoutMs = 1800) {
  return fetch(base + path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeoutMs),
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(`bmem HTTP ${response.status} for ${path}`);
    }
    return response.json();
  });
}

function eventPayload(message, event, session, config, role, sourceType, origin) {
  const sid = String(session?.id || session?.header?.id || "dsh");
  const project = String(
    session?.header?.cwd || session?.meta?.cwd || config?.project || "",
  );
  const body = bodyOf(messageBody(message));
  if (!body) return null;
  const id = message?.id;
  const seq = event?.seq;
  const nativeId = nativeEventId(sid, seq, id, body);
  const marker = markerId(body);
  const explicitRemember = role === "user" && REMEMBER_RE.test(body);
  const metadata = {
    origin,
    role,
    session_id: sid,
    turn_id: String(event?.data?.turn ?? ""),
    seq: seqToken(seq),
    message_id: messageToken(id, body),
    block_kind: messageKind(message),
    parser_version: "bmem-dsh-plugin-1",
    truncated: body.endsWith("[bmem truncated]"),
  };
  if (marker) {
    metadata.injection_id = marker;
    metadata.injection_reference = true;
  }
  if (explicitRemember) metadata.explicit_remember = true;
  const source_type = marker
    ? "injection_reference"
    : (explicitRemember ? "user_declaration" : sourceType);
  const occurredAt = isoTime(event?.time);
  if (explicitRemember && !marker) {
    return {
      mode: "declare",
      payload: {
        body,
        harness: "dsh",
        device_id: "local",
        native_event_id: nativeId,
        operation_id: nativeId,
        source_type: "user_declaration",
        occurred_at: occurredAt,
        session_id: sid,
        project,
        sensitivity: "normal",
        metadata,
        kind: "fact",
        conditions: {},
        derived_from: [],
      },
    };
  }
  return {
    mode: "event",
    payload: {
      body,
      harness: "dsh",
      device_id: "local",
      native_event_id: nativeId,
      operation_id: nativeId,
      source_type,
      occurred_at: occurredAt,
      session_id: sid,
      project,
      sensitivity: "normal",
      metadata,
    },
  };
}

async function deliver(base, candidate) {
  if (!candidate) return false;
  try {
    const path = candidate.mode === "declare"
      ? "/api/declarations"
      : "/api/events";
    await postJson(base, path, candidate.payload, 1800);
    return true;
  } catch {
    // Capture must never break a harness turn.
    return false;
  }
}

function listenForCapture(ctx, base, config) {
  ctx.on("session/event", (session, event) => {
    try {
      if (!event || typeof event !== "object") return;
      const baseEvent = event;
      if (baseEvent.type === "user/message" && baseEvent.data) {
        const message = baseEvent.data;
        const sourceKind = message?.source?.kind;
        if (sourceKind && !USER_SOURCE_KINDS.has(sourceKind)) return;
        void deliver(base, eventPayload(message, baseEvent, session, config, "user", "user_dialogue", "dsh-session-event"));
      } else if (baseEvent.type === "assistant/message" && baseEvent.data?.message) {
        const message = baseEvent.data.message;
        const sourceKind = message?.source?.kind;
        if (sourceKind && sourceKind !== "model") return;
        void deliver(base, eventPayload(message, baseEvent, session, config, "assistant", "agent_inference", "dsh-assistant-message"));
      } else if (baseEvent.type === "tool/result" && baseEvent.data?.message) {
        const message = baseEvent.data.message;
        const sourceKind = message?.source?.kind;
        if (sourceKind && sourceKind !== "tool") return;
        void deliver(base, eventPayload(message, baseEvent, session, config, "tool", "external", "dsh-tool-result"));
      }
    } catch {
      // Listener containment: DSH already contains throw/reject, but stay safe.
    }
  });
}

function latestUserText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || message.role !== "user") continue;
    const sourceKind = message.source?.kind;
    if (sourceKind && !USER_SOURCE_KINDS.has(sourceKind)) continue;
    const text = messageBody(message);
    if (text) return text;
  }
  return "";
}

/**
 * Per-agent recall text, read back by the registered system-prompt context
 * provider.  The loop passes `assembleContextFor(agent, signal)` to
 * `refreshContext`, so the provider receives the same agent subject that
 * `agent/pre-step` saw; the host owns the actual message.
 */
function createRecallCache() {
  const byObject = new WeakMap();
  const byKey = new Map();

  const keysFor = (subject) => {
    const keys = [];
    if (!subject || typeof subject !== "object") return keys;
    if (typeof subject.id === "string" && subject.id) keys.push(`id:${subject.id}`);
    const sessionId = subject.session?.id;
    if (typeof sessionId === "string" && sessionId) keys.push(`session:${sessionId}`);
    return keys;
  };

  return {
    set(subject, text) {
      const state = { text: typeof text === "string" ? text : "" };
      if (subject && typeof subject === "object") byObject.set(subject, state);
      for (const key of keysFor(subject)) {
        byKey.delete(key);
        byKey.set(key, state);
      }
      while (byKey.size > RECALL_STATES) byKey.delete(byKey.keys().next().value);
    },
    textFor(...subjects) {
      for (const subject of subjects) {
        if (!subject || typeof subject !== "object") continue;
        const direct = byObject.get(subject);
        if (direct) return direct.text;
        for (const key of keysFor(subject)) {
          const state = byKey.get(key);
          if (state) return state.text;
        }
      }
      return "";
    },
  };
}

function recallContextSpec(cache) {
  return {
    name: CONTEXT_NAME,
    order: CONTEXT_ORDER,
    // Recall text is opaque payload: never interpolate harness variables.
    interpolate: false,
    text: (context) => cache.textFor(context?.agent, context?.scope),
  };
}

/**
 * Registry gate.  Registration happens while the plugin is applied — that is,
 * before the first `systemPrompt.assemble()` — and is retried on the first step
 * in case the profile resolved `systemPrompt` later.  If the host has no
 * registry, recall simply contributes nothing.
 */
function createRecallGate(ctx, cache) {
  let admitted = false;
  let warned = false;
  const ensure = () => {
    if (admitted) return true;
    if (!ctx?.systemPrompt?.context) return false;
    try {
      ctx.systemPrompt.context(recallContextSpec(cache));
      admitted = true;
    } catch (error) {
      if (!warned) {
        warned = true;
        try {
          ctx.logger?.warn?.(`bmem: systemPrompt.context() rejected: ${String(error)}`);
        } catch {
          // Diagnostics must never break the plugin.
        }
      }
    }
    return admitted;
  };
  return { ensure };
}

function listenForRecall(ctx, base, config, cache, gate) {
  ctx.on("agent/pre-step", async (payload, next) => {
    if (gate.ensure()) {
      try {
        if (Number(payload?.step) === 1 && !payload?.signal?.aborted) {
          const agent = payload?.agent;
          const query = latestUserText(payload?.messages);
          if (!query) {
            cache.set(agent, "");
          } else {
            const session = agent?.session;
            const sid = String(session?.id || agent?.id || "dsh");
            const turn = String(payload?.turn ?? "");
            const project = String(
              session?.header?.cwd || session?.meta?.cwd || config?.project || "",
            );
            const generation = session?.surface?.contentGeneration;
            const contextEpoch = turn ? `turn:${turn}` : `surface:${generation ?? "0"}`;
            const result = await postJson(base, "/api/recall", {
              query,
              harness: "dsh",
              session_id: sid,
              turn_id: turn,
              task: "",
              context_epoch: contextEpoch,
              budget: Number(config?.budget ?? 2000),
              target: String(config?.target ?? "cloud"),
              conditions: {},
              project,
            }, 5000);
            const text = typeof result?.text === "string" ? result.text.trim() : "";
            if (result?.deduplicated === true && text) {
              // The server already injected this snapshot: keep the previous
              // text so the loop sees an unchanged runtime context.
              cache.set(agent, cache.textFor(agent) || text);
            } else {
              cache.set(agent, text);
            }
          }
        }
      } catch {
        // Recall degrades to no contribution; capture and tools still work.
      }
    }
    return next();
  });
}

function toolOutput() {
  return {
    schema: { type: "string" },
    render: (_args, value) => String(value ?? ""),
  };
}

const SAFE_MEMORY_KEYS = ["memory_id", "kind", "status", "sensitivity", "trust_tier"];

/**
 * Mirror of `bmem.api._private_cloud_ack`: for the default cloud target a
 * private save result is projected to id/status metadata only, so no private
 * body, conditions, revisions, sources or decisions are echoed to the model.
 */
function privateCloudAck(result) {
  const memory = result?.memory && typeof result.memory === "object" ? result.memory : {};
  const safe = {};
  for (const key of SAFE_MEMORY_KEYS) {
    if (Object.hasOwn(memory, key)) safe[key] = memory[key];
  }
  return {
    memory: safe,
    event_id: result?.event_id ?? null,
    duplicate: result?.duplicate ?? null,
    target: "cloud",
    note: "private memory stored locally; retrieve with target=local",
  };
}

function registerTools(ctx, base, config) {
  ctx.tools.register({
    name: "bmem_recall",
    description: "Recall personal memory relevant to a query with provenance and an injection marker.",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        query: { type: "string", description: "What to recall." },
        budget: { type: "number", description: "Token budget (0..10000)." },
        target: { type: "string", enum: ["local", "cloud"] },
        project: { type: "string", description: "Project relevance hint." },
      },
      required: ["query"],
    },
    output: toolOutput(),
    async execute(args) {
      const result = await postJson(base, "/api/recall", {
        query: String(args.query ?? ""),
        harness: "dsh",
        session_id: String(config?.session_id ?? "dsh-tool"),
        turn_id: "",
        task: "",
        context_epoch: "tool",
        budget: Number(args.budget ?? config?.budget ?? 2000),
        target: String(args.target ?? config?.target ?? "cloud"),
        conditions: {},
        project: String(args.project ?? config?.project ?? ""),
      }, 60_000);
      return String(result?.text ?? "");
    },
  });

  ctx.tools.register({
    name: "bmem_save",
    description: "Save an explicit user declaration as personal memory (T1).",
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {
        body: { type: "string", description: "The memory to save." },
        kind: {
          type: "string",
          enum: ["episode", "fact", "decision", "method", "preference"],
        },
        fact_key: { type: "string", description: "Optional deterministic fact key." },
        sensitivity: { type: "string", enum: ["normal", "private"] },
        target: {
          type: "string",
          enum: ["local", "cloud"],
          description: "cloud (default): private memories are acknowledged minimally; local returns full detail.",
        },
      },
      required: ["body"],
    },
    output: toolOutput(),
    async execute(args) {
      const nativeId = `dsh-tool:${randomUUID()}`;
      const result = await postJson(base, "/api/declarations", {
        body: String(args.body ?? ""),
        harness: "dsh",
        device_id: "local",
        native_event_id: nativeId,
        operation_id: nativeId,
        source_type: "user_declaration",
        occurred_at: new Date().toISOString(),
        session_id: String(config?.session_id ?? "dsh-tool"),
        project: String(config?.project ?? ""),
        sensitivity: String(args.sensitivity ?? "normal"),
        metadata: { origin: "dsh-native-tool", parser_version: "bmem-dsh-plugin-1" },
        kind: String(args.kind ?? "fact"),
        fact_key: typeof args.fact_key === "string" && args.fact_key.trim()
          ? args.fact_key
          : undefined,
        conditions: {},
        derived_from: [],
      }, 60_000);
      // Projection is decided from the persisted result, not the payload: an
      // update of a private memory can be stored private even when args say
      // normal. Fail closed if the result carries no usable sensitivity.
      const target = args.target === "local" ? "local" : "cloud";
      if (target === "local") return JSON.stringify(result);
      const persisted = result?.memory?.sensitivity;
      const sensitivity = typeof persisted === "string" && persisted
        ? persisted
        : String(args.sensitivity ?? "normal");
      if (sensitivity !== "normal") return JSON.stringify(privateCloudAck(result));
      return JSON.stringify(result);
    },
  });
}

export function apply(ctx, config) {
  const base = apiBase(config);
  const cache = createRecallCache();
  listenForCapture(ctx, base, config);
  // Registered while the plugin is applied, i.e. before the first assembly.
  const gate = createRecallGate(ctx, cache);
  gate.ensure();
  listenForRecall(ctx, base, config, cache, gate);
  registerTools(ctx, base, config);
}
