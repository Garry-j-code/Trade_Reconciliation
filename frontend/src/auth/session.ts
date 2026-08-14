const ACCESS = "tr.accessToken";
const ID = "tr.idToken";
const REFRESH = "tr.refreshToken";
const EMAIL = "tr.email";

export type TokenSet = {
  accessToken: string;
  idToken: string;
  refreshToken: string;
  email: string;
};

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  sessionStorage.setItem(key, value);
}

export function loadTokens(): TokenSet | null {
  const accessToken = read(ACCESS);
  const idToken = read(ID);
  const refreshToken = read(REFRESH);
  const email = read(EMAIL) ?? "";
  if (!accessToken || !idToken) return null;
  return { accessToken, idToken, refreshToken: refreshToken ?? "", email };
}

export function saveTokens(tokens: TokenSet): void {
  write(ACCESS, tokens.accessToken);
  write(ID, tokens.idToken);
  write(REFRESH, tokens.refreshToken);
  write(EMAIL, tokens.email);
}

export function clearTokens(): void {
  try {
    sessionStorage.removeItem(ACCESS);
    sessionStorage.removeItem(ID);
    sessionStorage.removeItem(REFRESH);
    sessionStorage.removeItem(EMAIL);
  } catch {
    /* ignore */
  }
}

export function bearerToken(): string | null {
  const tokens = loadTokens();
  return tokens?.idToken ?? tokens?.accessToken ?? null;
}

export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    const padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = atob(padded);
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function tokenExpired(token: string, skewSeconds = 60): boolean {
  const payload = decodeJwtPayload(token);
  const exp = payload?.exp;
  if (typeof exp !== "number") return false;
  return Date.now() / 1000 >= exp - skewSeconds;
}

export function emailFromIdToken(idToken: string): string {
  const payload = decodeJwtPayload(idToken);
  if (payload && typeof payload.email === "string") return payload.email;
  if (payload && typeof payload["cognito:username"] === "string") {
    return payload["cognito:username"];
  }
  return "";
}
