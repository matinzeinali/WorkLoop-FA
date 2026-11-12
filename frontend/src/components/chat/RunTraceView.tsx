import { useState } from "react";
import { Check, ChevronDown, ShieldAlert, Terminal, Wrench, X } from "lucide-react";
import { useLocale } from "../../context/LocaleContext";
import type { RunTrace, TraceItem } from "../../lib/workloopLive";
import { Robot } from "./AgentStage";

export interface RunActions {
  /** prompt_id of the prompt the backend is running now, if any */
  currentPromptId: number | null;
  runActive: boolean;
  onPermission: (id: number, approved: boolean) => Promise<void>;
  onFixPrompt: (text: string) => Promise<void>;
  onManualFix: (note: string) => Promise<void>;
  onStop: () => Promise<void>;
}

const T = {
  fa: {
    boss: "رئیس", worker: "کارمند", round: (r: number, m: number) => `دور ${r} از ${m}`,
    status: { pending: "در صف", running: "در حال کار", done: "انجام شد", failed: "تأیید نشد", manual_fix: "اصلاح دستی", error: "خطا" },
    kind: { task: "وظیفه", report: "گزارش", verdict: "رأی", thinking: "فکر", system: "سیستم" } as Record<string, string>,
    approved: "رئیس تأیید کرد", rejected: "رئیس رد کرد", bugs: "باگ‌ها", fixes: "دستور اصلاح",
    permTitle: "رئیس می‌خواهد یک دستور خطرناک اجرا کند", allow: "اجازه بده", deny: "رد کن", allowed: "اجازه داده شد", denied: "رد شد",
    needsTitle: (n: number) => `بعد از ${n} دور تأیید نشد. چه کنیم؟`,
    fixPh: "پرامپت اصلاحی: بگویید چه چیزی را درست کنند…", sendFix: "ارسال اصلاحیه و ادامه",
    manual: "خودم دستی درستش کردم", stop: "توقف اجرا", tools: (n: number) => `${n} ابزار`, waiting: "منتظر شروع…",
    exit: (c: number | null | undefined) => c === 0 ? "موفق" : `کد ${c ?? "?"}`, timeout: "پایان زمان", killed: "متوقف شد",
  },
  en: {
    boss: "Boss", worker: "Worker", round: (r: number, m: number) => `Round ${r} of ${m}`,
    status: { pending: "Queued", running: "Working", done: "Done", failed: "Not approved", manual_fix: "Fixed by hand", error: "Error" },
    kind: { task: "Task", report: "Report", verdict: "Verdict", thinking: "Thinking", system: "System" } as Record<string, string>,
    approved: "Boss approved", rejected: "Boss rejected", bugs: "Bugs", fixes: "Fix instructions",
    permTitle: "The Boss wants to run a risky command", allow: "Allow", deny: "Deny", allowed: "Allowed", denied: "Denied",
    needsTitle: (n: number) => `Not approved after ${n} rounds. What next?`,
    fixPh: "Fix prompt: tell them what to correct…", sendFix: "Send fix and continue",
    manual: "I fixed it by hand", stop: "Stop run", tools: (n: number) => `${n} tools`, waiting: "Waiting to start…",
    exit: (c: number | null | undefined) => c === 0 ? "ok" : `exit ${c ?? "?"}`, timeout: "timed out", killed: "stopped",
  },
};
type Text = (typeof T)["fa"];

const statusTone: Record<RunTrace["status"], string> = {
  pending: "bg-white/10 text-white/70", running: "bg-sky-400/15 text-sky-200", done: "bg-emerald-400/15 text-emerald-300",
  failed: "bg-amber-400/15 text-amber-200", manual_fix: "bg-amber-400/15 text-amber-200", error: "bg-red-400/15 text-red-300",
};

// Consecutive tool calls are folded into one expandable line.
type Block = TraceItem | { kind: "tools"; items: Extract<TraceItem, { kind: "tool" }>[] };
function group(items: TraceItem[]): Block[] {
  const out: Block[] = [];
  for (const it of items) {
    const last = out[out.length - 1];
    if (it.kind === "tool") {
      if (last?.kind === "tools") last.items.push(it); else out.push({ kind: "tools", items: [it] });
    } else out.push(it);
  }
  return out;
}

export default function RunTraceView({ trace, actions }: { trace: RunTrace; actions?: RunActions }) {
  const { locale } = useLocale();
  const t = T[locale === "fa" ? "fa" : "en"];
  const isCurrent = actions?.currentPromptId === trace.promptId;
  const busy = trace.status === "running" || trace.status === "pending";

  return (
    <div className="flex min-w-0 flex-col gap-2.5 pt-1 text-[16px] leading-7 text-white/90 lg:text-[17px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2.5 py-0.5 text-[13.5px] font-medium ${statusTone[trace.status]}`}>{t.status[trace.status]}</span>
        {trace.promptId > 0 && <span className="text-[13.5px] text-white/40">#{trace.promptId}</span>}
      </div>

      {!trace.items.length && busy && <p className="m-0 text-[14.5px] text-white/45">{t.waiting}</p>}

      {(() => {
        const blocks = group(trace.items);
        return blocks.map((b, i) => {
          const owner = ownerOf(b);
          const prev = i > 0 ? ownerOf(blocks[i - 1]) : null;
          return <BlockView key={i} block={b} t={t} actions={isCurrent ? actions : undefined} showAvatar={owner !== prev} running={busy} />;
        });
      })()}

      {trace.needsUser && isCurrent && actions && (
        <NeedsUser t={t} rounds={trace.roundsUsed} summary={trace.needsUser.summary} bugs={trace.needsUser.bugs} actions={actions} />
      )}
    </div>
  );
}

/** Who a block belongs to; tools and commands are drawn under that speaker. */
function ownerOf(block: Block): "boss" | "worker" | null {
  switch (block.kind) {
    case "message": return block.agent;
    case "tools": return block.items[0]?.agent ?? null;
    case "command": case "verdict": case "permission": return "boss";
    default: return null;
  }
}

/** One line of the dialogue: robot avatar (only on the first block of a speaker's turn) + content. */
function AgentRow({ agent, showAvatar, active, talking, label, children }: {
  agent: "boss" | "worker"; showAvatar: boolean; active: boolean; talking: boolean; label: string; children: React.ReactNode;
}) {
  const worker = agent === "worker";
  return (
    <div className={`flex items-start gap-3 ${worker ? "flex-row-reverse" : ""}`} dir="ltr">
      <div className="flex w-14 shrink-0 flex-col items-center">
        {showAvatar && (
          <>
            <Robot agent={agent} active={active} talking={talking} size={44} />
            <span className={`text-[12.5px] font-medium ${worker ? "text-sky-200" : "text-amber-200"}`}>{label}</span>
          </>
        )}
      </div>
      <div className={`flex min-w-0 max-w-[82%] flex-1 flex-col gap-2 ${worker ? "items-end" : "items-start"}`}>{children}</div>
    </div>
  );
}

function bubbleClass(agent: "boss" | "worker") {
  return `w-fit max-w-full rounded-[20px] px-4 py-2.5 ${
    agent === "boss" ? "rounded-tl-md bg-amber-300/[0.10] ring-1 ring-amber-300/20" : "rounded-tr-md bg-sky-300/[0.10] ring-1 ring-sky-300/20"
  }`;
}

function BlockView({ block, t, actions, showAvatar, running }: { block: Block; t: Text; actions?: RunActions; showAvatar: boolean; running: boolean }) {
  const owner = ownerOf(block);
  const content = (() => {
    switch (block.kind) {
      case "round":
        return <div className="my-1 flex items-center gap-3 text-[13.5px] text-white/40"><span className="h-px flex-1 bg-white/10" />{t.round(block.round, block.max)}<span className="h-px flex-1 bg-white/10" /></div>;
      case "message":
        return (
          <div className={bubbleClass(block.agent)} dir="auto">
            <p className="m-0 mb-0.5 text-[12.5px] text-white/45">{t.kind[block.msgKind] ?? block.msgKind}</p>
            <p dir="auto" className="m-0 whitespace-pre-wrap break-words">{block.text}{!block.done && <span className="animate-pulse">▍</span>}</p>
          </div>
        );
      case "tools":
        return <div className="w-full"><ToolGroup items={block.items} t={t} /></div>;
      case "command":
        return <div className="w-full"><CommandView c={block} t={t} /></div>;
      case "verdict":
        return (
          <div className={`w-fit max-w-full rounded-[20px] rounded-tl-md px-4 py-2.5 ring-1 ${block.status === "approved" ? "bg-emerald-400/10 ring-emerald-300/25" : "bg-red-400/10 ring-red-300/25"}`} dir="auto">
            <p className={`m-0 flex items-center gap-1.5 text-[14.5px] font-semibold ${block.status === "approved" ? "text-emerald-300" : "text-red-300"}`}>
              {block.status === "approved" ? <Check className="size-4" aria-hidden="true" /> : <X className="size-4" aria-hidden="true" />}
              {block.status === "approved" ? t.approved : t.rejected}
            </p>
            {block.summary && <p dir="auto" className="m-0 mt-1 whitespace-pre-wrap">{block.summary}</p>}
            {block.bugs.length > 0 && <List title={t.bugs} items={block.bugs} />}
            {block.fixes.length > 0 && <List title={t.fixes} items={block.fixes} />}
          </div>
        );
      case "permission":
        return <div className="w-full" dir="auto"><PermissionView p={block} t={t} actions={actions} /></div>;
      case "note":
        return <p className={`m-0 text-center text-[14.5px] ${block.tone === "error" ? "text-red-300" : block.tone === "ok" ? "text-emerald-300" : "text-white/60"}`}>{block.text}</p>;
    }
  })();
  if (!owner) return content;
  const talking = block.kind === "message" && !block.done;
  const active = running && (talking || (block.kind === "permission" && block.approved === undefined));
  return <AgentRow agent={owner} showAvatar={showAvatar} active={active} talking={talking} label={t[owner]}>{content}</AgentRow>;
}

function List({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="mt-1.5">
      <p className="m-0 text-[13.5px] font-medium text-white/60">{title}</p>
      <ul className="m-0 ps-5">{items.map((x, i) => <li key={i} dir="auto" className="text-[15px]">{x}</li>)}</ul>
    </div>
  );
}

function ToolGroup({ items, t }: { items: Extract<TraceItem, { kind: "tool" }>[]; t: Text }) {
  const [open, setOpen] = useState(false);
  const failed = items.filter((i) => i.ok === false).length;
  const last = items[items.length - 1];
  return (
    <div className="rounded-xl bg-white/[0.03] text-[14px]">
      <button type="button" onClick={() => setOpen((o) => !o)} className="flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-start text-white/55 hover:text-white/80">
        <Wrench className="size-4 shrink-0" aria-hidden="true" />
        <span>{t.tools(items.length)}</span>
        {failed > 0 && <span className="text-red-300">✗ {failed}</span>}
        {!open && <code dir="ltr" className="min-w-0 truncate text-white/40">{last.name} {last.arg}</code>}
        <ChevronDown className={`ms-auto size-3.5 shrink-0 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <ul className="m-0 list-none px-3 pb-2" dir="ltr">
          {items.map((i) => (
            <li key={i.callId} className="py-0.5 text-start">
              <span className={i.ok === false ? "text-red-300" : i.ok ? "text-emerald-300" : "text-white/40"}>{i.ok === false ? "✗" : i.ok ? "✓" : "…"}</span>{" "}
              <span className="text-white/75">{i.name}</span> <span className="text-white/45">{i.arg}</span>
              {i.ok === false && i.summary && <pre className="m-0 whitespace-pre-wrap text-[13px] text-red-200/80">{i.summary}</pre>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CommandView({ c, t }: { c: Extract<TraceItem, { kind: "command" }>; t: Text }) {
  const [open, setOpen] = useState(false);
  const status = !c.done ? "…" : c.timedOut ? t.timeout : c.killed ? t.killed : t.exit(c.exitCode);
  const ok = c.done && c.exitCode === 0 && !c.timedOut && !c.killed;
  return (
    <div className="light-code-surface overflow-hidden rounded-xl border border-white/10 bg-black/35 text-[14px]">
      <button type="button" onClick={() => setOpen((o) => !o)} className="flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-start" dir="ltr">
        <Terminal className="size-4 shrink-0 text-white/50" aria-hidden="true" />
        <code className="min-w-0 flex-1 truncate text-emerald-200/90">{c.command}</code>
        <span className={`shrink-0 ${!c.done ? "animate-pulse text-white/50" : ok ? "text-emerald-300" : "text-red-300"}`}>{status}</span>
        {c.durationMs != null && <span className="shrink-0 text-white/35">{(c.durationMs / 1000).toFixed(1)}s</span>}
        {c.output && <ChevronDown className={`size-3.5 shrink-0 text-white/40 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />}
      </button>
      {open && c.output && <pre dir="ltr" className="thin-scroll m-0 max-h-64 overflow-auto whitespace-pre-wrap border-t border-white/10 px-3 py-2 text-start text-[13px] text-white/75">{c.output}</pre>}
    </div>
  );
}

function PermissionView({ p, t, actions }: { p: Extract<TraceItem, { kind: "permission" }>; t: Text; actions?: RunActions }) {
  const [busy, setBusy] = useState(false);
  const answer = async (ok: boolean) => { if (!actions) return; setBusy(true); try { await actions.onPermission(p.id, ok); } finally { setBusy(false); } };
  return (
    <div className="rounded-2xl border border-amber-300/30 bg-amber-300/10 px-3.5 py-2.5">
      <p className="m-0 flex items-center gap-1.5 text-[14.5px] font-semibold text-amber-200"><ShieldAlert className="size-4" aria-hidden="true" />{t.permTitle}</p>
      <code dir="ltr" className="light-code-surface mt-1.5 block whitespace-pre-wrap rounded-lg bg-black/35 px-2.5 py-1.5 text-start text-[14px] text-white">{p.command}</code>
      {p.reason && <p dir="auto" className="m-0 mt-1.5 text-[14.5px] text-white/75">{p.reason}</p>}
      {p.danger && <p dir="auto" className="m-0 text-[14.5px] text-red-300">{p.danger}</p>}
      {p.approved === undefined ? (
        actions && (
          <div className="mt-2 flex gap-2">
            <button type="button" disabled={busy} onClick={() => void answer(true)} className="cursor-pointer rounded-xl bg-white/15 px-3 py-1 text-[14.5px] font-medium hover:bg-white/25 disabled:opacity-50">{t.allow}</button>
            <button type="button" disabled={busy} onClick={() => void answer(false)} className="cursor-pointer rounded-xl bg-red-500/20 px-3 py-1 text-[14.5px] font-medium text-red-200 hover:bg-red-500/30 disabled:opacity-50">{t.deny}</button>
          </div>
        )
      ) : <p className={`m-0 mt-1 text-[14px] ${p.approved ? "text-emerald-300" : "text-red-300"}`}>{p.approved ? t.allowed : t.denied}</p>}
    </div>
  );
}

function NeedsUser({ t, rounds, summary, bugs, actions }: { t: Text; rounds: number; summary: string; bugs: string[]; actions: RunActions }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => { setBusy(true); try { await fn(); } finally { setBusy(false); } };
  return (
    <div className="rounded-2xl border border-amber-300/30 bg-amber-300/[0.07] p-3.5">
      <p className="m-0 text-[16px] font-semibold text-amber-100">{t.needsTitle(rounds)}</p>
      {summary && <p dir="auto" className="m-0 mt-1 whitespace-pre-wrap text-[15px] text-white/80">{summary}</p>}
      {bugs.length > 0 && <List title={t.bugs} items={bugs} />}
      <textarea dir="auto" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={t.fixPh}
        className="mt-2.5 w-full resize-y rounded-xl border border-white/10 bg-white/10 px-3 py-2 text-[16px] text-white outline-none placeholder:text-white/40 focus:border-white/30" />
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" disabled={busy || !text.trim()} onClick={() => void run(() => actions.onFixPrompt(text.trim()))}
          className="cursor-pointer rounded-xl bg-glow px-3.5 py-1.5 text-[14.5px] font-medium text-white hover:brightness-125 disabled:cursor-not-allowed disabled:opacity-45">{t.sendFix}</button>
        <button type="button" disabled={busy} onClick={() => void run(() => actions.onManualFix(text.trim()))}
          className="cursor-pointer rounded-xl bg-white/10 px-3.5 py-1.5 text-[14.5px] font-medium hover:bg-white/20 disabled:opacity-45">{t.manual}</button>
        <button type="button" disabled={busy} onClick={() => void run(actions.onStop)}
          className="cursor-pointer rounded-xl px-3.5 py-1.5 text-[14.5px] font-medium text-red-300 hover:bg-red-500/15 disabled:opacity-45">{t.stop}</button>
      </div>
    </div>
  );
}
