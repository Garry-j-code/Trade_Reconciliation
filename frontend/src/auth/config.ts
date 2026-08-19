export type RuntimeConfig = {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoRegion: string;
  authDisabled: boolean;
};

/**
 * Raised when the site was published without usable Cognito config while the
 * API still enforces auth. Booting anyway would render a console that 401s on
 * every call instead of showing a login screen, so the app refuses to start.
 */
export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

const AUTH_DISABLED: RuntimeConfig = {
  cognitoUserPoolId: "",
  cognitoClientId: "",
  cognitoRegion: "us-east-1",
  authDisabled: true,
};

let cached: RuntimeConfig | null = null;

async function fetchConfig(): Promise<Partial<RuntimeConfig> | null> {
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as Partial<RuntimeConfig>;
  } catch {
    return null;
  }
}

function isLocalDev(): boolean {
  return ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
}

/**
 * "unknown" covers both an unreachable API and one built before /health
 * reported this field.
 */
async function fetchServerAuthMode(): Promise<"required" | "disabled" | "unknown"> {
  try {
    const res = await fetch("/health", { cache: "no-store" });
    if (!res.ok) return "unknown";
    const body = (await res.json()) as { auth?: string };
    if (body.auth === "required") return "required";
    if (body.auth === "disabled") return "disabled";
    return "unknown";
  } catch {
    return "unknown";
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return cached;

  const body = await fetchConfig();
  if (body?.cognitoUserPoolId && body.cognitoClientId && body.authDisabled !== true) {
    cached = {
      cognitoUserPoolId: body.cognitoUserPoolId,
      cognitoClientId: body.cognitoClientId,
      cognitoRegion: body.cognitoRegion ?? "us-east-1",
      authDisabled: false,
    };
    return cached;
  }

  // No usable Cognito config. Running auth-disabled is only ever legitimate
  // for local dev against a local API — on a hosted origin it means the site
  // was published without its generated config.json.
  if (isLocalDev()) {
    cached = AUTH_DISABLED;
    return cached;
  }

  const serverAuth = await fetchServerAuthMode();
  if (serverAuth === "disabled") {
    cached = AUTH_DISABLED;
    return cached;
  }
  const missing =
    body === null
      ? "config.json is missing from this deployment"
      : "config.json has no Cognito user pool or client ID";
  throw new ConfigError(
    serverAuth === "required"
      ? `${missing}, but the API requires sign-in.`
      : `${missing}, and the API could not confirm whether sign-in is required.`,
  );
}

export function getCachedConfig(): RuntimeConfig | null {
  return cached;
}
