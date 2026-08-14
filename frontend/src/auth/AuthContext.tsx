import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { refreshSession, signIn } from "./cognito";
import { loadRuntimeConfig, type RuntimeConfig } from "./config";
import {
  clearTokens,
  loadTokens,
  saveTokens,
  tokenExpired,
  type TokenSet,
} from "./session";

type AuthState = {
  ready: boolean;
  config: RuntimeConfig | null;
  tokens: TokenSet | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [tokens, setTokens] = useState<TokenSet | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const cfg = await loadRuntimeConfig();
      if (cancelled) return;
      setConfig(cfg);
      if (cfg.authDisabled) {
        setTokens(null);
        setReady(true);
        return;
      }
      let stored = loadTokens();
      if (stored && stored.refreshToken && tokenExpired(stored.idToken || stored.accessToken)) {
        try {
          stored = await refreshSession(cfg, stored.refreshToken);
          saveTokens(stored);
        } catch {
          clearTokens();
          stored = null;
        }
      }
      setTokens(stored);
      setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onUnauthorized(): void {
      clearTokens();
      setTokens(null);
    }
    window.addEventListener("auth:unauthorized", onUnauthorized);
    return () => window.removeEventListener("auth:unauthorized", onUnauthorized);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    if (!config || config.authDisabled) return;
    const next = await signIn(config, username, password);
    saveTokens(next);
    setTokens(next);
  }, [config]);

  const logout = useCallback(() => {
    clearTokens();
    setTokens(null);
  }, []);

  const value = useMemo(
    () => ({ ready, config, tokens, login, logout }),
    [ready, config, tokens, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
