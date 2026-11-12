import { Settings } from "lucide-react";
import avatar from "../../assets/avatar.jpg";
import { useLocale } from "../../context/LocaleContext";

interface Props {
  name: string;
  plan: string;
  onOpenSettings?: () => void;
}

export default function UserCard({ name, plan, onOpenSettings }: Props) {
  const { t } = useLocale();
  return (
    <button
      type="button"
      aria-label={t("settings")}
      title={t("settings")}
      onClick={onOpenSettings}
      className="group/user -mx-2 flex w-[248px] cursor-pointer items-center gap-3 rounded-2xl border border-transparent px-2 py-1.5 text-start transition-[background-color,border-color,transform] duration-200 hover:border-white/10 hover:bg-white/[0.08] active:scale-[0.98]"
    >
      <img
        src={avatar}
        alt=""
        className="size-11 shrink-0 rounded-full object-cover ring-2 ring-white/10 transition-[box-shadow] duration-200 group-hover/user:ring-white/30"
      />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[18px] font-medium leading-[22px] sm:text-[20px] sm:leading-[24px]">{name}</span>
        <span className="mt-1 text-[12.5px] leading-[14px] opacity-70">{plan}</span>
      </span>
      <Settings
        className="size-[18px] shrink-0 text-white/60 opacity-0 transition-[opacity,transform] duration-300 group-hover/user:rotate-45 group-hover/user:opacity-100"
        aria-hidden="true"
      />
    </button>
  );
}
