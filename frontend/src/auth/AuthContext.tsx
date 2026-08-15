import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { changePassword as cognitoChangePassword, refreshSession, signIn } from "./cognito";
import { loadRuntimeConfig, type RuntimeConfig } from "./config";
import {
  clearTokens,
  identityFromTokens,
  loadTokens,
  saveTokens,
  tokenExpired,
  type TokenSet,
} from "./session";

type AuthState = {
  ready: boolean;
  config: RuntimeConfig | null;
  tokens: TokenSet | null;
  displayName: string;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  changePassword: (previousPassword: string, proposedPassword: string) => Promise<void>;
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

  const changeUserPassword = useCallback(
    async (previousPassword: string, proposedPassword: string) => {
      if (!config || config.authDisabled) {
        throw new Error("Password changes are unavailable in local auth-disabled mode.");
      }
      let current = tokens;
      if (!current) {
        throw new Error("Sign in before changing your password.");
      }
      if (tokenExpired(current.accessToken) && current.refreshToken) {
        current = await refreshSession(config, current.refreshToken);
        saveTokens(current);
        setTokens(current);
      }
      await cognitoChangePassword(
        config,
        current.accessToken,
        previousPassword,
        proposedPassword,
      );
    },
    [config, tokens],
  );

  const displayName = tokens ? identityFromTokens(tokens) : "";

  const value = useMemo(
    () => ({
      ready,
      config,
      tokens,
      displayName,
      login,
      logout,
      changePassword: changeUserPassword,
    }),
    [ready, config, tokens, displayName, login, logout, changeUserPassword],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
