import { useState, type FormEvent } from "react";
import { Eye, EyeOff, Languages, LockKeyhole, UserRound } from "lucide-react";
import { useBackground } from "../../context/BackgroundContext";
import { useLocale } from "../../context/LocaleContext";
import Logo from "../icons/Logo";

interface Props {
  onLogin: (username: string, password: string) => boolean;
}

export default function LoginScreen({ onLogin }: Props) {
  const { locale, setLocale, t } = useLocale();
  const { background, blur } = useBackground();
  const [username, setUsername] = useState("user1");
  const [password, setPassword] = useState("12345678");
  const [showPassword, setShowPassword] = useState(false);
  const [invalid, setInvalid] = useState(false);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setInvalid(!onLogin(username, password));
  };

  return (
    <main
      dir={locale === "fa" ? "rtl" : "ltr"}
      lang={locale}
      className="login-screen relative flex min-h-dvh items-center justify-center overflow-hidden bg-ink px-4 py-8 text-white sm:px-6"
      style={{
        backgroundImage: `linear-gradient(135deg, color-mix(in srgb, var(--color-ink) 72%, transparent), color-mix(in srgb, var(--color-ink) 42%, transparent)), url(${background.src})`,
        backgroundPosition: "center",
        backgroundSize: "cover",
      }}
    >
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 bg-black/10" style={{ backdropFilter: blur ? `blur(${Math.min(blur, 10)}px)` : undefined }} />

      <button
        type="button"
        onClick={() => setLocale(locale === "fa" ? "en" : "fa")}
        className="absolute end-4 top-4 z-10 inline-flex h-10 items-center gap-2 rounded-full glass px-3 text-sm text-white/80 transition hover:bg-white/10 hover:text-white sm:end-6 sm:top-6"
        aria-label={locale === "fa" ? t("switchToEnglish") : t("switchToPersian")}
      >
        <Languages className="size-4" aria-hidden="true" />
        {locale === "fa" ? "EN" : "فا"}
      </button>

      <section className="relative z-10 w-full max-w-[420px] rounded-[28px] glass p-6 sm:p-9">
        <div className="mb-8 flex flex-col items-center text-center">
          <span className="mb-5 grid size-[62px] place-items-center rounded-[20px] border border-white/15 bg-glow/20 shadow-[0_8px_30px_rgb(0_0_0/0.18)]">
            <Logo className="h-auto w-[35px]" />
          </span>
          <h1 className="m-0 text-[24px] font-semibold leading-tight tracking-tight sm:text-[27px]">{t("loginTitle")}</h1>
          <p className="mb-0 mt-2 max-w-[290px] text-sm leading-6 text-white/60">{t("loginSubtitle")}</p>
        </div>

        <form onSubmit={submit} className="space-y-4" noValidate>
          <div>
            <label htmlFor="login-username" className="mb-2 block text-[13px] font-medium text-white/80">{t("username")}</label>
            <div className="login-field flex h-[52px] items-center gap-3 rounded-[14px] border border-white/15 bg-white/[0.08] px-3.5 transition focus-within:border-white/35 focus-within:bg-white/[0.12] focus-within:shadow-[0_0_0_3px_rgb(255_255_255/0.06)]">
              <UserRound className="size-[18px] shrink-0 text-white/45" aria-hidden="true" />
              <input
                id="login-username"
                name="username"
                type="text"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                autoFocus
                required
                value={username}
                onChange={(event) => { setUsername(event.target.value); setInvalid(false); }}
                placeholder={t("usernamePlaceholder")}
                className="login-input h-full min-w-0 flex-1 bg-transparent text-[14px] text-white outline-none placeholder:text-white/35"
              />
            </div>
          </div>

          <div>
            <label htmlFor="login-password" className="mb-2 block text-[13px] font-medium text-white/80">{t("password")}</label>
            <div className="login-field flex h-[52px] items-center gap-3 rounded-[14px] border border-white/15 bg-white/[0.08] px-3.5 transition focus-within:border-white/35 focus-within:bg-white/[0.12] focus-within:shadow-[0_0_0_3px_rgb(255_255_255/0.06)]">
              <LockKeyhole className="size-[18px] shrink-0 text-white/45" aria-hidden="true" />
              <input
                id="login-password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => { setPassword(event.target.value); setInvalid(false); }}
                placeholder={t("passwordPlaceholder")}
                className="login-input h-full min-w-0 flex-1 bg-transparent text-[14px] text-white outline-none placeholder:text-white/35"
              />
              <button
                type="button"
                onClick={() => setShowPassword((visible) => !visible)}
                aria-label={showPassword ? t("hidePassword") : t("showPassword")}
                className="grid size-8 shrink-0 place-items-center rounded-lg text-white/45 transition hover:bg-white/10 hover:text-white/80"
              >
                {showPassword ? <EyeOff className="size-[17px]" aria-hidden="true" /> : <Eye className="size-[17px]" aria-hidden="true" />}
              </button>
            </div>
          </div>

          {invalid && <p role="alert" className="m-0 rounded-lg border border-rose-300/20 bg-rose-400/10 px-3 py-2 text-[13px] leading-5 text-rose-100">{t("invalidCredentials")}</p>}

          <button
            type="submit"
            className="mt-2 inline-flex h-12 w-full cursor-pointer items-center justify-center rounded-xl bg-glow font-semibold text-white shadow-[0_5px_18px_rgb(0_0_0/0.2)] transition hover:brightness-110 active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 focus-visible:ring-offset-2 focus-visible:ring-offset-ink"
          >
            {t("signIn")}
          </button>
        </form>
      </section>
    </main>
  );
}
