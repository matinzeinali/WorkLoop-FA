import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, LogOut, Menu, X } from "lucide-react";
import { createSettingsSections } from "./sections";
import { useLocale } from "../../context/LocaleContext";

interface Props {
  onClose: () => void;
  onNotify: (message: string) => void;
  onExportData: () => void;
  onClearHistory: () => void;
  onProfileChange: (name: string) => void;
  onLogout?: () => void;
}

export default function SettingsPanel({ onClose, onNotify, onExportData, onClearHistory, onProfileChange, onLogout }: Props) {
  const { locale, t } = useLocale();
  const rtl = locale === "fa";
  const settingsSections = createSettingsSections({ onNotify, onExportData, onClearHistory, onProfileChange }, t);
  const [activeId, setActiveId] = useState(settingsSections[0].id);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const active = settingsSections.find((s) => s.id === activeId) ?? settingsSections[0];
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const mobileNavRef = useRef<HTMLElement>(null);
  const mobileMenuOpenRef = useRef(mobileMenuOpen);
  const wasMobileMenuOpen = useRef(false);
  mobileMenuOpenRef.current = mobileMenuOpen;
  const previousFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appShell = document.getElementById("app-shell");
    if (appShell) appShell.inert = true;
    closeRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (event.target instanceof Element && event.target.closest("[data-settings-popup]")) return;
        event.preventDefault();
        if (mobileMenuOpenRef.current) {
          setMobileMenuOpen(false);
          menuButtonRef.current?.focus();
          return;
        }
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const insidePanel = panelRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      const settingsPopup = document.querySelector<HTMLElement>("[data-settings-popup]");
      const insidePopup = settingsPopup?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      const focusable = [...(insidePanel ?? []), ...(insidePopup ?? [])].filter(
        (element) => !element.closest("[inert]") && element.getClientRects().length > 0,
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
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (appShell) appShell.inert = false;
      if (previousFocus.current?.isConnected && !previousFocus.current.closest("[inert]")) {
        previousFocus.current.focus();
      } else {
        const accountTrigger = [...document.querySelectorAll<HTMLElement>("[data-account-menu-trigger]")]
          .find((element) => !element.closest("[inert]"));
        (accountTrigger ?? document.getElementById("main-menu-button"))?.focus();
      }
    };
  }, []);

  useEffect(() => {
    if (mobileMenuOpen) mobileNavRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    else if (wasMobileMenuOpen.current) menuButtonRef.current?.focus();
    wasMobileMenuOpen.current = mobileMenuOpen;
  }, [mobileMenuOpen]);

  return createPortal(
    <div
      dir={rtl ? "rtl" : "ltr"}
      lang={locale}
      className="fixed inset-0 z-[80] flex bg-ink md:bg-black/35"
    >
      <div
        ref={panelRef}
        role="main"
        aria-labelledby="settings-dialog-title"
        className="relative flex h-dvh w-full flex-col overflow-hidden glass text-white"
      >
        <header className="flex h-[72px] shrink-0 items-center gap-3 border-b border-white/10 px-4 md:px-7">
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label={t("backToChat")}
            className="flex size-10 cursor-pointer items-center justify-center rounded-full text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          >
            {rtl ? <ArrowRight className="size-5" aria-hidden="true" /> : <ArrowLeft className="size-5" aria-hidden="true" />}
          </button>
          <h1 id="settings-dialog-title" className="m-0 text-[22px] font-semibold text-white">{t("settings")}</h1>
          <button
            ref={menuButtonRef}
            type="button"
            onClick={() => setMobileMenuOpen((open) => !open)}
            aria-label={mobileMenuOpen ? t("closeSettingsMenu") : t("settingsMenu")}
            aria-expanded={mobileMenuOpen}
            aria-controls="mobile-settings-menu"
            className="ms-auto flex size-10 cursor-pointer items-center justify-center rounded-full text-white/80 transition-colors hover:bg-white/10 hover:text-white md:hidden"
          >
            {mobileMenuOpen ? <X className="size-5" aria-hidden="true" /> : <Menu className="size-5" aria-hidden="true" />}
          </button>
        </header>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <nav
            aria-label={t("settings")}
            className={`hidden shrink-0 gap-1 overflow-x-auto border-b border-white/10 p-3 md:flex md:w-[300px] md:flex-col md:overflow-y-auto md:border-b-0 md:p-4 ${rtl ? "md:border-l" : "md:border-r"}`}
          >
            {settingsSections.map(({ id, title, description, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setActiveId(id)}
                aria-current={id === activeId ? "page" : undefined}
                className={`flex shrink-0 cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-start transition-colors ${
                  id === activeId ? "bg-white/15 text-white" : "text-white/70 hover:bg-white/10 hover:text-white"
                }`}
              >
                <Icon className="size-5 shrink-0" aria-hidden="true" />
                <span className="min-w-0">
                  <span className="block text-[14px] font-medium">{title}</span>
                  <span className="hidden truncate text-[12px] text-white/50 md:block">{description}</span>
                </span>
              </button>
            ))}
            {onLogout && (
              <button
                type="button"
                onClick={onLogout}
                className="mt-auto flex shrink-0 cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-start text-red-300 transition-colors hover:bg-red-500/15 hover:text-red-200"
              >
                <LogOut className="size-5 shrink-0" aria-hidden="true" />
                <span className="text-[14px] font-medium">{t("logout")}</span>
              </button>
            )}
          </nav>

          <section aria-labelledby="settings-title" inert={mobileMenuOpen} className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5 md:p-8">
            <div className="mx-auto max-w-[760px] pb-10">
              <div className="mb-6 flex items-center gap-3.5">
                <span className="grid size-11 shrink-0 place-items-center rounded-2xl border border-white/10 bg-white/[0.07]">
                  <active.icon className="size-5 text-white/85" aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <h2 id="settings-title" className="m-0 text-[21px] font-semibold leading-7 text-white">{active.title}</h2>
                  <p className="m-0 text-[14px] text-white/55">{active.description}</p>
                </div>
              </div>
              {active.content}
            </div>
          </section>
        </div>
        <div className="absolute inset-x-0 bottom-0 top-[72px] z-30 md:hidden">
          <button
            type="button"
            tabIndex={mobileMenuOpen ? 0 : -1}
            aria-label={t("closeSettingsMenu")}
            onClick={() => setMobileMenuOpen(false)}
            className={`absolute inset-0 bg-black/55 transition-opacity duration-200 ${mobileMenuOpen ? "opacity-100" : "pointer-events-none opacity-0"}`}
          />
          <nav
            ref={mobileNavRef}
            id="mobile-settings-menu"
            aria-label={t("settingsMenu")}
            aria-hidden={!mobileMenuOpen}
            inert={!mobileMenuOpen}
            className={`absolute bottom-0 start-0 top-0 flex w-[min(82vw,280px)] flex-col gap-1 overflow-y-auto glass-pop p-3 transition-transform duration-200 ease-out ${
              mobileMenuOpen ? "translate-x-0" : rtl ? "translate-x-full" : "-translate-x-full"
            }`}
          >
            {settingsSections.map(({ id, title, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => {
                  setActiveId(id);
                  setMobileMenuOpen(false);
                }}
                aria-current={id === activeId ? "page" : undefined}
                className={`flex min-h-12 shrink-0 cursor-pointer items-center gap-3 rounded-xl px-3 text-start transition-colors ${
                  id === activeId ? "bg-white/15 text-white" : "text-white/75 hover:bg-white/10 hover:text-white"
                }`}
              >
                <Icon className="size-5 shrink-0" aria-hidden="true" />
                <span className="text-[14px] font-medium">{title}</span>
              </button>
            ))}
            {onLogout && (
              <button
                type="button"
                onClick={onLogout}
                className="mt-auto flex shrink-0 cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-start text-red-300 transition-colors hover:bg-red-500/15 hover:text-red-200"
              >
                <LogOut className="size-5 shrink-0" aria-hidden="true" />
                <span className="text-[14px] font-medium">{t("logout")}</span>
              </button>
            )}
          </nav>
        </div>
      </div>
    </div>,
    document.body,
  );
}
