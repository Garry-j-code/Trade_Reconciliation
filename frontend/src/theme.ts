export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "trade-recon-theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

export function isThemePreference(value: string | null): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function readThemePreference(): ThemePreference {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    if (isThemePreference(raw)) return raw;
  } catch {
    /* private mode */
  }
  return "system";
}

export function persistThemePreference(preference: ThemePreference): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    /* private mode */
  }
}

export function systemPrefersDark(): boolean {
  return window.matchMedia(DARK_QUERY).matches;
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === "light" || preference === "dark") return preference;
  return systemPrefersDark() ? "dark" : "light";
}

export function applyTheme(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference);
  const root = document.documentElement;
  if (preference === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.dataset.theme = preference;
  }
  root.style.colorScheme = resolved;
  return resolved;
}

export function subscribeSystemTheme(onChange: () => void): () => void {
  const mq = window.matchMedia(DARK_QUERY);
  const handler = (): void => {
    onChange();
  };
  mq.addEventListener("change", handler);
  return () => mq.removeEventListener("change", handler);
}

export function readCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
