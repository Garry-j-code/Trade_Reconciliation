export type RuntimeConfig = {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoRegion: string;
  authDisabled: boolean;
};

const FALLBACK: RuntimeConfig = {
  cognitoUserPoolId: "",
  cognitoClientId: "",
  cognitoRegion: "us-east-1",
  authDisabled: true,
};

let cached: RuntimeConfig | null = null;

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return cached;
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (!res.ok) {
      cached = FALLBACK;
      return cached;
    }
    const body = (await res.json()) as Partial<RuntimeConfig>;
    const authDisabled =
      body.authDisabled === true ||
      !body.cognitoUserPoolId ||
      !body.cognitoClientId;
    cached = {
      cognitoUserPoolId: body.cognitoUserPoolId ?? "",
      cognitoClientId: body.cognitoClientId ?? "",
      cognitoRegion: body.cognitoRegion ?? "us-east-1",
      authDisabled,
    };
    return cached;
  } catch {
    cached = FALLBACK;
    return cached;
  }
}

export function getCachedConfig(): RuntimeConfig | null {
  return cached;
}
