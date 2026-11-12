import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Minus, Users } from "lucide-react";
import { useLocale } from "../../context/LocaleContext";
import type { RunTrace, TraceItem } from "../../lib/workloopLive";

type Agent = "boss" | "worker";

const TEXT = {
  fa: {
    boss: "رئیس", worker: "کارمند", stage: "گفتگوی رئیس و کارمند", minimize: "کوچک کردن", open: "نمایش گفتگو",
    round: (r: number, m: number) => `دور ${r} از ${m}`, approved: "تأیید شد ✓", rejected: "رد شد ✗",
    permission: "منتظر اجازه شما", waiting: "آماده…", done: "کار تمام شد", thinking: "…",
  },
  en: {
    boss: "Boss", worker: "Worker", stage: "Boss and Worker", minimize: "Minimize", open: "Show conversation",
    round: (r: number, m: number) => `Round ${r} of ${m}`, approved: "Approved ✓", rejected: "Rejected ✗",
    permission: "Waiting for your permission", waiting: "Ready…", done: "Work finished", thinking: "…",
  },
};

interface StageState {
  speaker: Agent | null;
  talking: boolean;
  lines: Record<Agent, string>;
  actions: Record<Agent, string>;
  round: { round: number; max: number } | null;
  verdict: "approved" | "rejected" | null;
  waitingPermission: boolean;
}

/** Reduce a run trace to what the two robots show right now. */
function stageFrom(trace: RunTrace): StageState {
  const st: StageState = {
    speaker: null, talking: false, lines: { boss: "", worker: "" }, actions: { boss: "", worker: "" },
    round: null, verdict: null, waitingPermission: false,
  };
  const items: TraceItem[] = trace.items;
  for (const it of items) {
    switch (it.kind) {
      case "round": st.round = { round: it.round, max: it.max }; st.verdict = null; st.lines = { boss: "", worker: "" }; st.actions = { boss: "", worker: "" }; break;
      case "message":
        st.lines[it.agent] = it.text; st.speaker = it.agent; st.talking = !it.done; st.actions[it.agent] = ""; break;
      case "tool":
        st.actions[it.agent] = `🔧 ${it.name}${it.arg ? ` · ${it.arg}` : ""}`; st.speaker = it.agent; st.talking = false; break;
      case "command":
        st.actions.boss = `$ ${it.command}${it.done ? (it.exitCode === 0 ? "  ✓" : "  ✗") : "  …"}`; st.speaker = "boss"; st.talking = false; break;
      case "verdict":
        st.verdict = it.status; st.lines.boss = it.summary; st.speaker = "boss"; st.talking = false; st.actions.boss = ""; break;
      case "permission":
        st.waitingPermission = it.approved === undefined; st.speaker = "boss";
        st.actions.boss = `🛡 ${it.command}`; break;
      default: break;
    }
  }
  return st;
}

const tail = (text: string, n = 150) => {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > n ? `…${clean.slice(-n)}` : clean;
};

export default function AgentStage({ trace, active }: { trace: RunTrace | null; active: boolean }) {
  const { locale } = useLocale();
  const t = TEXT[locale === "fa" ? "fa" : "en"];
  const reduce = useReducedMotion();
  const [minimized, setMinimized] = useState(false);
  const [visible, setVisible] = useState(false);

  // Show while a run is active; keep the result on screen a few seconds after it ends.
  useEffect(() => {
    if (active && trace) { setVisible(true); return; }
    if (!trace) { setVisible(false); return; }
    const timer = window.setTimeout(() => setVisible(false), 6000);
    return () => window.clearTimeout(timer);
  }, [active, trace?.promptId, trace]);

  if (!trace || !visible) return null;
  const st = stageFrom(trace);
  const finished = !active && (trace.status === "done" || trace.status === "failed" || trace.status === "error");

  if (minimized) {
    return (
      <button type="button" onClick={() => setMinimized(false)} aria-label={t.open} title={t.open}
        className={`absolute bottom-5 z-40 flex items-center gap-2 rounded-full glass-pop px-3.5 py-2 text-[14px] font-medium text-white ${locale === "fa" ? "left-5" : "right-5"}`}>
        <Users className="size-4" aria-hidden="true" />
        {st.speaker ? t[st.speaker] : t.stage}
        {active && <span className="size-2 animate-pulse rounded-full bg-emerald-300" aria-hidden="true" />}
      </button>
    );
  }

  return (
    <motion.aside
      aria-label={t.stage}
      aria-live="polite"
      initial={reduce ? false : { opacity: 0, y: 24, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      className={`agent-stage-panel absolute bottom-5 z-40 w-[min(360px,calc(100%-2.5rem))] p-1 text-white [text-shadow:0_1px_3px_rgb(0_0_0/0.6)] ${locale === "fa" ? "left-5" : "right-5"}`}
      dir={locale === "fa" ? "rtl" : "ltr"}
    >
      <div className="mb-2 ms-auto flex w-fit items-center gap-2 rounded-full glass-pop py-0.5 pe-1 ps-3 text-[13px] text-white/60 [text-shadow:none]">
        <span className="font-medium text-white/85">{t.stage}</span>
        {st.round && <span>· {t.round(st.round.round, st.round.max)}</span>}
        <button type="button" onClick={() => setMinimized(true)} aria-label={t.minimize} title={t.minimize}
          className="ms-auto grid size-7 cursor-pointer place-items-center rounded-full text-white/60 hover:bg-white/10 hover:text-white">
          <Minus className="size-4" aria-hidden="true" />
        </button>
      </div>

      <div className="flex flex-col gap-3">
        {(["boss", "worker"] as Agent[]).map((agent) => {
          const speaking = st.speaker === agent;
          const line = st.lines[agent];
          const action = st.actions[agent];
          return (
            <div key={agent} className={`flex items-end gap-3 ${agent === "worker" ? "flex-row-reverse" : ""}`}>
              <div className="flex shrink-0 flex-col items-center gap-1">
                <Robot agent={agent} active={speaking && !finished} talking={speaking && st.talking} />
                <span className={`text-[12.5px] font-medium ${agent === "boss" ? "text-amber-200" : "text-sky-200"}`}>{t[agent]}</span>
              </div>
              <AnimatePresence mode="popLayout">
                {(line || action || (agent === "boss" && (st.verdict || st.waitingPermission))) && (
                  <motion.div
                    key={`${agent}-${st.round?.round ?? 0}`}
                    initial={reduce ? false : { opacity: 0, y: 8 }}
                    animate={{ opacity: speaking ? 1 : 0.55, y: 0 }}
                    exit={{ opacity: 0 }}
                    className={`relative min-w-0 flex-1 rounded-2xl glass-pop px-3.5 py-2.5 text-[14.5px] leading-6 [text-shadow:none] ${
                      agent === "boss" ? "rounded-es-md border-s-2 !border-s-amber-300/80" : "rounded-ee-md border-e-2 !border-e-sky-300/80"
                    } ${speaking ? "ring-1 ring-white/20" : ""}`}
                  >
                    {agent === "boss" && st.verdict && (
                      <p className={`m-0 mb-1 text-[13.5px] font-semibold ${st.verdict === "approved" ? "text-emerald-300" : "text-red-300"}`}>
                        {st.verdict === "approved" ? t.approved : t.rejected}
                      </p>
                    )}
                    {agent === "boss" && st.waitingPermission && (
                      <p className="m-0 mb-1 text-[13.5px] font-semibold text-amber-200">{t.permission}</p>
                    )}
                    {line && <p dir="auto" className="m-0 break-words">{tail(line)}{speaking && st.talking && <span className="animate-pulse">▍</span>}</p>}
                    {action && <p dir="ltr" className="m-0 mt-1 truncate text-start font-mono text-[12.5px] text-white/55">{action}</p>}
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          );
        })}
      </div>

      {finished && trace.status === "done" && <p className="m-0 mt-3 text-center text-[13.5px] text-emerald-300">{t.done}</p>}
    </motion.aside>
  );
}

/** Small original robot drawing; bobs while active, mouth moves while talking. */
export function Robot({ agent, active, talking, size = 56 }: { agent: Agent; active: boolean; talking: boolean; size?: number }) {
  const reduce = useReducedMotion();
  const boss = agent === "boss";
  const body = boss ? "#f5c26b" : "#7cc4f5";
  const shade = boss ? "#d99a2b" : "#3f97d6";
  return (
    <motion.svg
      width={size} height={Math.round(size * 60 / 56)} viewBox="0 0 56 60" aria-hidden="true"
      animate={reduce || !active ? { y: 0 } : { y: [0, -3, 0] }}
      transition={reduce || !active ? undefined : { duration: 1.1, repeat: Infinity, ease: "easeInOut" }}
      style={{ filter: active ? `drop-shadow(0 0 10px ${body}88)` : "none", opacity: active ? 1 : 0.75 }}
    >
      {/* antenna */}
      <line x1="28" y1="4" x2="28" y2="12" stroke={shade} strokeWidth="2.5" strokeLinecap="round" />
      <motion.circle cx="28" cy="4" r="3" fill={active ? "#fff" : shade}
        animate={reduce || !active ? undefined : { opacity: [1, 0.3, 1] }} transition={{ duration: 0.9, repeat: Infinity }} />
      {/* head */}
      <rect x="8" y="12" width="40" height="30" rx="11" fill={body} />
      <rect x="13" y="18" width="30" height="18" rx="7" fill="#1d1b22" />
      {/* eyes (blink) */}
      <motion.g animate={reduce ? undefined : { scaleY: [1, 1, 0.1, 1] }} transition={{ duration: 3.4, repeat: Infinity, times: [0, 0.9, 0.95, 1] }}
        style={{ originY: "25px" }}>
        <circle cx="21.5" cy="25" r="3" fill={body} />
        <circle cx="34.5" cy="25" r="3" fill={body} />
      </motion.g>
      {/* mouth */}
      <motion.rect x="23" y="30.5" width="10" rx="1.5" fill={body}
        animate={talking && !reduce ? { height: [1.5, 4, 1.5, 3, 1.5], y: [30.5, 29.5, 30.5, 30, 30.5] } : { height: 1.5, y: 30.5 }}
        transition={talking && !reduce ? { duration: 0.5, repeat: Infinity } : { duration: 0.2 }} />
      {/* ears */}
      <rect x="4" y="22" width="4" height="10" rx="2" fill={shade} />
      <rect x="48" y="22" width="4" height="10" rx="2" fill={shade} />
      {/* body */}
      <rect x="15" y="44" width="26" height="14" rx="6" fill={shade} />
      {boss ? (
        <>
          {/* crown */}
          <path d="M18 13 L21 6 L25 11 L28 5 L31 11 L35 6 L38 13 Z" fill="#ffe08a" stroke={shade} strokeWidth="1" />
          {/* tie */}
          <path d="M28 44 L25 48 L28 56 L31 48 Z" fill="#b8452c" />
        </>
      ) : (
        <>
          {/* hard hat */}
          <path d="M12 16 Q28 2 44 16 Z" fill="#ffd34d" />
          <rect x="10" y="15" width="36" height="3" rx="1.5" fill="#e6b52e" />
          {/* tool belt */}
          <rect x="15" y="50" width="26" height="3" fill="#2b5c80" />
        </>
      )}
    </motion.svg>
  );
}
