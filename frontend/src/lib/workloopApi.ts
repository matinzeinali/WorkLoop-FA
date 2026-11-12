// Client for the WorkLoop backend. In dev, Vite proxies /api to http://127.0.0.1:8000.

export interface Provider {
  id: string;
  name: string;
  requires_base_url: boolean;
  default_base_url: string | null;
  example_models: string[];
}

export interface Profile {
  id: number;
  name: string;
  provider: string;
  model_name: string;
  base_url: string | null;
  key_preview: string;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  last_tested_at: string | null;
}

export interface Roles {
  boss: Profile | null;
  worker: Profile | null;
  is_complete: boolean;
}

export interface TestResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  model: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public code?: string,
    public details?: unknown,
    public status = 0,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Backend is not reachable.");
  }
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const e = data?.error ?? {};
    const fieldErrors: { field: string; message: string }[] | undefined = e.details?.errors;
    const extra = fieldErrors?.length ? ` (${fieldErrors.map((x) => `${x.field}: ${x.message}`).join("; ")})` : "";
    throw new ApiError((e.message ?? `HTTP ${res.status}`) + extra, e.code, e.details, res.status);
  }
  return data as T;
}

export interface ConnectionInput {
  provider: string;
  model_name: string;
  api_key?: string;
  base_url?: string | null;
}

export interface ProjectInfo {
  path: string | null;
  exists: boolean;
  name: string | null;
  file_count: number | null;
}

export const workloop = {
  project: () => request<ProjectInfo>("GET", "/project"),
  browseFolder: () => request<{ path: string | null }>("POST", "/project/browse"),
  setProject: (path: string) => request<ProjectInfo>("PUT", "/project", { path }),
  providers: () => request<Provider[]>("GET", "/providers"),
  profiles: () => request<Profile[]>("GET", "/profiles"),
  roles: () => request<Roles>("GET", "/roles"),
  createProfile: (body: ConnectionInput & { name: string; api_key: string }) => request<Profile>("POST", "/profiles", body),
  updateProfile: (id: number, body: Partial<ConnectionInput> & { name?: string }) => request<Profile>("PUT", `/profiles/${id}`, body),
  deleteProfile: (id: number) => request<void>("DELETE", `/profiles/${id}`),
  testUnsaved: (body: ConnectionInput & { api_key: string }) => request<TestResult>("POST", "/profiles/test", body),
  testSaved: (id: number) => request<TestResult>("POST", `/profiles/${id}/test`),
  setRoles: (boss_profile_id: number, worker_profile_id: number) =>
    request<Roles>("PUT", "/roles", { boss_profile_id, worker_profile_id }),

  createPrompt: (content: string) => request<{ id: number; status: string; position: number }>("POST", "/prompts", { content }),
  deletePrompt: (id: number) => request<void>("DELETE", `/prompts/${id}`),
  runState: () => request<{ state: string; run_id: number | null; prompt_id: number | null; round: number | null; max_rounds: number }>("GET", "/run"),
  startRun: () => request<{ run_id: number | null; state: string }>("POST", "/run/start"),
  stopRun: () => request<{ run_id: number | null; state: string }>("POST", "/run/stop"),
  answerPermission: (id: number, approved: boolean) => request<unknown>("POST", `/permissions/${id}`, { approved }),
  fixPrompt: (text: string) => request<{ prompt: { id: number } }>("POST", "/run/fix-prompt", { text }),
  manualFix: (note?: string) => request<unknown>("POST", "/run/manual-fix", note ? { note } : {}),
};

/** Turn an API error into a short message the user can act on. */
export function explainError(e: unknown, locale: "fa" | "en"): string {
  const err = e as ApiError;
  const fa: Record<string, string> = {
    ROLES_NOT_SET: "اول در تنظیمات برای رئیس و کارمند یک API فعال کنید.",
    PROJECT_NOT_SET: "اول پوشه پروژه را بالای کادر پیام وصل کنید.",
    PROJECT_NOT_FOUND: "پوشه پروژه دیگر وجود ندارد. یک پوشه دیگر وصل کنید.",
    NO_PENDING_PROMPTS: "پرامپتی در صف نیست.",
    RUN_ACTIVE: "یک اجرا در جریان است.",
    NOT_NEEDS_USER: "اجرا الان منتظر شما نیست.",
  };
  const en: Record<string, string> = {
    ROLES_NOT_SET: "Set an active API for both the Boss and the Worker in Settings first.",
    PROJECT_NOT_SET: "Connect a project folder above the message box first.",
    PROJECT_NOT_FOUND: "The project folder no longer exists. Connect another one.",
    NO_PENDING_PROMPTS: "There is no prompt in the queue.",
    RUN_ACTIVE: "A run is already in progress.",
    NOT_NEEDS_USER: "The run is not waiting for you right now.",
  };
  if (err.code === "API_TEST_FAILED") {
    const d = err.details as { boss?: { ok: boolean; message: string }; worker?: { ok: boolean; message: string } } | undefined;
    const parts = [
      d?.boss && !d.boss.ok ? `${locale === "fa" ? "رئیس" : "Boss"}: ${d.boss.message}` : "",
      d?.worker && !d.worker.ok ? `${locale === "fa" ? "کارمند" : "Worker"}: ${d.worker.message}` : "",
    ].filter(Boolean).join(" — ");
    return (locale === "fa" ? "اتصال API برقرار نشد. " : "Could not connect the API. ") + parts;
  }
  if (err.status === 0) return locale === "fa" ? "بکند WorkLoop در دسترس نیست." : "The WorkLoop backend is not reachable.";
  return (err.code && (locale === "fa" ? fa : en)[err.code]) || err.message;
}
