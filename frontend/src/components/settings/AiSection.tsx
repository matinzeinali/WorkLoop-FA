import { useEffect, useState } from "react";
import { Check, Pencil, Plus, Trash2, Zap } from "lucide-react";
import { Button, Field, Row, Toggle, inputClass } from "./controls";
import { isDemo, setDemo } from "../../lib/demo";
import Select from "./Select";
import { useLocale } from "../../context/LocaleContext";
import { workloop, type ApiError, type Profile, type Provider, type Roles } from "../../lib/workloopApi";

type RoleKey = "boss" | "worker";

// A profile belongs to a role through its name prefix, e.g. "رئیس: OpenAI اصلی".
const PREFIX: Record<RoleKey, string> = { boss: "رئیس: ", worker: "کارمند: " };
const roleOf = (p: Profile): RoleKey | null =>
  p.name.startsWith(PREFIX.boss) ? "boss" : p.name.startsWith(PREFIX.worker) ? "worker" : null;
const labelOf = (p: Profile) => p.name.replace(PREFIX.boss, "").replace(PREFIX.worker, "");

const TEXT = {
  fa: {
    model: "مدل", boss: "رئیس", worker: "کارمند",
    bossHint: "برنامه‌ریزی می‌کند، تست می‌گیرد و کار کارمند را تأیید یا رد می‌کند.",
    workerHint: "کد را می‌نویسد و اصلاح می‌کند.",
    empty: (r: string) => `هنوز هیچ API برای ${r} اضافه نشده.`,
    add: "افزودن API", name: "نام", namePh: "مثلاً OpenAI اصلی",
    provider: "ارائه‌دهنده", modelName: "نام مدل", apiKey: "کلید API", baseUrl: "Base URL",
    required: "الزامی", optional: "اختیاری", keepKey: (p: string) => `خالی = همان کلید فعلی (${p})`,
    active: "فعال", makeActive: "فعال کن", test: "تست", edit: "ویرایش", del: "حذف",
    save: "ذخیره", cancel: "انصراف", testing: "در حال تست…",
    ok: (ms: number) => `اتصال موفق — ${ms} ms`,
    saved: "API ذخیره شد.", deleted: "API حذف شد.", activated: (r: string) => `API فعال ${r} تغییر کرد.`,
    needOther: (r: string) => `برای فعال شدن، ${r} هم باید یک API فعال داشته باشد.`,
    confirmDel: (n: string) => `«${n}» حذف شود؟`,
    inUse: "این API الان فعال است. اول یک API دیگر را فعال کنید.",
    runActive: "در حین اجرا نمی‌شود API فعال را عوض کرد.",
    needKey: "کلید API را وارد کنید.", needFields: "نام و مدل را وارد کنید.",
    loadFailed: "اطلاعات از بکند WorkLoop دریافت نشد. بکند روشن است؟",
    note: "هر نقش می‌تواند چند API داشته باشد؛ در هر اجرا API «فعال» آن نقش استفاده می‌شود. کلیدها رمزنگاری‌شده در بکند ذخیره می‌شوند.",
  },
  en: {
    model: "Model", boss: "Boss", worker: "Worker",
    bossHint: "Plans, runs tests and approves or rejects the worker's changes.",
    workerHint: "Writes and fixes the code.",
    empty: (r: string) => `No API added for the ${r} yet.`,
    add: "Add API", name: "Name", namePh: "e.g. Main OpenAI",
    provider: "Provider", modelName: "Model name", apiKey: "API key", baseUrl: "Base URL",
    required: "required", optional: "optional", keepKey: (p: string) => `Empty = keep current key (${p})`,
    active: "Active", makeActive: "Make active", test: "Test", edit: "Edit", del: "Delete",
    save: "Save", cancel: "Cancel", testing: "Testing…",
    ok: (ms: number) => `Connected — ${ms} ms`,
    saved: "API saved.", deleted: "API deleted.", activated: (r: string) => `Active ${r} API changed.`,
    needOther: (r: string) => `The ${r} also needs an active API before roles can be set.`,
    confirmDel: (n: string) => `Delete "${n}"?`,
    inUse: "This API is active. Make another one active first.",
    runActive: "The active API can't change while a run is active.",
    needKey: "Enter the API key.", needFields: "Enter a name and a model.",
    loadFailed: "Could not reach the WorkLoop backend. Is it running?",
    note: "Each role can have several APIs; a run uses the role's active one. Keys are stored encrypted by the backend.",
  },
};
type Text = (typeof TEXT)["fa"];

const KEY_PREFIXES: Record<string, string[]> = {
  gemini: ["AIza"],
  anthropic: ["sk-ant-"],
  openrouter: ["sk-or-"],
  groq: ["gsk_"],
  xai: ["xai-"],
  openai: ["sk-"],
  deepseek: ["sk-"],
};

/** A warning when the key clearly doesn't look like the chosen provider's keys. */
function keyMismatch(provider: string, key: string, fa: boolean): string {
  const k = key.trim();
  const expected = KEY_PREFIXES[provider];
  // Saved keys only show their first 4 characters, so a short key matches a longer prefix too.
  if (!k || !expected || expected.some((p) => k.startsWith(p) || (k.length < p.length && p.startsWith(k)))) return "";
  if (provider === "openai" && (k.startsWith("sk-ant-") || k.startsWith("sk-or-"))) return "";
  const list = expected.map((p) => `${p}…`).join(fa ? " یا " : " or ");
  return fa
    ? `کلیدهای این ارائه‌دهنده معمولاً با ${list} شروع می‌شوند. اگر کلید را از سرویس دیگری (مثلاً یک واسط یا سرویس ایرانی) گرفته‌اید، ارائه‌دهنده را «Custom (OpenAI-compatible)» بگذارید و Base URL همان سرویس را وارد کنید.`
    : `Keys for this provider usually start with ${list}. If the key is from another service (a proxy or reseller), choose "Custom (OpenAI-compatible)" and enter that service's Base URL.`;
}

interface Draft { id: number | null; label: string; provider: string; model: string; apiKey: string; baseUrl: string; keyPreview?: string }

function ApiForm({ draft, providers, text, onCancel, onSaved, notify }: {
  draft: Draft; providers: Provider[]; text: Text; onCancel: () => void; onSaved: (d: Draft) => Promise<void>; notify: (m: string) => void;
}) {
  const [d, setD] = useState(draft);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const info = providers.find((p) => p.id === d.provider);
  const set = (patch: Partial<Draft>) => { setResult(null); setD({ ...d, ...patch }); };

  const test = async () => {
    setBusy(true); setResult(null);
    try {
      const r = !d.apiKey.trim() && d.id
        ? await workloop.testSaved(d.id)
        : await workloop.testUnsaved({ provider: d.provider, model_name: d.model, api_key: d.apiKey, base_url: d.baseUrl || null });
      setResult({ ok: r.ok, message: r.ok ? text.ok(r.latency_ms) : r.message });
    } catch (e) { setResult({ ok: false, message: (e as Error).message }); }
    finally { setBusy(false); }
  };

  const save = async () => {
    if (!d.label.trim() || !d.model.trim()) return notify(text.needFields);
    if (!d.id && !d.apiKey.trim()) return notify(text.needKey);
    setBusy(true);
    try { await onSaved(d); } finally { setBusy(false); }
  };

  return (
    <div className="mt-3 rounded-2xl border border-white/15 bg-white/[0.06] p-4">
      <div className="grid grid-cols-1 gap-x-4 md:grid-cols-2">
        <Field label={text.name} htmlFor="api-name">
          <input id="api-name" className={inputClass} placeholder={text.namePh} value={d.label} onChange={(e) => set({ label: e.target.value })} />
        </Field>
        <Field label={text.provider} htmlFor="api-provider">
          <Select id="api-provider" label={text.provider} value={d.provider}
            options={providers.map((p) => ({ value: p.id, label: p.name }))}
            onChange={(provider) => set({ provider, model: "", baseUrl: "" })} />
        </Field>
        <Field label={text.modelName} htmlFor="api-model">
          <input id="api-model" dir="ltr" list="api-models" className={inputClass} placeholder={info?.example_models[0] ?? ""}
            value={d.model} onChange={(e) => set({ model: e.target.value })} />
          <datalist id="api-models">{info?.example_models.map((m) => <option key={m} value={m} />)}</datalist>
        </Field>
        <Field label={text.apiKey} htmlFor="api-key">
          <input id="api-key" type="password" dir="ltr" autoComplete="off" className={inputClass}
            placeholder={d.keyPreview ? text.keepKey(d.keyPreview) : (KEY_PREFIXES[d.provider]?.[0] ?? "") + "..."} value={d.apiKey} onChange={(e) => set({ apiKey: e.target.value })} />
          {keyMismatch(d.provider, d.apiKey || (d.keyPreview ?? "").split("...")[0], text === TEXT.fa) && (
            <p role="alert" className="m-0 text-[12.5px] leading-5 text-amber-300">
              {keyMismatch(d.provider, d.apiKey || (d.keyPreview ?? "").split("...")[0], text === TEXT.fa)}
            </p>
          )}
        </Field>
      </div>
      <Field label={`${text.baseUrl} (${info?.requires_base_url ? text.required : text.optional})`} htmlFor="api-base">
        <input id="api-base" type="url" dir="ltr" className={inputClass} placeholder={info?.default_base_url ?? ""}
          value={d.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} />
      </Field>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={save} disabled={busy}>{text.save}</Button>
        <Button onClick={test} disabled={busy || !d.model.trim()}>{busy ? text.testing : text.test}</Button>
        <Button onClick={onCancel} disabled={busy}>{text.cancel}</Button>
        {result && <span role="status" className={`text-[13px] ${result.ok ? "text-emerald-300" : "text-red-300"}`}>{result.message}</span>}
      </div>
    </div>
  );
}

export default function AiSection({ onNotify }: { onNotify: (message: string) => void }) {
  const { locale } = useLocale();
  const text = TEXT[locale === "fa" ? "fa" : "en"];
  const [role, setRole] = useState<RoleKey>("boss");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [roles, setRoles] = useState<Roles | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [testMsg, setTestMsg] = useState<Record<number, { ok: boolean; message: string }>>({});

  const reload = async () => {
    const [p, r] = await Promise.all([workloop.profiles(), workloop.roles()]);
    setProfiles(p); setRoles(r);
  };
  useEffect(() => {
    Promise.all([workloop.providers(), reload()]).then(([list]) => setProviders(list)).catch(() => setLoadError(true));
  }, []);

  const fail = (e: unknown) => {
    const err = e as ApiError;
    onNotify(err.code === "RUN_ACTIVE" ? text.runActive : err.code === "PROFILE_IN_USE" ? text.inUse : err.message);
  };

  const list = profiles.filter((p) => roleOf(p) === role);
  const activeId = roles?.[role]?.id ?? null;
  const other: RoleKey = role === "boss" ? "worker" : "boss";

  const activate = async (id: number) => {
    const otherId = roles?.[other]?.id ?? profiles.find((p) => roleOf(p) === other)?.id;
    if (!otherId) return onNotify(text.needOther(text[other]));
    try {
      setRoles(role === "boss" ? await workloop.setRoles(id, otherId) : await workloop.setRoles(otherId, id));
      onNotify(text.activated(text[role]));
    } catch (e) { fail(e); }
  };

  const saveDraft = async (d: Draft) => {
    const body = { name: PREFIX[role] + d.label.trim(), provider: d.provider, model_name: d.model.trim(), base_url: d.baseUrl.trim() || null };
    try {
      const saved = d.id
        ? await workloop.updateProfile(d.id, { ...body, ...(d.apiKey.trim() ? { api_key: d.apiKey.trim() } : {}) })
        : await workloop.createProfile({ ...body, api_key: d.apiKey.trim() });
      setDraft(null);
      await reload();
      onNotify(text.saved);
      // The first API of a role becomes its active one, when the other role is ready too.
      if (!d.id && !activeId) {
        const otherId = roles?.[other]?.id ?? profiles.find((p) => roleOf(p) === other)?.id;
        if (otherId) setRoles(role === "boss" ? await workloop.setRoles(saved.id, otherId) : await workloop.setRoles(otherId, saved.id));
      }
    } catch (e) { fail(e); }
  };

  const remove = async (p: Profile) => {
    if (!confirm(text.confirmDel(labelOf(p)))) return;
    try { await workloop.deleteProfile(p.id); await reload(); onNotify(text.deleted); } catch (e) { fail(e); }
  };

  const test = async (p: Profile) => {
    setTesting(p.id);
    try {
      const r = await workloop.testSaved(p.id);
      setTestMsg((m) => ({ ...m, [p.id]: { ok: r.ok, message: r.ok ? text.ok(r.latency_ms) : r.message } }));
      await reload();
    } catch (e) { fail(e); }
    finally { setTesting(null); }
  };

  const demoRow = <DemoRow fa={locale === "fa"} />;
  if (loadError) return <>{demoRow}<p role="alert" className="m-0 text-[13px] text-red-300">{text.loadFailed}</p></>;
  const providerName = (id: string) => providers.find((p) => p.id === id)?.name ?? id;

  return (
    <>
      {demoRow}
      <Field label={text.model} htmlFor="ai-role">
        <div id="ai-role" role="radiogroup" aria-label={text.model} className="light-choice-group grid grid-cols-2 gap-1 rounded-xl border border-white/10 bg-white/10 p-1">
          {(["boss", "worker"] as RoleKey[]).map((r) => (
            <button key={r} type="button" role="radio" aria-checked={role === r}
              onClick={() => { setRole(r); setDraft(null); }}
              className={`cursor-pointer rounded-lg py-2 text-[14px] font-medium transition-colors ${role === r ? "bg-white/20 text-white" : "text-white/60 hover:text-white"}`}>
              {text[r]}
              <span className="ms-2 text-[12px] text-white/45">{new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US").format(profiles.filter((p) => roleOf(p) === r).length)}</span>
            </button>
          ))}
        </div>
        <p className="m-0 text-[13px] text-white/50">{role === "boss" ? text.bossHint : text.workerHint}</p>
      </Field>

      <div className="mt-2 flex flex-col gap-2">
        {!list.length && !draft && <p className="m-0 rounded-xl border border-dashed border-white/15 p-4 text-center text-[13px] text-white/50">{text.empty(text[role])}</p>}
        {list.map((p) => {
          const active = p.id === activeId;
          const msg = testMsg[p.id];
          return draft?.id === p.id ? (
            <ApiForm key={p.id} draft={draft} providers={providers} text={text} notify={onNotify} onCancel={() => setDraft(null)} onSaved={saveDraft} />
          ) : (
            <div key={p.id} data-active={active} className={`light-ai-profile-card rounded-xl border px-4 py-3 ${active ? "border-white/30 bg-white/[0.09]" : "border-white/10 bg-white/[0.04]"}`}>
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" onClick={() => !active && activate(p.id)} aria-pressed={active} title={active ? text.active : text.makeActive}
                  className={`grid size-5 shrink-0 cursor-pointer place-items-center rounded-full border ${active ? "border-white bg-white text-ink" : "border-white/40 hover:border-white"}`}>
                  {active && <Check className="size-3.5" strokeWidth={3} aria-hidden="true" />}
                </button>
                <div className="min-w-0 flex-1">
                  <p className="m-0 flex items-center gap-2 text-[14px] font-medium text-white">
                    {labelOf(p)}
                    {active && <span className="rounded-full bg-white/15 px-2 text-[11px] text-white/80">{text.active}</span>}
                    {p.last_test_ok != null && <span className={`text-[12px] ${p.last_test_ok ? "text-emerald-300" : "text-red-300"}`}>{p.last_test_ok ? "✓" : "✗"}</span>}
                  </p>
                  <p className="m-0 text-[12px] text-white/50" dir="auto">{providerName(p.provider)} · <span dir="ltr">{p.model_name}</span> · <span dir="ltr">{p.key_preview}</span></p>
                  {keyMismatch(p.provider, p.key_preview.split("...")[0], locale === "fa") && (
                    <p className="m-0 mt-0.5 text-[12px] text-amber-300">
                      {locale === "fa" ? "⚠ این کلید شبیه کلیدهای این ارائه‌دهنده نیست." : "⚠ This key doesn't look like this provider's keys."}
                    </p>
                  )}
                </div>
                <div className="flex gap-1">
                  <IconBtn label={text.test} onClick={() => test(p)} disabled={testing === p.id}><Zap className="size-4" /></IconBtn>
                  <IconBtn label={text.edit} onClick={() => setDraft({ id: p.id, label: labelOf(p), provider: p.provider, model: p.model_name, apiKey: "", baseUrl: p.base_url ?? "", keyPreview: p.key_preview })}><Pencil className="size-4" /></IconBtn>
                  <IconBtn label={text.del} onClick={() => remove(p)} danger><Trash2 className="size-4" /></IconBtn>
                </div>
              </div>
              {testing === p.id && <p className="m-0 mt-1 text-[12px] text-white/50">{text.testing}</p>}
              {msg && testing !== p.id && <p role="status" className={`m-0 mt-1 text-[12px] ${msg.ok ? "text-emerald-300" : "text-red-300"}`}>{msg.message}</p>}
            </div>
          );
        })}
        {draft && draft.id === null && (
          <ApiForm draft={draft} providers={providers} text={text} notify={onNotify} onCancel={() => setDraft(null)} onSaved={saveDraft} />
        )}
      </div>

      {!draft && (
        <div className="mt-3">
          <Button onClick={() => setDraft({ id: null, label: "", provider: providers[0]?.id ?? "", model: "", apiKey: "", baseUrl: "" })} disabled={!providers.length}>
            <span className="inline-flex items-center gap-1.5"><Plus className="size-4" aria-hidden="true" />{text.add}</span>
          </Button>
        </div>
      )}
      <p className="m-0 mt-4 text-[13px] text-white/50">{text.note}</p>
    </>
  );
}

function IconBtn({ label, onClick, children, danger, disabled }: { label: string; onClick: () => void; children: React.ReactNode; danger?: boolean; disabled?: boolean }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick} disabled={disabled}
      className={`grid size-8 cursor-pointer place-items-center rounded-lg transition-colors disabled:opacity-40 ${danger ? "text-red-300 hover:bg-red-500/15" : "text-white/70 hover:bg-white/10 hover:text-white"}`}>
      {children}
    </button>
  );
}

function DemoRow({ fa }: { fa: boolean }) {
  const [on, setOn] = useState(isDemo);
  return (
    <div className="mb-3 rounded-2xl border border-white/10 bg-white/[0.04] px-4">
      <Row
        title={fa ? "حالت دمو" : "Demo mode"}
        description={fa
          ? "گفتگوی از پیش‌نوشته‌شده بین شما، رئیس و کارمند؛ بدون API و بدون تغییر در فایل‌ها."
          : "A scripted conversation between you, the Boss and the Worker; no API calls, no file changes."}
      >
        <Toggle label={fa ? "حالت دمو" : "Demo mode"} checked={on} onChange={(v) => { setDemo(v); setOn(v); }} />
      </Row>
    </div>
  );
}
