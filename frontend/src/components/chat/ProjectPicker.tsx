import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, Folder, FolderOpen, FolderPlus, Laptop, Lightbulb, MessageSquare, Plus, X } from "lucide-react";
import { useDismiss } from "../../hooks/useDismiss";
import { useLocale } from "../../context/LocaleContext";
import { workloop, type ApiError, type ProjectInfo } from "../../lib/workloopApi";
import { ACTIVE_PROJECT_CHANGED, OPEN_NEW_PROJECT, PROJECTS_CHANGED, readProjects, setActiveProject, writeProjects, type SavedProject } from "../../lib/projectState";

// A project is a name + one folder. The backend keeps only the active folder,
// so the list of saved projects lives in this browser.
const folderName = (path: string) => path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;

const TEXT = {
  fa: {
    choose: "انتخاب پروژه", create: "ساخت پروژه", newProject: "پروژه جدید",
    name: "نام پروژه", namePh: "مثلاً فروشگاه آنلاین", folders: "پوشه پروژه", thisComputer: "همین کامپیوتر",
    addFolder: "افزودن پوشه", picking: "پنجره انتخاب پوشه باز شد…", typePath: "یا مسیر را دستی وارد کنید",
    tip: "رئیس و کارمند فقط داخل پوشه این پروژه فایل می‌خوانند، می‌نویسند و دستور اجرا می‌کنند.",
    creating: "در حال ساخت…", remove: "حذف", close: "بستن",
    runActive: "در حین اجرا نمی‌شود پروژه را عوض کرد.", offline: "بکند WorkLoop در دسترس نیست.",
    missing: "پوشه پیدا نشد",
  },
  en: {
    choose: "Choose project", create: "Create project", newProject: "New project",
    name: "Project name", namePh: "e.g. Online shop", folders: "Source folder", thisComputer: "This computer",
    addFolder: "Add folder", picking: "Folder dialog is open…", typePath: "or type the path",
    tip: "The Boss and Worker only read, write and run commands inside this project's folder.",
    creating: "Creating…", remove: "Remove", close: "Close",
    runActive: "The project can't change while a run is active.", offline: "The WorkLoop backend is not reachable.",
    missing: "Folder not found",
  },
};
type Text = (typeof TEXT)["fa"];

function errorText(e: unknown, text: Text) {
  const err = e as ApiError;
  const field = (err.details as { errors?: { message: string }[] } | undefined)?.errors?.[0]?.message;
  return err.code === "RUN_ACTIVE" ? text.runActive : err.status === 0 ? text.offline : field ?? err.message;
}

export default function ProjectPicker() {
  const { locale } = useLocale();
  const text = TEXT[locale === "fa" ? "fa" : "en"];
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [projects, setProjects] = useState(readProjects);
  const [menuOpen, setMenuOpen] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [error, setError] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  useDismiss(rootRef, menuOpen, () => setMenuOpen(false));
  useEffect(() => {
    const onList = () => setProjects(readProjects());
    const onActive = (e: Event) => setProject((e as CustomEvent<ProjectInfo | null>).detail);
    const onOpen = () => setModalOpen(true);
    window.addEventListener(PROJECTS_CHANGED, onList);
    window.addEventListener(ACTIVE_PROJECT_CHANGED, onActive);
    window.addEventListener(OPEN_NEW_PROJECT, onOpen);
    return () => {
      window.removeEventListener(PROJECTS_CHANGED, onList);
      window.removeEventListener(ACTIVE_PROJECT_CHANGED, onActive);
      window.removeEventListener(OPEN_NEW_PROJECT, onOpen);
    };
  }, []);

  useEffect(() => {
    workloop.project()
      .then((info) => { setProject(info); setActiveProject(info); })
      .catch(() => { setProject(null); setActiveProject(null); });
  }, []);

  const active = projects.find((p) => p.path === project?.path);
  const label = active?.name ?? project?.name ?? text.choose;
  const ok = !!project?.path && project.exists;

  const activate = async (p: SavedProject) => {
    setError("");
    try {
      const info = await workloop.setProject(p.path);
      setProject(info); setActiveProject(info); setMenuOpen(false);
    }
    catch (e) { setError(errorText(e, text)); }
  };

  const onCreated = (p: SavedProject, info: ProjectInfo) => {
    const list = [p, ...projects.filter((x) => x.path !== p.path)];
    setProjects(list); writeProjects(list); setProject(info); setActiveProject(info); setModalOpen(false);
  };

  const removeSaved = (path: string) => {
    const list = projects.filter((x) => x.path !== path);
    setProjects(list); writeProjects(list);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => (projects.length ? setMenuOpen((o) => !o) : setModalOpen(true))}
        aria-expanded={menuOpen}
        title={project?.path ?? text.choose}
        className={`flex h-8 max-w-[260px] cursor-pointer items-center gap-1.5 rounded-full px-3 text-[13.5px] transition-colors hover:bg-white/10 ${
          project?.path && !ok ? "text-red-300" : "text-white/90"
        }`}
      >
        {ok ? <FolderOpen className="size-4 shrink-0" aria-hidden="true" /> : <Folder className="size-4 shrink-0" aria-hidden="true" />}
        <span className="truncate">{project?.path && !project.exists ? `${label} · ${text.missing}` : label}</span>
      </button>

      {menuOpen && (
        <div role="menu" className="absolute bottom-full start-0 z-40 mb-3 w-[min(320px,86vw)] rounded-2xl glass-pop p-1.5 text-white">
          {projects.map((p) => {
            const current = p.path === project?.path;
            return (
              <div key={p.path} className="group flex items-center rounded-xl hover:bg-white/10">
                <button type="button" role="menuitem" onClick={() => void activate(p)} className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5 px-3 py-2 text-start">
                  <Folder className="size-4 shrink-0 text-white/55" aria-hidden="true" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13.5px]">{p.name}</span>
                    <span dir="ltr" className="block truncate text-start text-[11.5px] text-white/55">{p.path}</span>
                  </span>
                  {current && <Check className="size-4 shrink-0" aria-hidden="true" />}
                </button>
                {!current && (
                  <button type="button" aria-label={text.remove} title={text.remove} onClick={() => removeSaved(p.path)}
                    className="me-1 grid size-7 shrink-0 cursor-pointer place-items-center rounded-lg text-white/40 opacity-0 hover:bg-white/10 hover:text-white group-hover:opacity-100">
                    <X className="size-3.5" aria-hidden="true" />
                  </button>
                )}
              </div>
            );
          })}
          <div className="my-1 h-px bg-white/10" />
          <button type="button" role="menuitem"
            onClick={async () => {
              setError("");
              try { const info = await workloop.useScratchProject(); setProject(info); setActiveProject(info); setMenuOpen(false); }
              catch (e) { setError(errorText(e, text)); }
            }}
            className="flex w-full cursor-pointer items-center gap-2.5 rounded-xl px-3 py-2 text-start text-[13.5px] hover:bg-white/10">
            <MessageSquare className="size-4 text-white/55" aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="block">{locale === "fa" ? "بدون پروژه" : "No project"}</span>
              <span className="block text-[11.5px] text-white/45">{locale === "fa" ? "فقط چت؛ در پوشه WorkLoop-Workspace" : "Just chat, in the WorkLoop-Workspace folder"}</span>
            </span>
          </button>
          <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); setModalOpen(true); }}
            className="flex w-full cursor-pointer items-center gap-2.5 rounded-xl px-3 py-2 text-start text-[13.5px] hover:bg-white/10">
            <Plus className="size-4 text-white/55" aria-hidden="true" />{text.newProject}
          </button>
          {error && <p role="alert" className="m-0 px-3 pb-1.5 text-[12.5px] text-red-300">{error}</p>}
        </div>
      )}

      {modalOpen && <CreateProjectModal text={text} onClose={() => setModalOpen(false)} onCreated={onCreated} />}
    </div>
  );
}

function CreateProjectModal({ text, onClose, onCreated }: {
  text: Text; onClose: () => void; onCreated: (p: SavedProject, info: ProjectInfo) => void;
}) {
  const { locale } = useLocale();
  const [name, setName] = useState("");
  const [folder, setFolder] = useState<string | null>(null);
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState("");
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    nameRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCloseRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const pick = async () => {
    setError(""); setPicking(true);
    try {
      const res = await workloop.browseFolder();
      if (res.path) { setFolder(res.path); setTyping(false); }
    } catch (e) {
      setError(errorText(e, text)); setTyping(true);
    } finally { setPicking(false); }
  };

  const path = folder ?? (typed.trim() || null);
  const create = async () => {
    if (!path || busy) return;
    setBusy(true); setError("");
    try {
      const info = await workloop.setProject(path);
      onCreated({ name: name.trim() || info.name || folderName(path), path: info.path ?? path }, info);
    } catch (e) { setError(errorText(e, text)); }
    finally { setBusy(false); }
  };

  return createPortal(
    <div dir={locale === "fa" ? "rtl" : "ltr"} className="fixed inset-0 z-[90] grid place-items-center glass-scrim p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-labelledby="create-project-title"
        className="w-full max-w-[560px] rounded-[28px] glass-pop p-6 text-white">
        <div className="mb-5 flex items-center justify-between">
          <h2 id="create-project-title" className="m-0 text-[21px] font-medium">{text.create}</h2>
          <button type="button" aria-label={text.close} onClick={onClose} className="grid size-8 cursor-pointer place-items-center rounded-full text-white/55 hover:bg-white/10 hover:text-white">
            <X className="size-[18px]" aria-hidden="true" />
          </button>
        </div>

        <label htmlFor="project-name" className="mb-1.5 block text-[14.5px] text-white/90">{text.name}</label>
        <div className="mb-5 flex items-center gap-2.5 rounded-2xl border border-white/15 bg-white/[0.08] px-3.5 focus-within:border-white/35">
          <Folder className="size-[18px] shrink-0 text-white/55" aria-hidden="true" />
          <input ref={nameRef} id="project-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={path ? folderName(path) : text.namePh}
            onKeyDown={(e) => { if (e.key === "Enter") void create(); }}
            className="w-full bg-transparent py-2.5 text-[14.5px] text-white outline-none placeholder:text-white/40" />
        </div>

        <div className="mb-1.5 flex items-center justify-between">
          <p className="m-0 text-[14.5px] font-medium text-white/90">{text.folders}</p>
          <span className="flex items-center gap-1.5 text-[13.5px] text-white/55"><Laptop className="size-4" aria-hidden="true" />{text.thisComputer}</span>
        </div>
        <div className="overflow-hidden rounded-2xl border border-white/15">
          {folder ? (
            <div className="flex items-center gap-3 px-4 py-3">
              <Folder className="size-[18px] shrink-0 text-white/55" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14.5px]">{folderName(folder)}</span>
                <span dir="ltr" className="block truncate text-start text-[11.5px] text-white/40">{folder}</span>
              </span>
              <button type="button" aria-label={text.remove} title={text.remove} onClick={() => setFolder(null)}
                className="grid size-7 cursor-pointer place-items-center rounded-full text-white/55 hover:bg-white/10 hover:text-white">
                <X className="size-4" aria-hidden="true" />
              </button>
            </div>
          ) : (
            <>
              <button type="button" onClick={() => void pick()} disabled={picking}
                className="flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-start text-[14.5px] text-white/90 hover:bg-white/10 disabled:cursor-wait">
                <FolderPlus className="size-[18px] text-white/55" aria-hidden="true" />{picking ? text.picking : text.addFolder}
              </button>
              {typing ? (
                <div className="border-t border-white/15 px-4 py-2.5">
                  <input dir="ltr" autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="D:\Projects\my-app"
                    onKeyDown={(e) => { if (e.key === "Enter") void create(); }}
                    className="w-full rounded-xl border border-white/15 bg-white/[0.08] px-3 py-2 text-[13px] text-white outline-none placeholder:text-white/40 focus:border-white/35" />
                </div>
              ) : (
                <button type="button" onClick={() => setTyping(true)} className="block w-full cursor-pointer border-t border-white/15 px-4 py-2 text-start text-[12.5px] text-white/55 hover:text-white">
                  {text.typePath}
                </button>
              )}
            </>
          )}
        </div>

        <div className="mt-4 flex items-start gap-2.5 rounded-2xl bg-white/[0.08] px-4 py-3 text-[13.5px] text-white/70">
          <Lightbulb className="mt-0.5 size-[18px] shrink-0" aria-hidden="true" />{text.tip}
        </div>

        {error && <p role="alert" className="m-0 mt-3 text-[13px] text-red-300">{error}</p>}

        <div className="mt-5 flex justify-end">
          <button type="button" onClick={() => void create()} disabled={!path || busy}
            className="cursor-pointer rounded-full bg-white px-5 py-2.5 text-[14.5px] font-semibold text-ink transition hover:brightness-90 disabled:cursor-not-allowed disabled:bg-white/25 disabled:text-white/60">
            {busy ? text.creating : text.create}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
