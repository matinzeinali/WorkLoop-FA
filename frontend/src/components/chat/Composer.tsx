import { useEffect, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { ArrowUp, Plus, Square } from "lucide-react";
import {
  ACCEPTED_TYPES,
  MAX_ATTACHMENTS,
  MAX_FILE_SIZE,
  type Attachment,
} from "../../data/messages";
import AttachmentPreview from "./AttachmentPreview";
import ProjectPicker from "./ProjectPicker";
import { isDemo } from "../../lib/demo";
import { useLocale } from "../../context/LocaleContext";

interface Props {
  compact?: boolean;
  thinking?: boolean;
  busy?: boolean;
  onSend?: (message: string, attachments: Attachment[]) => void;
  /** Set while a run is active: the send button becomes a stop button. */
  onStop?: () => void;
}

const toolButton =
  "flex size-9 cursor-pointer items-center justify-center rounded-full text-white/75 transition-[background-color,color,transform] duration-200 hover:bg-white/10 hover:text-white active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent";

export default function Composer({ compact, thinking = false, busy = false, onSend, onStop }: Props) {
  const { t, locale } = useLocale();
  const reduceMotion = useReducedMotion();
  const [value, setValue] = useState("");
  const [demo, setDemoState] = useState(isDemo);
  useEffect(() => {
    const update = () => setDemoState(isDemo());
    window.addEventListener("workloop:demo", update);
    return () => window.removeEventListener("workloop:demo", update);
  }, []);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string>();
  const [isMobile, setIsMobile] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const dragDepth = useRef(0);


  const canSend = value.trim().length > 0 || attachments.length > 0;
  const requestPending = busy || thinking;
  const showLoading = requestPending;

  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const update = () => setIsMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  const fitHeight = () => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
    // Only scroll once the text outgrows the max height; sub-pixel rounding under
    // the desktop UI scale otherwise shows a scrollbar on a single line.
    textarea.style.overflowY = textarea.scrollHeight > 160 ? "auto" : "hidden";
  };
  useEffect(fitHeight, [value]);
  // Re-measure when the width changes: on first layout the box can be measured before
  // it has its final width, which wraps the text and leaves it stuck at full height.
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea || typeof ResizeObserver === "undefined") return;
    let lastWidth = textarea.offsetWidth;
    const observer = new ResizeObserver(() => {
      if (textarea.offsetWidth !== lastWidth) { lastWidth = textarea.offsetWidth; fitHeight(); }
    });
    observer.observe(textarea);
    return () => observer.disconnect();
  }, []);

  const addFiles = (files: FileList | File[]) => {
    setError(undefined);
    const next: Attachment[] = [];
    let rejected: string | undefined;
    for (const file of Array.from(files)) {
      if (!ACCEPTED_TYPES.includes(file.type)) {
        rejected = `${file.name}: ${t("onlyFiles")}`;
      } else if (file.size > MAX_FILE_SIZE) {
        rejected = `${file.name}: ${t("tooLarge")}`;
      } else if (attachments.length + next.length >= MAX_ATTACHMENTS) {
        rejected = t("maxFiles");
      } else {
        next.push({
          id: crypto.randomUUID(),
          name: file.name,
          size: file.size,
          type: file.type,
          url: URL.createObjectURL(file),
        });
      }
    }
    if (rejected) setError(rejected);
    if (next.length) setAttachments((prev) => [...prev, ...next]);
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((a) => a.id !== id);
    });
  };

  // Release preview URLs of unsent attachments on unmount.
  const pending = useRef(attachments);
  pending.current = attachments;
  useEffect(() => () => pending.current.forEach((a) => URL.revokeObjectURL(a.url)), []);

  const submit = () => {
    if (!canSend || requestPending) return;
    onSend?.(value.trim(), attachments); // sent attachments keep their URLs for display in the chat
    setValue("");
    setAttachments([]);
    setError(undefined);
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  // Enter sends, Shift+Enter inserts a newline.
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const handleFileInput = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addFiles(e.target.files);
    e.target.value = "";
  };

  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer.types).includes("Files");

  const onDragEnter = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  };
  const onDragLeave = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  };
  const onDrop = (e: DragEvent) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    addFiles(e.dataTransfer.files);
  };


  return (
    <div className={compact ? "w-full" : "w-full rounded-[26px] glass-light transition-[border-color,box-shadow] duration-200 focus-within:!border-white/30 focus-within:!shadow-[0_0_0_4px_rgb(255_255_255/0.06),0_18px_50px_rgb(0_0_0/0.28)]"}>
      <form
        onSubmit={handleSubmit}
        onDragEnter={onDragEnter}
        onDragOver={(e) => hasFiles(e) && e.preventDefault()}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={`relative z-10 flex w-full flex-col text-white transition-colors ${compact ? "rounded-[24px] glass-light focus-within:border-white/35" : "rounded-t-[26px]"} ${
          dragging ? "!border-dashed !border-white/60" : ""
        }`}
      >
        {dragging && (
          <div className="light-dark-overlay pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-[24px] bg-black/40 text-[15px] font-medium text-white">
            {t("dropFiles")}
          </div>
        )}

        {attachments.length > 0 && (
          <ul aria-label={t("attachments")} className="m-0 flex list-none gap-3 overflow-x-auto px-4 pb-1 pt-3.5">
            {attachments.map((a) => (
              <li key={a.id}>
                <AttachmentPreview attachment={a} onRemove={() => removeAttachment(a.id)} />
              </li>
            ))}
          </ul>
        )}

        {/* Compact (chat has messages): one line — [+] [text] [mic][send]. Otherwise text above, tools below. */}
        <div className={compact ? "flex min-h-[58px] items-end gap-1.5 px-2.5 py-2.5" : "flex flex-col"}>
        <label htmlFor="prompt" className="sr-only">{t("message")}</label>
        <textarea
          ref={textareaRef}
          id="prompt"
          dir={value ? "auto" : locale === "fa" ? "rtl" : "ltr"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isMobile ? t("mobileMessagePlaceholder") : t("messagePlaceholder")}
          rows={compact ? 1 : 2}
          className={`thin-scroll max-h-40 resize-none bg-transparent text-[16px] leading-7 text-white caret-white outline-none placeholder:text-white/40 focus-visible:outline-none ${
            compact ? "order-2 min-w-0 flex-1 self-center px-2 py-1.5" : "w-full px-5 pt-4"
          }`}
        />

        <div className={compact ? "contents" : "flex items-center gap-1 px-2.5 pb-2.5 pt-1.5"}>
          <input ref={fileInputRef} type="file" multiple accept={ACCEPTED_TYPES.join(",")} onChange={handleFileInput} className="hidden" tabIndex={-1} />
          <button type="button" aria-label={t("attachFiles")} title={t("attachFiles")} className={`${toolButton} ${compact ? "order-1 shrink-0" : ""}`} onClick={() => fileInputRef.current?.click()}>
            <Plus className="size-5" aria-hidden="true" />
          </button>

          <div className={`flex items-center gap-1 ${compact ? "order-3 shrink-0" : "ms-auto"}`}>
            {onStop ? (
            <button
              type="button"
              onClick={onStop}
              aria-label={locale === "fa" ? "توقف اجرا" : "Stop run"}
              title={locale === "fa" ? "توقف اجرا" : "Stop run"}
              className="grid size-9 cursor-pointer place-items-center rounded-full bg-white text-ink transition-transform duration-200 hover:scale-105 active:scale-90"
            >
              <Square className="size-3.5 fill-current" aria-hidden="true" />
            </button>
            ) : (
            <button
              type="submit"
              aria-label={showLoading ? t("typing") : t("sendMessage")}
              aria-busy={showLoading}
              disabled={!canSend || requestPending}
              className={`grid size-9 cursor-pointer place-items-center rounded-full transition-[background-color,transform,opacity] duration-200 active:scale-90 disabled:cursor-not-allowed ${
                canSend && !requestPending ? "bg-white text-ink hover:scale-105" : "bg-white/20 text-white/60"
              }`}
            >
              {showLoading ? (
                <motion.span
                  aria-hidden="true"
                  animate={reduceMotion ? { rotate: 0 } : { rotate: 360 }}
                  transition={reduceMotion ? undefined : { duration: 1.2, ease: "linear", repeat: Infinity }}
                  className="size-4 rounded-full border-2 border-white/40 border-t-white"
                />
              ) : (
                <ArrowUp className="size-[18px]" strokeWidth={2.4} aria-hidden="true" />
              )}
            </button>
            )}
          </div>
        </div>
        </div>

        {error && <p role="alert" className={`m-0 text-[12px] text-red-300 ${compact ? "px-5 pb-2" : "px-5 pb-2"}`}>{error}</p>}
      </form>

      <div className={`relative z-20 flex items-center gap-2 ${compact ? "mt-1 px-2" : "border-t border-white/10 px-3 py-2"}`}>
        {demo ? (
          <span className="flex h-7 items-center gap-1.5 rounded-full border border-amber-300/25 bg-amber-300/[0.12] px-2.5 text-[12.5px] font-medium text-amber-200">
            <span className="size-1.5 animate-pulse rounded-full bg-amber-300" aria-hidden="true" />
            {locale === "fa" ? "حالت دمو" : "Demo mode"}
          </span>
        ) : <ProjectPicker />}
      </div>
    </div>
  );
}
