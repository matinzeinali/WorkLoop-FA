import type { ChatGroup } from "../../data/chats";
import ChatListItem from "./ChatListItem";
import type { ChatAction } from "./ChatItemMenu";
import { useLocale } from "../../context/LocaleContext";
import { Folder } from "lucide-react";

interface Props {
  groups: ChatGroup[];
  projectGroups?: ChatGroup[];
  activeId?: string;
  onSelect?: (id: string) => void;
  onAction?: (id: string, action: ChatAction) => void;
  emptyMessage?: string;
}

export default function ChatList({ groups, projectGroups = [], activeId, onSelect, onAction, emptyMessage }: Props) {
  const { t } = useLocale();
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
      {groups.length === 0 && projectGroups.length === 0 ? (
        <p role="status" className="m-0 ps-[18px] pt-2 text-[13px] text-white/55">{emptyMessage}</p>
      ) : (
        <>
          {projectGroups.length > 0 && (
            <section className="mb-5" aria-labelledby="group-projects">
              <h2 id="group-projects" className="m-0 ps-[18px] pt-1 text-start text-[12px] font-semibold leading-[24px] text-white/55">
                {t("projects")}
              </h2>
              <div className="mt-1 flex flex-col gap-2">
                {projectGroups.map((group) => (
                  <section key={group.id} aria-labelledby={`group-${group.id}`}>
                    <h3 id={`group-${group.id}`} dir="auto" className="m-0 flex items-center gap-2 ps-[18px] py-1 text-start text-[13px] font-medium text-white/75">
                      <Folder className="size-4 shrink-0" aria-hidden="true" />
                      <span className="truncate">{group.label}</span>
                    </h3>
                    {group.chats.length > 0 ? renderChats(group) : (
                      <p className="m-0 py-1 ps-[42px] text-[12px] text-white/40">{t("noProjectChats")}</p>
                    )}
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
