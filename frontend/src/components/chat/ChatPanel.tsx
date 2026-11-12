import { Fragment, useEffect, useRef } from "react";
import { motion, useReducedMotion } from "framer-motion";
import type { Attachment, Message } from "../../data/messages";
import Composer from "./Composer";
import MessageBubble, { TypingIndicator } from "./MessageBubble";
import { useLocale } from "../../context/LocaleContext";
import type { RunActions } from "./RunTraceView";

interface Props {
  messages: Message[];
  thinking?: boolean;
  busy?: boolean;
  regeneratingMessageIds?: string[];
  onSend?: (message: string, attachments: Attachment[]) => void;
  onCopyMessage?: (message: Message) => void;
  onShareMessage?: (message: Message) => void;
  onEditPrompt?: (id: string, content: string) => void;
  onRegenerateAnswer?: (id: string) => void;
  onRegenerateRun?: (id: string) => void;
  runActions?: RunActions;
}

export default function ChatPanel({
  messages,
  thinking,
  busy = false,
  regeneratingMessageIds = [],
  onSend,
  onCopyMessage,
  onShareMessage,
  onEditPrompt,
  onRegenerateAnswer,
  onRegenerateRun,
  runActions,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null);
  const { t, locale } = useLocale();
  const reduceMotion = useReducedMotion();
  const fa = locale === "fa";
  const onStop = runActions?.runActive ? () => void runActions.onStop() : undefined;

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, thinking]);

  if (messages.length === 0) {
    return (
      <div className="flex size-full items-center justify-center px-5 md:px-10">
        <div className="flex w-full max-w-[840px] flex-col items-center">
          <h1
            key={locale}
            aria-label={t("welcome")}
            className={`mb-5 text-balance text-center text-white sm:mb-7 ${
              fa
                ? "text-[26px] font-bold leading-[1.5] sm:text-[40px] lg:text-[50px]"
                : "text-[28px] font-semibold leading-[1.15] tracking-[-0.035em] sm:text-[46px] lg:text-[58px]"
            }`}
          >
            {t("welcome").split(" ").map((word, index) => (
              <Fragment key={index}>
                {index > 0 && " "}
                <motion.span
                  aria-hidden="true"
                  initial={reduceMotion ? false : { opacity: 0, y: "0.35em", filter: "blur(10px)" }}
                  animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
                  transition={{ duration: 0.7, delay: 0.08 + index * 0.09, ease: [0.22, 1, 0.36, 1] }}
                  className="inline-block bg-gradient-to-b from-white via-white to-white/55 bg-clip-text pb-[0.1em] text-transparent"
                >
                  {word}
                </motion.span>
              </Fragment>
            ))}
          </h1>
          <motion.div
            className="w-full"
            initial={reduceMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.35, ease: [0.22, 1, 0.36, 1] }}
          >
            <Composer thinking={thinking} busy={busy} onSend={onSend} onStop={onStop} />
          </motion.div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex size-full flex-col">
      <div className="thin-scroll message-scroll min-h-0 flex-1 overflow-y-auto px-5 pt-16 md:px-10">
        <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-6 pb-6 lg:gap-8">
          {messages.map((m) => (
            <MessageBubble
              key={m.id}
              message={m}
              regenerating={regeneratingMessageIds.includes(m.id) || (!!m.run && (m.run.status === "running" || (m.run.status === "pending" && !!runActions?.runActive)))}
              onCopy={onCopyMessage}
              onShare={onShareMessage}
              onEdit={m.role === "user" ? onEditPrompt : undefined}
              onRegenerate={m.role === "assistant" ? (m.run ? onRegenerateRun : onRegenerateAnswer) : undefined}
              runActions={runActions}
            />
          ))}
          {thinking && <TypingIndicator />}
          <div ref={endRef} />
        </div>
      </div>
      <div className="px-5 pb-5 md:px-10">
        <div className="mx-auto w-full max-w-[960px]">
          <Composer compact thinking={thinking} busy={busy} onSend={onSend} onStop={onStop} />
        </div>
      </div>
    </div>
  );
}
