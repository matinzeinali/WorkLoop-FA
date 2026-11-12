import { useEffect, useMemo, useRef, useState } from "react";
import { CirclePlus, PanelLeft, Search } from "lucide-react";
import { useBackground } from "./context/BackgroundContext";
import ChatPanel from "./components/chat/ChatPanel";
import SettingsPanel from "./components/settings/SettingsPanel";
import Sidebar from "./components/sidebar/Sidebar";
import { chatGroups, demoChats, type ChatGroup, type ChatSummary } from "./data/chats";
import { PLACEHOLDER_REPLY, type Attachment, type Message } from "./data/messages";
import type { ChatAction } from "./components/sidebar/ChatItemMenu";
import { getTranslation, useLocale, type Locale } from "./context/LocaleContext";
import ActionDialog, { type ActionDialogState } from "./components/ui/ActionDialog";
import { customInstructions, selectedModel, sendChat, type ChatMessage } from "./lib/paastaApi";
import LoginScreen from "./components/auth/LoginScreen";
import { workloop, explainError } from "./lib/workloopApi";
import { ACTIVE_PROJECT_CHANGED, OPEN_NEW_PROJECT, PROJECTS_CHANGED, getActiveProject, readProjects, setActiveProject } from "./lib/projectState";
import { applyEvent, eventPromptId, promptLang, subscribeLive, traceToText, type RunState, type RunTrace } from "./lib/workloopLive";
import type { RunActions } from "./components/chat/RunTraceView";
import { isDemo, runDemo } from "./lib/demo";
import { authenticateUser, restoreAuthSession, saveAuthSession, type AuthAccount } from "./lib/localAuth";

interface Conversation extends ChatSummary {
  groupId: string;
  /** Folder of the project that was active when the chat started. */
  projectPath?: string;
  messages: Message[];
  updatedAt: number;
}

const chatStorageKey = (username: string) => `chatbot:conversations:${username}`;
const demoChatsSeededKey = (username: string) => `chatbot:demo-chats-seeded:${username}`;
const profileNameKey = (username: string) => `chatbot:account-name:${username}`;

const starterTitleKeys: Record<string, string> = {
  "1": "chatDefault1", "2": "chatDefault2", "3": "chatDefault3", "4": "chatDefault4", "5": "chatDefault5",
};

function localizedTitle(id: string, title: string, locale: Locale) {
  const original = chatGroups.flatMap((group) => group.chats).find((chat) => chat.id === id);
  const demo = demoChats.find((chat) => chat.id === id);
  if (demo && (title === demo.title || title === demo.titleFa)) return locale === "fa" ? demo.titleFa : demo.title;
  const key = starterTitleKeys[id];
  return original && key && (title === original.title || title === getTranslation("fa", key) || title === getTranslation("en", key))
    ? getTranslation(locale, key)
    : title;
}

function localizedMessages(conversation: Conversation, locale: Locale): Message[] {
  const starter = chatGroups.flatMap((group) => group.chats).find((chat) => chat.id === conversation.id);
  const demo = demoChats.find((chat) => chat.id === conversation.id);
  const key = starterTitleKeys[conversation.id];
  const translatedTitle = localizedTitle(conversation.id, conversation.title, locale);
  return conversation.messages.map((message, index) => ({
    ...message,
    content: message.previewPrompt !== undefined
      ? makePreviewAnswer(locale, message.previewPrompt, message.previewGeneration ?? 0)
      : message.content === PLACEHOLDER_REPLY || message.content === getTranslation("en", "demoReply") || message.content === getTranslation("fa", "demoReply")
      ? getTranslation(locale, "demoReply")
      : demo && message.role === "user" && [demo.promptEn, demo.promptFa].includes(message.content)
        ? locale === "fa" ? demo.promptFa : demo.promptEn
        : demo && message.role === "assistant" && [demo.answerEn, demo.answerFa].includes(message.content)
          ? locale === "fa" ? demo.answerFa : demo.answerEn
      : starter && key && index === 0 && [starter.title, getTranslation("en", key), getTranslation("fa", key)].includes(message.content)
        ? translatedTitle
        : message.content,
  }));
}

function makePreviewAnswer(locale: Locale, prompt: string, generation: number) {
  const intro = getTranslation(locale, generation > 0 ? "regeneratedPreviewFor" : "previewAnswerFor")
    .replace("{prompt}", prompt.trim())
    .replace("{generation}", String(generation));
  return `${intro}\n\n${getTranslation(locale, "demoReply")}`;
}


function makeStarterConversations(locale: Locale): Conversation[] {
  return chatGroups.flatMap((group) => group.chats.map((chat, index) => ({
    ...chat,
    title: localizedTitle(chat.id, chat.title, locale),
    groupId: group.id,
    updatedAt: Date.now() - index * 60_000,
    messages: [
      { id: `${chat.id}-user`, role: "user" as const, content: demoChats.find((demo) => demo.id === chat.id)?.[locale === "fa" ? "promptFa" : "promptEn"] ?? localizedTitle(chat.id, chat.title, locale) },
      { id: `${chat.id}-assistant`, role: "assistant" as const, content: demoChats.find((demo) => demo.id === chat.id)?.[locale === "fa" ? "answerFa" : "answerEn"] ?? getTranslation(locale, "demoReply") },
    ],
  })));
}

function readConversations(locale: Locale, username: string): Conversation[] {
  const storageKey = chatStorageKey(username);
  const seededKey = demoChatsSeededKey(username);
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        const conversations = parsed.filter((item): item is Conversation =>
          !!item && typeof item.id === "string" && typeof item.title === "string" &&
          typeof item.groupId === "string" && Array.isArray(item.messages),
        ).map((item) => {
          const translated = localizedTitle(item.id, item.title, locale);
          return {
            ...item,
            title: translated,
            messages: localizedMessages(item, locale).map((message) => ({
              ...message,
              // Browser object URLs expire between sessions, so file previews stay session-only.
              attachments: undefined,
            })),
          };
        });

        if (localStorage.getItem(seededKey) !== "true") {
          const existingIds = new Set(conversations.map((conversation) => conversation.id));
          const demoConversations = makeStarterConversations(locale).filter((conversation) =>
            demoChats.some((demo) => demo.id === conversation.id) && !existingIds.has(conversation.id),
          );
          localStorage.setItem(seededKey, "true");
          return [...conversations, ...demoConversations];
        }
        return conversations;
      }
    }
    localStorage.setItem(seededKey, "true");
  } catch {
    // Storage may be disabled or contain an old, invalid value.
  }
  return makeStarterConversations(locale);
}

function downloadFile(name: string, content: string, type = "application/json") {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function safeTitle(value: string) {
  const title = value.trim().replace(/\s+/g, " ");
  return title.length > 48 ? `${title.slice(0, 47).trimEnd()}…` : title;
}

// The glass panel's footprint; shared by the dimming ring, the panel and its positioning.
const panelInset = (collapsed: boolean, rtl: boolean) =>
  `absolute inset-0 ${collapsed ? "" : rtl ? "lg:right-[325px]" : "lg:left-[325px]"}`;

// Whole-UI scale on desktop (1 = the 1920px artboard's own sizes); 0.68 matches viewing it at 80% browser zoom.
const BASE_SCALE = 0.68;
const DESKTOP_MIN = 1024;
// Desktop sidebar is enlarged relative to the artboard (283px × 1.15 ≈ 325px).
const SIDEBAR_ZOOM = 1.15;

function useUiScale() {
  const compute = () =>
    window.innerWidth >= DESKTOP_MIN ? BASE_SCALE * Math.max(1, window.innerWidth / 1920) : 1;
  const [scale, setScale] = useState(compute);
  useEffect(() => {
    const onResize = () => setScale(compute());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return scale;
}

// The backend is text-only, so attachments are mentioned by name.
function toChatHistory(messages: Message[]): ChatMessage[] {
  const instructions = customInstructions();
  return [
    ...(instructions ? [{ role: "system" as const, content: instructions }] : []),
    ...messages.map((message) => ({
      role: message.role,
      content: message.attachments?.length
        ? `${message.content}

[Attached: ${message.attachments.map((attachment) => attachment.name).join(", ")}]`
        : message.content,
    })),
  ];
}

export default function App() {
  const [account, setAccount] = useState<AuthAccount | null>(restoreAuthSession);

  const login = (username: string, password: string) => {
    const authenticated = authenticateUser(username, password);
    if (!authenticated) return false;
    saveAuthSession(authenticated.username);
    setAccount(authenticated);
    return true;
  };

  const logout = () => {
    saveAuthSession(null);
    setAccount(null);
  };

  if (!account) return <LoginScreen onLogin={login} />;
  return <ChatWorkspace key={account.username} account={account} onLogout={logout} />;
}

function ChatWorkspace({ account, onLogout }: { account: AuthAccount; onLogout: () => void }) {
  const { locale, t } = useLocale();
  const rtl = locale === "fa";
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarSearchKey, setSidebarSearchKey] = useState(0);
  const [drawerSearchKey, setDrawerSearchKey] = useState(0);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const drawerWasOpen = useRef(false);
  const [activeId, setActiveId] = useState<string>();
  const [collapsed, setCollapsed] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>(() => readConversations(locale, account.username));
  const [thinkingIds, setThinkingIds] = useState<string[]>([]);
  const [regeneratingMessageIds, setRegeneratingMessageIds] = useState<string[]>([]);
  const [view, setView] = useState<"chat" | "settings">("chat");
  const [dialog, setDialog] = useState<ActionDialogState | null>(null);
  const [toast, setToast] = useState("");
  const [profileName, setProfileName] = useState(() => {
    try { return localStorage.getItem(profileNameKey(account.username)) || account.name; } catch { return account.name; }
  });
  const toastTimer = useRef<number | undefined>(undefined);
  const pendingRequests = useRef<AbortController[]>([]);
  const scale = useUiScale();
  const { background, blur } = useBackground();

  const openSettings = () => setView("settings");
  const notify = (message: string) => {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 3200);
  };
  const notifyReplyError = (error: Error) => {
    if (/quota exceeded|current quota|free_tier_requests/i.test(error.message)) {
      notify(t("apiQuotaExceeded"));
      return;
    }
    notify(`${t("replyFailed")} ${error.message}`);
  };

  // ---- WorkLoop: every chat message is a prompt; replies show the live run. ----
  const [runState, setRunState] = useState<RunState>({ state: "idle", run_id: null, prompt_id: null, round: null, max_rounds: 6 });
  const conversationsRef = useRef(conversations);
  conversationsRef.current = conversations;
  /** Point the backend at a chat's project before it runs (only possible while no run is active). */
  const applyChatProject = async (path: string | undefined) => {
    if (!path || runStateRef.current.state !== "idle") return;
    const current = await workloop.project().catch(() => null);
    if (current?.path === path) return;
    setActiveProject(await workloop.setProject(path));
  };
  const runStateRef = useRef(runState);
  runStateRef.current = runState;
  const demoAbort = useRef<AbortController | null>(null);
  const demoPermissions = useRef(new Map<number, (approved: boolean) => void>());
  const updateTrace = (promptId: number, fn: (trace: RunTrace) => RunTrace) => {
    setConversations((current) => {
      let changed = false;
      const next = current.map((conversation) => {
        if (!conversation.messages.some((m) => m.run?.promptId === promptId)) return conversation;
        changed = true;
        return {
          ...conversation,
          updatedAt: Date.now(),
          messages: conversation.messages.map((m) => m.run?.promptId === promptId ? { ...m, run: fn(m.run) } : m),
        };
      });
      return changed ? next : current;
    });
  };

  useEffect(() => subscribeLive((event) => {
    if (event.type === "_connection" || event.type === "queue_empty") return;
    if (event.type === "hello" || event.type === "run_state") {
      if (demoAbort.current) return; // a demo owns the run state while it plays
      const state = event.type === "hello" ? event.data.run_state : event.data;
      runStateRef.current = state; setRunState(state); return;
    }
    const promptId = eventPromptId(event, runStateRef.current.prompt_id);
    if (promptId == null) return;
    updateTrace(promptId, (trace) => applyEvent(trace, event));
  }), []);

  /** Add a user message and an assistant run reply for an already-queued prompt. */
  const appendExchange = (conversationId: string, content: string, promptId: number) => {
    const userMessage: Message = { id: crypto.randomUUID(), role: "user", content };
    const reply: Message = {
      id: crypto.randomUUID(), role: "assistant", content: "", replyToId: userMessage.id,
      run: { promptId, status: "pending", roundsUsed: 0, items: [], lang: promptLang(content) },
    };
    setConversations((current) => current.map((c) => c.id === conversationId
      ? { ...c, updatedAt: Date.now(), messages: [...c.messages, userMessage, reply] } : c));
  };

  const runActions: RunActions = {
    currentPromptId: runState.prompt_id,
    runActive: runState.state !== "idle",
    onPermission: async (id, approved) => {
      const answerDemo = demoPermissions.current.get(id);
      if (answerDemo) { demoPermissions.current.delete(id); answerDemo(approved); return; }
      try { await workloop.answerPermission(id, approved); } catch (e) { notify(explainError(e, locale)); }
    },
    onFixPrompt: async (text) => {
      const failedId = runState.prompt_id;
      try {
        const res = await workloop.fixPrompt(text);
        const conversation = conversations.find((c) => c.messages.some((m) => m.run?.promptId === failedId));
        if (failedId != null) updateTrace(failedId, (trace) => ({ ...trace, needsUser: undefined }));
        if (conversation) appendExchange(conversation.id, text, res.prompt.id);
      } catch (e) { notify(explainError(e, locale)); }
    },
    onManualFix: async (note) => {
      try { await workloop.manualFix(note || undefined); } catch (e) { notify(explainError(e, locale)); }
    },
    onStop: async () => {
      if (demoAbort.current) { demoAbort.current.abort(); return; }
      try { await workloop.stopRun(); } catch (e) { notify(explainError(e, locale)); }
    },
  };

  useEffect(() => {
    try {
      const serializable = conversations.map(({ messages: items, ...conversation }) => ({
        ...conversation,
        messages: items.map(({ attachments: _attachments, ...message }) => message),
      }));
      localStorage.setItem(chatStorageKey(account.username), JSON.stringify(serializable));
    } catch {
      // Keep the current session usable when browser storage is full or unavailable.
    }
  }, [conversations, account.username]);

  useEffect(() => () => {
    window.clearTimeout(toastTimer.current);
    pendingRequests.current.forEach((request) => request.abort());
  }, []);

  useEffect(() => {
    if (!drawerOpen) {
      if (drawerWasOpen.current && view !== "settings") {
        window.requestAnimationFrame(() => menuButtonRef.current?.focus());
      }
      drawerWasOpen.current = false;
      return;
    }

    drawerWasOpen.current = true;
    const focusFrame = window.requestAnimationFrame(() => {
      drawerRef.current?.querySelector<HTMLElement>('button:not([disabled]), a[href], input:not([disabled])')?.focus();
    });
    const handleDrawerKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDrawerOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = drawerRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleDrawerKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleDrawerKeyDown);
    };
  }, [drawerOpen, view]);

  const messages = useMemo(() => {
    const conversation = conversations.find((item) => item.id === activeId);
    if (!conversation) return [];
    return localizedMessages(conversation, locale);
  }, [conversations, activeId, locale, t]);
  const [savedProjects, setSavedProjects] = useState(readProjects);
  const [activeProjectPath, setActiveProjectPath] = useState(() => getActiveProject()?.path ?? null);
  useEffect(() => {
    const onList = () => setSavedProjects(readProjects());
    const onActive = () => setActiveProjectPath(getActiveProject()?.path ?? null);
    window.addEventListener(PROJECTS_CHANGED, onList);
    window.addEventListener(ACTIVE_PROJECT_CHANGED, onActive);
    return () => { window.removeEventListener(PROJECTS_CHANGED, onList); window.removeEventListener(ACTIVE_PROJECT_CHANGED, onActive); };
  }, []);
  const projectGroups: ChatGroup[] = useMemo(() => savedProjects.map((project) => ({
    id: `project:${project.path}`,
    label: project.name,
    chats: conversations
      .filter((conversation) => conversation.projectPath === project.path && conversation.groupId !== "archive")
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 5)
      .map((conversation) => ({ ...conversation, title: localizedTitle(conversation.id, conversation.title, locale) })),
  })), [savedProjects, conversations, locale]);
  const selectProject = async (path: string) => {
    try {
      const info = await workloop.setProject(path);
      setActiveProject(info);
      setView("chat");
      setActiveId(undefined);
    } catch (e) { notify(explainError(e, locale)); }
  };
  const newProject = () => { setView("chat"); window.dispatchEvent(new Event(OPEN_NEW_PROJECT)); };

  // Archived chats stay out of the sidebar history.
  const groups: ChatGroup[] = useMemo(() => chatGroups.filter((group) => group.id !== "archive").map((group) => ({
    ...group,
    label: t(group.id),
    chats: conversations.filter((conversation) => conversation.groupId === group.id)
      .map((conversation) => ({ ...conversation, title: localizedTitle(conversation.id, conversation.title, locale) })),
  })), [conversations, locale, t]);

  /**
   * Queue a prompt and start a run (or play the demo). `attach` puts the run on a reply:
   * send() adds a new reply, regenerate replaces the existing one.
   */
  const launchRun = (id: string, content: string, promptText: string, attach: (reply: Partial<Message>) => void) => {
    const addReply = attach;
    const request = new AbortController();
    pendingRequests.current.push(request);
    (async () => {
      if (isDemo()) {
        if (demoAbort.current) throw new Error(locale === "fa" ? "یک دمو در حال پخش است." : "A demo is already playing.");
        const promptId = -Date.now(); // negative: never clashes with real prompt ids
        addReply({ run: { promptId, status: "pending", roundsUsed: 0, items: [], lang: promptLang(content) } });
        setThinkingIds((current) => current.filter((thinkingId) => thinkingId !== id)); // the trace shows progress
        const abort = new AbortController();
        demoAbort.current = abort;
        const running: RunState = { state: "running", run_id: null, prompt_id: promptId, round: null, max_rounds: 6 };
        runStateRef.current = running; setRunState(running);
        try {
          await runDemo(promptId, content, {
            emit: (event) => updateTrace(promptId, (trace) => applyEvent(trace, event)),
            askPermission: (permissionId) => new Promise<boolean>((resolve) => demoPermissions.current.set(permissionId, resolve)),
            signal: abort.signal,
          });
        } finally {
          demoAbort.current = null;
          demoPermissions.current.clear();
          const idle: RunState = { state: "idle", run_id: null, prompt_id: null, round: null, max_rounds: 6 };
          runStateRef.current = idle; setRunState(idle);
        }
        return;
      }
      // Run in the chat's own project, not whatever folder happens to be active.
      await applyChatProject(conversationsRef.current.find((c) => c.id === id)?.projectPath ?? getActiveProject()?.path ?? undefined);
      const prompt = await workloop.createPrompt(promptText);
      addReply({ run: { promptId: prompt.id, status: "pending", roundsUsed: 0, items: [], lang: promptLang(content) } });
      if (runStateRef.current.state !== "idle") return; // the active run takes it from the queue
      try {
        try {
          await workloop.startRun();
        } catch (e) {
          // No project folder yet: chat in the default workspace folder and try once more.
          const code = (e as { code?: string }).code;
          if (code !== "PROJECT_NOT_SET" && code !== "PROJECT_NOT_FOUND") throw e;
          setActiveProject(await workloop.useScratchProject());
          await workloop.startRun();
        }
      } catch (e) {
        if ((e as { code?: string }).code === "RUN_ACTIVE") return;
        // Nothing will run it: take it back out of the queue and show why.
        await workloop.deletePrompt(prompt.id).catch(() => undefined);
        updateTrace(prompt.id, (trace) => ({ ...trace, status: "error", items: [...trace.items, { kind: "note", tone: "error", text: explainError(e, locale) }] }));
      }
    })()
      .catch((e) => addReply({ content: explainError(e, locale) }))
      .finally(() => {
        setThinkingIds((current) => current.filter((thinkingId) => thinkingId !== id));
        pendingRequests.current = pendingRequests.current.filter((pending) => pending !== request);
      });
  };

  /** Run a WorkLoop reply's prompt again; the reply is replaced by the new run. */
  const regenerateRun = (messageId: string) => {
    if (!activeId || pendingRequests.current.length > 0) return;
    const conversation = conversations.find((c) => c.id === activeId);
    const reply = conversation?.messages.find((m) => m.id === messageId);
    if (!conversation || !reply?.run) return;
    const stuckInQueue = reply.run.status === "pending" && runStateRef.current.state === "idle";
    if (reply.run.status === "running" || (reply.run.status === "pending" && !stuckInQueue)) {
      notify(locale === "fa" ? "این اجرا هنوز تمام نشده." : "This run hasn't finished yet.");
      return;
    }
    // A prompt left queued with nothing running it: take it out before running again.
    if (stuckInQueue && reply.run.promptId > 0) void workloop.deletePrompt(reply.run.promptId).catch(() => undefined);
    const prompt = conversation.messages.find((m) => m.id === reply.replyToId)
      ?? [...conversation.messages.slice(0, conversation.messages.indexOf(reply))].reverse().find((m) => m.role === "user");
    if (!prompt) return;
    const conversationId = conversation.id;
    const replace = (patch: Partial<Message>) => setConversations((current) => current.map((c) => c.id !== conversationId ? c : {
      ...c,
      updatedAt: Date.now(),
      messages: c.messages.map((m) => m.id === messageId
        ? { ...m, content: "", run: undefined, ...patch, ...(patch.run ? { run: { ...patch.run, regenerated: true } } : {}) }
        : m),
    }));
    setThinkingIds((current) => current.includes(conversationId) ? current : [...current, conversationId]);
    launchRun(conversationId, prompt.content, prompt.content, replace);
  };

  const send = (content: string, attachments: Attachment[]) => {
    if (pendingRequests.current.length > 0) return;
    const id = activeId ?? crypto.randomUUID();
    const userMessage: Message = { id: crypto.randomUUID(), role: "user", content, attachments };
    setActiveId(id);
    setView("chat");
    setConversations((current) => {
      const exists = current.some((conversation) => conversation.id === id);
      const next = current.map((conversation) => conversation.id === id
        ? { ...conversation, groupId: conversation.groupId === "archive" ? "today" : conversation.groupId, updatedAt: Date.now(), messages: [...conversation.messages, userMessage] }
        : conversation);
      return exists ? next : [{
        id,
        title: safeTitle(content) || t("newChat"),
        groupId: "today",
        projectPath: getActiveProject()?.path ?? undefined,
        updatedAt: Date.now(),
        messages: [userMessage],
      }, ...next];
    });
    setThinkingIds((current) => current.includes(id) ? current : [...current, id]);
    // Attachments can't reach the agents; mention them by name in the prompt.
    const promptText = attachments.length
      ? `${content}\n\n[Attached: ${attachments.map((a) => a.name).join(", ")}]` : content;
    const addReply = (reply: Partial<Message>) => setConversations((current) => current.map((conversation) => conversation.id === id
      ? { ...conversation, updatedAt: Date.now(), messages: [...conversation.messages, { id: crypto.randomUUID(), role: "assistant", content: "", replyToId: userMessage.id, ...reply }] }
      : conversation));
    launchRun(id, content, promptText, addReply);
  };

  const selectChat = (id: string) => {
    setView("chat");
    setActiveId(id);
    // Opening a chat makes its project the active one (shown under the prompt box).
    void applyChatProject(conversations.find((c) => c.id === id)?.projectPath).catch((e) => notify(explainError(e, locale)));
  };

  const newChat = () => {
    setView("chat");
    setActiveId(undefined);
  };

  const handleChatAction = (id: string, action: ChatAction) => {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) return;
    if (action === "rename") {
      setDialog({ type: "rename", id, title: localizedTitle(id, conversation.title, locale) });
    } else if (action === "pin") {
      const nextGroup = conversation.groupId === "pinned" ? "today" : "pinned";
      setConversations((current) => current.map((item) => item.id === id ? { ...item, groupId: nextGroup } : item));
      notify(t(nextGroup === "pinned" ? "chatPinned" : "chatUnpinned"));
    } else if (action === "archive") {
      const restoring = conversation.groupId === "archive";
      setConversations((current) => current.map((item) => item.id === id ? { ...item, groupId: restoring ? "today" : "archive" } : item));
      notify(t(restoring ? "chatRestored" : "chatArchived"));
    } else if (action === "export") {
      downloadFile(`${safeTitle(conversation.title).replace(/[^a-z0-9-_]+/gi, "-") || "chat"}.json`, JSON.stringify(conversation, null, 2));
      notify(t("chatExported"));
    } else if (action === "share") {
      const body = localizedMessages(conversation, locale).map((message) => `${message.role === "user" ? t("you") : t("assistant")}: ${message.content}`).join("\n\n");
      setDialog({ type: "share", title: localizedTitle(id, conversation.title, locale), content: body });
    } else if (action === "delete") {
      setDialog({ type: "delete", id, title: localizedTitle(id, conversation.title, locale) });
    }
  };

  const renameChat = (id: string, title: string) => {
    setConversations((current) => current.map((item) => item.id === id ? { ...item, title, updatedAt: Date.now() } : item));
    setDialog(null);
    notify(t("chatRenamed"));
  };

  const regenerateAnswer = (messageId: string, promptOverride?: string, conversationId = activeId) => {
    if (!conversationId) return;
    const conversation = conversations.find((item) => item.id === conversationId);
    const replyIndex = conversation?.messages.findIndex((message) => message.id === messageId) ?? -1;
    if (!conversation || replyIndex < 0 || conversation.messages[replyIndex].role !== "assistant") return;
    const reply = conversation.messages[replyIndex];
    const history = localizedMessages(conversation, locale).slice(0, replyIndex);
    if (promptOverride !== undefined) {
      const promptIndex = reply.replyToId
        ? history.findIndex((message) => message.id === reply.replyToId)
        : history.map((message) => message.role).lastIndexOf("user");
      if (promptIndex >= 0) history[promptIndex] = { ...history[promptIndex], content: promptOverride };
    }
    setRegeneratingMessageIds((current) => current.includes(messageId) ? current : [...current, messageId]);
    const request = new AbortController();
    pendingRequests.current.push(request);
    sendChat(selectedModel(), toChatHistory(history), request.signal)
      .then((result) => {
        setConversations((current) => current.map((item) => item.id !== conversationId
          ? item
          : {
            ...item,
            updatedAt: Date.now(),
            messages: item.messages.map((message) => message.id !== messageId
              ? message
              : { ...message, content: result.message, previewPrompt: undefined, previewGeneration: undefined }),
          }));
        notify(t("responseRegenerated"));
      })
      .catch((error: Error) => {
        if (error.name !== "AbortError") notifyReplyError(error);
      })
      .finally(() => {
        setRegeneratingMessageIds((current) => current.filter((id) => id !== messageId));
        pendingRequests.current = pendingRequests.current.filter((pending) => pending !== request);
      });
  };


  const deleteChat = (id: string) => {
    setConversations((current) => current.filter((item) => item.id !== id));
    setThinkingIds((current) => current.filter((thinkingId) => thinkingId !== id));
    if (activeId === id) setActiveId(undefined);
    setDialog(null);
    notify(t("chatDeleted"));
  };

  const exportAll = () => {
    downloadFile(t("chatDataFile"), JSON.stringify({ exportedAt: new Date().toISOString(), conversations }, null, 2));
    notify(t("dataExported"));
  };

  const requestClearHistory = () => setDialog({ type: "clear-history" });

  const clearHistory = () => {
    setConversations([]);
    setThinkingIds([]);
    setActiveId(undefined);
    setDialog(null);
    notify(t("historyCleared"));
  };

  const copyShareText = async (content: string) => {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(content);
    setDialog(null);
    notify(t("copiedText"));
  };

  const copyMessage = async (message: Message) => {
    const content = (message.run ? traceToText(message.run) : "") || message.content || message.attachments?.map((attachment) => attachment.name).join(", ") || "";
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(content);
      notify(t("copiedText"));
    } catch {
      notify(t("copyUnavailable"));
    }
  };

  const shareMessage = async (message: Message) => {
    const title = t(message.role === "user" ? "prompt" : "answer");
    const content = (message.run ? traceToText(message.run) : "") || message.content || message.attachments?.map((attachment) => attachment.name).join(", ") || "";
    if (navigator.share) {
      try {
        await navigator.share({ title, text: content });
        return;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
    }
    setDialog({ type: "share", title, content });
  };

  const downloadShareText = (title: string, content: string) => {
    const fileName = `${safeTitle(title).replace(/[^a-z0-9-_]+/gi, "-") || "chat"}.txt`;
    downloadFile(fileName, `${title}\n\n${content}`, "text/plain");
    setDialog(null);
    notify(t("downloadedText"));
  };

  const closeDrawer = () => setDrawerOpen(false);
  const PANEL_INSET = panelInset(collapsed, rtl);

  return (
    <div
      id="app-shell"
      dir={rtl ? "rtl" : "ltr"}
      lang={locale}
      className="relative min-h-0 overflow-hidden bg-ink text-white"
      style={{
        zoom: scale,
        width: `${100 / scale}vw`,
        height: `${100 / scale}dvh`,
        boxSizing: "border-box",
        padding: "env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)",
      }}
    >
      {/* Background image; oversized so the blur doesn't feather at the edges. */}
      <div
        aria-hidden="true"
        className="app-bg pointer-events-none absolute -inset-[6%] bg-cover bg-center transition-[filter] duration-300"
        style={{
          backgroundImage: `url(${background.src})`,
          filter: blur ? `blur(${blur}px)` : undefined,
        }}
      />
      {/* Dims everything outside the panel. */}
      <div
        aria-hidden="true"
        className={`app-dim ${PANEL_INSET} pointer-events-none shadow-[0_0_0_200vmax_color-mix(in_srgb,var(--color-ink)_70%,transparent)]`}
      />

      {!collapsed && (
        <aside className={`absolute inset-y-0 z-10 hidden w-[325px] lg:block ${rtl ? "right-0" : "left-0"}`}>
          <div
            className={rtl ? "origin-top-right" : "origin-top-left"}
            style={{
              width: 283,
              height: `${100 / SIDEBAR_ZOOM}%`,
              transform: `scale(${SIDEBAR_ZOOM})`,
            }}
          >
            <Sidebar
              projectGroups={projectGroups}
              activeProjectPath={activeProjectPath}
              onSelectProject={selectProject}
              onNewProject={newProject}
              activeId={activeId}
              groups={groups}
              focusSearchKey={sidebarSearchKey}
              name={profileName}
              onChatAction={handleChatAction}
              onSelect={selectChat}
              onNewChat={newChat}
              onToggleSidebar={() => setCollapsed(true)}
              onOpenSettings={openSettings}
              onLogout={onLogout}
            />
          </div>
        </aside>
      )}

      <main inert={drawerOpen} className={`${PANEL_INSET} glass rounded-none !border-y-0 ${rtl ? "!border-r !border-l-0" : "!border-l !border-r-0"} ${collapsed ? "!border-0" : ""}`}>
        <div className={`absolute top-3 z-10 flex flex-col items-center gap-0.5 rounded-full glass p-1 lg:hidden ${rtl ? "right-3" : "left-3"}`}>
          <button
            ref={menuButtonRef}
            id="main-menu-button"
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label={t("openSidebar")}
            aria-expanded={drawerOpen}
            aria-controls="chat-history-drawer"
            title={t("openSidebar")}
            className="flex size-10 shrink-0 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
          >
            <PanelLeft className="size-5" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => {
              setDrawerOpen(true);
              setDrawerSearchKey((key) => key + 1);
            }}
            aria-label={t("searchChats")}
            aria-expanded={drawerOpen}
            aria-controls="chat-history-drawer"
            title={t("searchChats")}
            className="flex size-10 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
          >
            <Search className="size-5" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={newChat}
            aria-label={t("newChat")}
            title={t("newChat")}
            className="flex size-10 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
          >
            <CirclePlus className="size-5" aria-hidden="true" />
          </button>
        </div>
        {collapsed && (
          <div className={`absolute top-4 z-10 hidden items-center gap-0.5 rounded-full glass p-1 lg:flex ${rtl ? "right-5" : "left-5"}`}>
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              aria-label={t("openSidebar")}
              title={t("openSidebar")}
              className="flex size-12 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
            >
              <PanelLeft className="size-5" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => {
                setCollapsed(false);
                setSidebarSearchKey((key) => key + 1);
              }}
              aria-label={t("searchChats")}
              title={t("searchChats")}
              className="flex size-12 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
            >
              <Search className="size-5" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={newChat}
              aria-label={t("newChat")}
              title={t("newChat")}
              className="flex size-12 cursor-pointer items-center justify-center rounded-full text-white/75 transition-colors hover:bg-white/10 hover:text-white active:scale-95"
            >
              <CirclePlus className="size-5" aria-hidden="true" />
            </button>
          </div>
        )}
        <ChatPanel
          messages={messages}
          thinking={activeId ? thinkingIds.includes(activeId) : false}
          busy={pendingRequests.current.length > 0}
          regeneratingMessageIds={regeneratingMessageIds}
          onSend={send}
          onCopyMessage={copyMessage}
          onShareMessage={shareMessage}
          onRegenerateAnswer={regenerateAnswer}
          onRegenerateRun={regenerateRun}
          runActions={runActions}
        />
      </main>

      {/* Mobile / tablet drawer: always mounted, toggled with CSS transitions. */}
      <button
        type="button"
        aria-label={t("closeHistory")}
        aria-hidden={!drawerOpen}
        tabIndex={drawerOpen ? 0 : -1}
        onClick={closeDrawer}
        className={`absolute inset-0 z-20 bg-black/50 transition-opacity duration-200 lg:hidden ${
          drawerOpen ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      <aside
        ref={drawerRef}
        id="chat-history-drawer"
        aria-label={t("history")}
        role={drawerOpen ? "dialog" : undefined}
        aria-hidden={!drawerOpen}
        aria-modal={drawerOpen}
        inert={!drawerOpen}
        className={`absolute inset-y-0 z-30 w-[300px] max-w-[calc(100vw_-_16px)] glass-pop transition-transform duration-250 ease-out lg:hidden ${
          rtl ? `right-0 ${drawerOpen ? "translate-x-0" : "translate-x-full"}` : `left-0 ${drawerOpen ? "translate-x-0" : "-translate-x-full"}`
        }`}
      >
        <Sidebar
              projectGroups={projectGroups}
              activeProjectPath={activeProjectPath}
              onSelectProject={selectProject}
              onNewProject={newProject}
          groups={groups}
          focusSearchKey={drawerSearchKey}
          name={profileName}
          onChatAction={handleChatAction}
          onToggleSidebar={closeDrawer}
          onOpenSettings={() => {
            openSettings();
            closeDrawer();
          }}
          onLogout={onLogout}
          activeId={activeId}
          onSelect={(id) => {
            selectChat(id);
            closeDrawer();
          }}
          onNewChat={() => {
            newChat();
            closeDrawer();
          }}
        />
      </aside>
      {view === "settings" && (
        <SettingsPanel
          onLogout={onLogout}
          onClose={() => setView("chat")}
          onNotify={notify}
          onExportData={exportAll}
          onClearHistory={requestClearHistory}
          onProfileChange={(name) => setProfileName(name)}
        />
      )}
      {toast && (
        <div role="status" className="fixed bottom-16 left-1/2 z-50 w-fit max-w-[min(42rem,calc(100vw-2rem))] -translate-x-1/2 break-words rounded-xl glass-pop px-4 py-3 text-center text-sm text-white [overflow-wrap:anywhere]">
          {toast}
        </div>
      )}
      <a
        href="https://matinzeinali.ir/"
        target="_blank"
        rel="noreferrer"
        dir={locale === "fa" ? "rtl" : "ltr"}
        aria-label={`${t("madeBy")} matinzeinali.ir`}
        className={`absolute bottom-1.5 left-1/2 z-[5] hidden w-[325px] -translate-x-1/2 items-center justify-center gap-1.5 whitespace-nowrap text-[14px] font-medium leading-5 text-white/90 [text-shadow:0_1px_4px_rgb(0_0_0/0.9)] transition hover:text-white ${collapsed ? "" : "lg:inline-flex"}`}
      >
        <span>{t("madeBy")}</span>
        <span dir="ltr" className="font-semibold tracking-[0.01em] underline decoration-transparent underline-offset-2 transition hover:decoration-white/75">matinzeinali.ir</span>
      </a>
      {dialog && (
        <ActionDialog
          dialog={dialog}
          onClose={() => setDialog(null)}
          onRename={renameChat}
          onDelete={deleteChat}
          onClearHistory={clearHistory}
          onCopy={copyShareText}
          onDownload={downloadShareText}
        />
      )}
    </div>
  );
}
