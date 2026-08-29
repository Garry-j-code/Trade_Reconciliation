const ACCESS = "tr.accessToken";
const ID = "tr.idToken";
const REFRESH = "tr.refreshToken";
const EMAIL = "tr.email";
const SIGNED_IN_AS = "tr.signedInAs";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type TokenSet = {
  accessToken: string;
  idToken: string;
  refreshToken: string;
  email: string;
  signedInAs: string;
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

export function isOpaqueUserId(value: string): boolean {
  const trimmed = value.trim();
  return !trimmed || UUID_RE.test(trimmed);
}

export function pickDisplayIdentity(
  ...candidates: (string | null | undefined)[]
): string {
  for (const candidate of candidates) {
    if (typeof candidate === "string" && !isOpaqueUserId(candidate)) {
      return candidate.trim();
    }
  }
  return "";
}

export function loadTokens(): TokenSet | null {
  const accessToken = read(ACCESS);
  const idToken = read(ID);
  const refreshToken = read(REFRESH);
  const email = read(EMAIL) ?? "";
  const signedInAs = read(SIGNED_IN_AS) ?? "";
  if (!accessToken || !idToken) return null;
  return { accessToken, idToken, refreshToken: refreshToken ?? "", email, signedInAs };
}

export function saveTokens(tokens: TokenSet): void {
  write(ACCESS, tokens.accessToken);
  write(ID, tokens.idToken);
  write(REFRESH, tokens.refreshToken);
  write(EMAIL, tokens.email);
  write(SIGNED_IN_AS, tokens.signedInAs);
}

export function clearTokens(): void {
  try {
    sessionStorage.removeItem(ACCESS);
    sessionStorage.removeItem(ID);
    sessionStorage.removeItem(REFRESH);
    sessionStorage.removeItem(EMAIL);
    sessionStorage.removeItem(SIGNED_IN_AS);
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

function claimString(payload: Record<string, unknown> | null, key: string): string {
  if (!payload) return "";
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

export function emailFromIdToken(idToken: string): string {
  const payload = decodeJwtPayload(idToken);
  return pickDisplayIdentity(
    claimString(payload, "email"),
    claimString(payload, "preferred_username"),
    claimString(payload, "cognito:username"),
  );
}

export function identityFromTokens(tokens: TokenSet): string {
  const idPayload = decodeJwtPayload(tokens.idToken);
  const accessPayload = decodeJwtPayload(tokens.accessToken);
  return (
    pickDisplayIdentity(
      tokens.email,
      tokens.signedInAs,
      claimString(idPayload, "email"),
      claimString(idPayload, "preferred_username"),
      claimString(idPayload, "cognito:username"),
      claimString(accessPayload, "username"),
    ) || "Signed in"
  );
}
