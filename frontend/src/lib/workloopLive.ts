// Live connection to the WorkLoop backend: one shared WebSocket, plus the run trace
// model that turns backend events into what a chat reply shows.

export type RunStateName = "idle" | "running" | "waiting_permission" | "needs_user" | "stopping";
export interface RunState { state: RunStateName; run_id: number | null; prompt_id: number | null; round: number | null; max_rounds: number }
export interface LiveEvent { type: string; data: any } // eslint-disable-line @typescript-eslint/no-explicit-any

type Listener = (event: LiveEvent) => void;
const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let retry = 0;
let connected = false;

function wsUrl() {
  // In dev, Vite serves the page; the backend is on 8000. /ws is not proxied, so connect directly.
  const host = location.hostname === "localhost" ? "localhost" : "127.0.0.1";
  return `ws://${host}:8000/ws`;
}

function emit(event: LiveEvent) { listeners.forEach((l) => l(event)); }

function connect() {
  try { socket = new WebSocket(wsUrl()); } catch { return reconnect(); }
  socket.onopen = () => { retry = 0; connected = true; emit({ type: "_connection", data: { connected: true } }); };
  socket.onclose = () => { connected = false; emit({ type: "_connection", data: { connected: false } }); reconnect(); };
  socket.onmessage = (m) => {
    let ev: LiveEvent;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "ping") { socket?.send(JSON.stringify({ type: "pong" })); return; }
    if (ev.type === "pong") return;
    emit(ev);
  };
}
function reconnect() { setTimeout(connect, Math.min(10000, 700 * 2 ** retry++)); }

export function subscribeLive(listener: Listener) {
  listeners.add(listener);
  if (!socket) connect();
  listener({ type: "_connection", data: { connected } });
  return () => { listeners.delete(listener); };
}

// ---------- run trace (stored on the assistant message) ----------

export type TraceItem =
  | { kind: "round"; round: number; max: number }
  | { kind: "message"; id: number; agent: "boss" | "worker"; msgKind: string; text: string; done: boolean }
  | { kind: "tool"; callId: string; agent: "boss" | "worker"; name: string; arg: string; ok?: boolean; summary?: string }
  | { kind: "command"; id: number; command: string; reason: string; output: string; exitCode?: number | null; durationMs?: number; killed?: boolean; timedOut?: boolean; done: boolean }
  | { kind: "verdict"; round: number; status: "approved" | "rejected"; summary: string; bugs: string[]; fixes: string[] }
  | { kind: "permission"; id: number; command: string; reason: string; danger: string; approved?: boolean }
  | { kind: "note"; tone: "info" | "error" | "ok"; text: string };

export interface RunTrace {
  promptId: number;
  status: "pending" | "running" | "done" | "failed" | "manual_fix" | "error";
  roundsUsed: number;
  items: TraceItem[];
  needsUser?: { summary: string; bugs: string[] };
}

const asText = (x: unknown) => typeof x === "string" ? x : (x as { description?: string; title?: string })?.description ?? (x as { title?: string })?.title ?? JSON.stringify(x);
const argPreview = (a: Record<string, unknown> | undefined) => {
  if (!a) return "";
  const v = a.path ?? a.command ?? a.pattern ?? a.query ?? Object.values(a).find((x) => typeof x === "string") ?? "";
  const s = String(v);
  return s.length > 90 ? `${s.slice(0, 87)}…` : s;
};

/** Apply one backend event to a trace. Returns the same object when nothing changed. */
export function applyEvent(trace: RunTrace, ev: LiveEvent): RunTrace {
  const d = ev.data;
  const items = trace.items;
  const update = (pred: (i: TraceItem) => boolean, fn: (i: any) => TraceItem) => { // eslint-disable-line @typescript-eslint/no-explicit-any
    const idx = items.findIndex(pred);
    if (idx < 0) return trace;
    const next = items.slice(); next[idx] = fn(items[idx]);
    return { ...trace, items: next };
  };
  const push = (item: TraceItem) => ({ ...trace, items: [...items, item] });

  switch (ev.type) {
    case "prompt_status":
      return { ...trace, status: d.status, roundsUsed: d.rounds_used, ...(d.status !== "failed" ? { needsUser: undefined } : {}) };
    case "round_started":
      return push({ kind: "round", round: d.round, max: d.max_rounds });
    case "message_start":
      return push({ kind: "message", id: d.message_id, agent: d.agent, msgKind: d.kind, text: "", done: false });
    case "token":
      return update((i) => i.kind === "message" && i.id === d.message_id, (i) => ({ ...i, text: i.text + d.text }));
    case "message_end": {
      const next = update((i) => i.kind === "message" && i.id === d.message_id, (i) => ({ ...i, done: true }));
      return { ...next, items: next.items.filter((i) => !(i.kind === "message" && i.id === d.message_id && !i.text.trim())) };
    }
    case "tool_call":
      return push({ kind: "tool", callId: d.call_id, agent: d.agent, name: d.name, arg: argPreview(d.arguments) });
    case "tool_result":
      return update((i) => i.kind === "tool" && i.callId === d.call_id, (i) => ({ ...i, ok: d.ok, summary: d.summary }));
    case "command_started":
      return push({ kind: "command", id: d.command_id, command: d.command, reason: d.reason, output: "", done: false });
    case "command_output":
      return update((i) => i.kind === "command" && i.id === d.command_id, (i) => ({ ...i, output: (i.output + d.text).slice(-20000) }));
    case "command_finished":
      return update((i) => i.kind === "command" && i.id === d.command_id,
        (i) => ({ ...i, done: true, exitCode: d.exit_code, durationMs: d.duration_ms, killed: d.killed, timedOut: d.timed_out }));
    case "verdict":
      return push({ kind: "verdict", round: d.round, status: d.status, summary: d.summary, bugs: (d.bugs ?? []).map(asText), fixes: (d.fix_instructions ?? []).map(asText) });
    case "permission_request":
      return push({ kind: "permission", id: d.permission_id, command: d.command, reason: d.reason, danger: d.danger_reason });
    case "permission_resolved":
      return update((i) => i.kind === "permission" && i.id === d.permission_id, (i) => ({ ...i, approved: d.approved }));
    case "needs_user":
      return { ...trace, status: "failed", roundsUsed: d.rounds_used, needsUser: { summary: d.summary, bugs: (d.bugs ?? []).map(asText) } };
    case "manual_fix_applied":
      return { ...push({ kind: "note", tone: "info", text: `+${d.added.length} / -${d.removed.length} / ~${d.modified.length}` }), needsUser: undefined };
    case "error":
      return push({ kind: "note", tone: "error", text: d.message });
    default:
      return trace;
  }
}

/** Events that carry a prompt_id; others belong to the prompt that is running now. */
export function eventPromptId(ev: LiveEvent, current: number | null): number | null {
  return typeof ev.data?.prompt_id === "number" ? ev.data.prompt_id : current;
}

/** Plain-text version of a trace, for copy / share / export. */
export function traceToText(trace: RunTrace): string {
  return trace.items.map((i) => {
    switch (i.kind) {
      case "round": return `— Round ${i.round}/${i.max} —`;
      case "message": return `[${i.agent}] ${i.text}`;
      case "tool": return `  · ${i.name} ${i.arg}`;
      case "command": return `$ ${i.command}${i.exitCode != null ? ` (exit ${i.exitCode})` : ""}`;
      case "verdict": return `Verdict: ${i.status} — ${i.summary}`;
      case "permission": return `Permission: ${i.command}`;
      case "note": return i.text;
    }
  }).join("\n");
}
