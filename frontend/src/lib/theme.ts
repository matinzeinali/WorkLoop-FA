// Color theme: "glass" (glass over a photo), "light" (plain light UI) or "dark" (plain dark UI).
export type Theme = "glass" | "light" | "dark";
const KEY = "workloop:theme";

export function getTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "glass";
  } catch { return "glass"; }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(KEY, theme); } catch { /* storage unavailable */ }
}
