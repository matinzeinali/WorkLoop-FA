// Demo mode: plays a scripted Boss/Worker run in the browser, through the same
// events the real backend sends, so the chat looks and behaves like a real run.
// Nothing is sent to the backend and no files are touched.

import type { LiveEvent } from "./workloopLive";

const KEY = "workloop:demo";
export const isDemo = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
export const setDemo = (on: boolean) => {
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch { /* storage unavailable */ }
  window.dispatchEvent(new Event("workloop:demo"));
};

export interface DemoHooks {
  emit: (event: LiveEvent) => void;
  /** Resolves with the user's answer when the scripted permission request is answered. */
  askPermission: (permissionId: number) => Promise<boolean>;
  signal: AbortSignal;
}

let nextId = 1;
const id = () => nextId++;

class Stopped extends Error {}

export async function runDemo(promptId: number, request: string, hooks: DemoHooks): Promise<void> {
  const { emit, signal } = hooks;
  const sleep = (ms: number) => new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(t); reject(new Stopped()); }, { once: true });
  });
  const ev = (type: string, data: Record<string, unknown>) => emit({ type, data: { prompt_id: promptId, ...data } });

  // Stream a message in small chunks, like LLM tokens.
  const say = async (agent: "boss" | "worker", kind: string, round: number, text: string) => {
    const messageId = id();
    ev("message_start", { message_id: messageId, round, agent, kind });
    const words = text.split(/(\s+)/);
    for (let i = 0; i < words.length; i += 3) {
      ev("token", { message_id: messageId, text: words.slice(i, i + 3).join("") });
      await sleep(45);
    }
    ev("message_end", { message_id: messageId, content_length: text.length });
    await sleep(350);
  };
  const tool = async (agent: "boss" | "worker", name: string, args: Record<string, unknown>, ok = true, summary = "") => {
    const callId = `demo-${id()}`;
    ev("tool_call", { call_id: callId, message_id: null, agent, name, arguments: args });
    await sleep(380);
    ev("tool_result", { call_id: callId, agent, name, ok, summary });
  };
  const command = async (cmd: string, reason: string, output: string, exitCode: number, ms: number) => {
    const commandId = id();
    ev("command_started", { command_id: commandId, command: cmd, cwd: ".", reason });
    for (const line of output.split("\n")) {
      ev("command_output", { command_id: commandId, stream: exitCode === 0 ? "stdout" : "stderr", text: `${line}\n` });
      await sleep(120);
    }
    ev("command_finished", { command_id: commandId, exit_code: exitCode, duration_ms: ms, killed: false, timed_out: false });
    await sleep(300);
  };

  const max = 6;
  const topic = request.length > 60 ? `${request.slice(0, 57)}…` : request;

  try {
    ev("prompt_status", { status: "running", rounds_used: 0 });
    await sleep(400);

    // ---- Round 1 ----
    ev("round_started", { round: 1, max_rounds: max });
    await tool("boss", "list_files", { path: "." });
    await tool("boss", "read_file", { path: "README.md" });
    await say("boss", "task", 1,
      `درخواست: «${topic}»\n\nبرنامه:\n۱. ساختار فعلی پروژه را بررسی کن.\n۲. تغییر خواسته‌شده را در فایل‌های مربوط پیاده کن.\n۳. برای بخش جدید تست بنویس.\n\nمعیار پذیرش: همه تست‌ها سبز باشند و کد قبلی خراب نشود.`);
    await tool("worker", "read_file", { path: "src/app.py" });
    await tool("worker", "write_file", { path: "src/feature.py" });
    await tool("worker", "write_file", { path: "tests/test_feature.py" });
    await say("worker", "report", 1,
      "انجام شد. فایل src/feature.py را اضافه کردم و در src/app.py صدایش زدم. سه تست هم در tests/test_feature.py نوشتم.");
    await command("pytest -q", "اجرای تست‌ها برای بررسی کار کارمند",
      "..F\nFAILED tests/test_feature.py::test_empty_input - TypeError: 'NoneType' object is not iterable\n1 failed, 2 passed in 0.41s", 1, 1240);
    ev("verdict", {
      round: 1, status: "rejected",
      summary: "دو تست قبول شد ولی ورودی خالی باعث خطا می‌شود.",
      bugs: ["وقتی ورودی خالی است، تابع None برمی‌گرداند و برنامه می‌شکند."],
      fix_instructions: ["برای ورودی خالی یک لیست خالی برگردان و تست مربوط را دوباره اجرا کن."],
    });
    await sleep(700);

    // ---- Round 2 ----
    ev("round_started", { round: 2, max_rounds: max });
    ev("prompt_status", { status: "running", rounds_used: 1 });
    await tool("worker", "read_file", { path: "src/feature.py" });
    await tool("worker", "edit_file", { path: "src/feature.py" });
    await say("worker", "report", 2, "باگ ورودی خالی را درست کردم: حالا به‌جای None یک لیست خالی برمی‌گردد.");

    // A risky command that needs the user's permission.
    const permissionId = id();
    ev("permission_request", {
      permission_id: permissionId,
      command: "pip install -r requirements.txt",
      reason: "نصب وابستگی‌ها قبل از اجرای کامل تست‌ها",
      danger_reason: "پکیج‌ها را در محیط پایتون شما نصب می‌کند.",
    });
    const approved = await Promise.race([
      hooks.askPermission(permissionId),
      new Promise<boolean>((_, reject) => signal.addEventListener("abort", () => reject(new Stopped()), { once: true })),
    ]);
    ev("permission_resolved", { permission_id: permissionId, approved });
    await sleep(300);
    if (approved) {
      await command("pip install -r requirements.txt", "نصب وابستگی‌ها",
        "Requirement already satisfied: pytest in ./.venv\nSuccessfully installed 0 packages", 0, 2100);
    }

    await command("pytest -q", "اجرای دوباره تست‌ها", "...\n3 passed in 0.38s", 0, 1180);
    ev("verdict", {
      round: 2, status: "approved",
      summary: approved
        ? "همه تست‌ها قبول شد و رفتار قبلی برنامه دست نخورده. کار تأیید است."
        : "بدون نصب دوباره وابستگی‌ها هم همه تست‌ها قبول شد. کار تأیید است.",
      bugs: [], fix_instructions: [],
    });
    ev("prompt_status", { status: "done", rounds_used: 2 });
    ev("prompt_done", { rounds_used: 2 });
  } catch (e) {
    if (!(e instanceof Stopped)) throw e;
    ev("error", { code: "STOPPED", message: "دمو متوقف شد.", details: null });
    ev("prompt_status", { status: "failed", rounds_used: 0 });
  }
}
