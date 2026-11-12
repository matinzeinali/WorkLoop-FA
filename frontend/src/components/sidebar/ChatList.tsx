import type { ChatGroup } from "../../data/chats";
import ChatListItem from "./ChatListItem";
import type { ChatAction } from "./ChatItemMenu";
import { useLocale } from "../../context/LocaleContext";
import { useState } from "react";
import { ChevronDown, Folder, FolderOpen, Plus } from "lucide-react";

// Which projects are collapsed in the sidebar, remembered per browser.
const COLLAPSED_KEY = "workloop:collapsed-projects";
const readCollapsed = (): string[] => {
  try { const v: unknown = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]"); return Array.isArray(v) ? v.filter((x) => typeof x === "string") : []; } catch { return []; }
};

interface Props {
  groups: ChatGroup[];
  projectGroups?: ChatGroup[];
  activeId?: string;
  onSelect?: (id: string) => void;
  onAction?: (id: string, action: ChatAction) => void;
  emptyMessage?: string;
  activeProjectPath?: string | null;
  onSelectProject?: (path: string) => void;
  onNewProject?: () => void;
}

export default function ChatList({ groups, projectGroups = [], activeId, onSelect, onAction, emptyMessage, activeProjectPath = null, onSelectProject, onNewProject }: Props) {
  const { t, locale } = useLocale();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggleProject = (path: string) => setCollapsed((current) => {
    const next = current.includes(path) ? current.filter((p) => p !== path) : [...current, path];
    try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next)); } catch { /* storage unavailable */ }
    return next;
  });
  const renderChats = (group: ChatGroup) => (
    <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
      {group.chats.map((chat) => (
        <ChatListItem
          key={chat.id}
          id={chat.id}
          title={chat.title}
          pinned={group.id === "pinned"}
          archived={group.id === "archive"}
          active={chat.id === activeId}
          onSelect={() => onSelect?.(chat.id)}
          onAction={onAction}
        />
      ))}
    </ul>
  );
  return (
    <nav
      aria-label={t("history")}
      className="relative mt-[21.5px] min-h-0 flex-1 before:absolute before:bottom-0 before:left-0 before:top-2 before:w-px before:rounded-full before:bg-white/20 rtl:before:left-auto rtl:before:right-0"
    >
      <div data-chat-scroll className="thin-scroll chat-scroll h-full overflow-y-auto overflow-x-hidden">
      {groups.length === 0 && projectGroups.length === 0 && !onNewProject ? (
        <p role="status" className="m-0 ps-[18px] pt-2 text-[13px] text-white/55">{emptyMessage}</p>
      ) : (
        <>
          {(projectGroups.length > 0 || onNewProject) && (
            <section className="mb-5" aria-labelledby="group-projects">
              <div className="flex items-center justify-between pe-2">
                <h2 id="group-projects" className="m-0 ps-[18px] pt-1 text-start text-[12px] font-semibold leading-[24px] text-white/55">
                  {t("projects")}
                </h2>
                {onNewProject && (
                  <button type="button" onClick={onNewProject} aria-label={t("newProject")} title={t("newProject")}
                    className="grid size-6 cursor-pointer place-items-center rounded-md text-white/55 transition hover:bg-white/10 hover:text-white">
                    <Plus className="size-4" aria-hidden="true" />
                  </button>
                )}
              </div>
              {projectGroups.length === 0 && (
                <button type="button" onClick={onNewProject} className="m-0 cursor-pointer ps-[18px] py-1 text-start text-[12.5px] text-white/40 hover:text-white/70">{t("noProjects")}</button>
              )}
              <div className="mt-1 flex flex-col gap-2">
                {projectGroups.map((group) => (
                  <section key={group.id} aria-labelledby={`group-${group.id}`}>
                    {(() => {
                      const path = group.id.replace(/^project:/, "");
                      const current = path === activeProjectPath;
                      const open = !collapsed.includes(path);
                      return (
                        <>
                          <h3 id={`group-${group.id}`} className="m-0">
                            <div className={`group/project flex w-[calc(100%-8px)] items-center rounded-lg transition-colors ${
                              current ? "bg-white/[0.07]" : "hover:bg-white/[0.05]"
                            }`}>
                              {/* Name toggles the project's chats open/closed. */}
                              <button type="button" onClick={() => toggleProject(path)} title={path} dir="auto"
                                aria-expanded={open} aria-current={current ? "true" : undefined}
                                className={`flex min-w-0 flex-1 cursor-pointer items-center gap-2.5 py-1.5 ps-[18px] text-start text-[13.5px] ${
                                  current ? "font-semibold text-white" : "font-medium text-white/70 hover:text-white"
                                }`}>
                                {current
                                  ? <FolderOpen className="size-4 shrink-0 text-amber-200/90" aria-hidden="true" />
                                  : <Folder className="size-4 shrink-0 text-white/50" aria-hidden="true" />}
                                <span className="truncate">{group.label}</span>
                                <ChevronDown aria-hidden="true" className={`size-3.5 shrink-0 text-white/45 transition-transform duration-200 ${
                                  open ? "" : locale === "fa" ? "rotate-90" : "-rotate-90"
                                }`} />
                              </button>
                              {/* New chat inside this project. */}
                              <button type="button" onClick={() => onSelectProject?.(path)}
                                aria-label={`${t("newChat")} · ${group.label}`} title={t("newChat")}
                                className="me-1 grid size-7 shrink-0 cursor-pointer place-items-center rounded-md text-white/50 opacity-70 transition hover:bg-white/10 hover:text-white group-hover/project:opacity-100">
                                <Plus className="size-4" aria-hidden="true" />
                              </button>
                            </div>
                          </h3>
                          {/* The project's chats hang under it on a thin guide line, like a folder tree. */}
                          {open && (
                            <div className="ms-[26px] me-3 mt-1 border-s border-white/10 ps-2 [&_li]:!ms-0 [&_li]:!w-full">
                              {group.chats.length > 0 ? renderChats(group) : (
                                <p className="m-0 py-1 ps-2 text-[12px] text-white/40">{t("noProjectChats")}</p>
                              )}
                            </div>
                          )}
                        </>
                      );
                    })()}
                  </section>
                ))}
              </div>
            </section>
          )}
          {groups.length > 0 && (
            <section aria-labelledby="group-recents">
              <h2 id="group-recents" className="m-0 ps-[18px] pt-1 text-start text-[12px] font-semibold leading-[24px] text-white/55">
                {t("recents")}
              </h2>
              {groups.map((group) => (
                <section key={group.id} className="mb-3" aria-labelledby={`group-${group.id}`}>
          <h2
            id={`group-${group.id}`}
            dir="auto"
            className="m-0 ps-[18px] pt-1 text-start text-[12px] font-medium leading-[24px] text-white/50"
          >
            {group.label}
          </h2>
                  {renderChats(group)}
                </section>
              ))}
            </section>
          )}
        </>
      )}
      </div>
    </nav>
  );
}
