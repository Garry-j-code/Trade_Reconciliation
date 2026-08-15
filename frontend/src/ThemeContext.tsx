import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  applyTheme,
  persistThemePreference,
  readThemePreference,
  resolveTheme,
  subscribeSystemTheme,
  type ResolvedTheme,
  type ThemePreference,
} from "./theme";

type ThemeContextValue = {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(() =>
    readThemePreference(),
  );
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    resolveTheme(readThemePreference()),
  );

  useEffect(() => {
    const next = applyTheme(preference);
    setResolved(next);
    return subscribeSystemTheme(() => {
      setResolved(applyTheme(preference));
    });
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference): void => {
    persistThemePreference(next);
    setPreferenceState(next);
    setResolved(applyTheme(next));
  }, []);

  const value = useMemo(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved, setPreference],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme must be used within ThemeProvider");
  }
  return ctx;
}
