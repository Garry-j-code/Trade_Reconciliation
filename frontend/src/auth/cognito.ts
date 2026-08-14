import type { RuntimeConfig } from "./config";
import { emailFromIdToken, type TokenSet } from "./session";

type CognitoAuthResult = {
  AccessToken?: string;
  IdToken?: string;
  RefreshToken?: string;
};

type CognitoResponse = {
  AuthenticationResult?: CognitoAuthResult;
  ChallengeName?: string;
  message?: string;
  __type?: string;
};

function endpoint(region: string): string {
  return `https://cognito-idp.${region}.amazonaws.com/`;
}

async function cognitoCall(
  cfg: RuntimeConfig,
  target: string,
  body: Record<string, unknown>,
): Promise<CognitoResponse> {
  const res = await fetch(endpoint(cfg.cognitoRegion), {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": target,
    },
    body: JSON.stringify(body),
  });
  const parsed = (await res.json()) as CognitoResponse;
  if (!res.ok) {
    const msg = parsed.message || parsed.__type || `Cognito error ${res.status}`;
    throw new Error(msg.replace(/^.*Exception: /, ""));
  }
  return parsed;
}

function tokensFrom(result: CognitoAuthResult, fallbackRefresh = ""): TokenSet {
  const idToken = result.IdToken ?? "";
  const accessToken = result.AccessToken ?? "";
  if (!idToken && !accessToken) {
    throw new Error("Sign-in did not return tokens");
  }
  return {
    accessToken,
    idToken,
    refreshToken: result.RefreshToken ?? fallbackRefresh,
    email: idToken ? emailFromIdToken(idToken) : "",
  };
}

export async function signIn(
  cfg: RuntimeConfig,
  username: string,
  password: string,
): Promise<TokenSet> {
  const response = await cognitoCall(cfg, "AWSCognitoIdentityProviderService.InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: cfg.cognitoClientId,
    AuthParameters: {
      USERNAME: username.trim(),
      PASSWORD: password,
    },
  });
  if (response.ChallengeName) {
    throw new Error(`Additional sign-in step required (${response.ChallengeName}). Ask the operator to set a permanent password.`);
  }
  if (!response.AuthenticationResult) {
    throw new Error("Sign-in failed");
  }
  return tokensFrom(response.AuthenticationResult);
}

export async function refreshSession(cfg: RuntimeConfig, refreshToken: string): Promise<TokenSet> {
  const response = await cognitoCall(cfg, "AWSCognitoIdentityProviderService.InitiateAuth", {
    AuthFlow: "REFRESH_TOKEN_AUTH",
    ClientId: cfg.cognitoClientId,
    AuthParameters: {
      REFRESH_TOKEN: refreshToken,
    },
  });
  if (!response.AuthenticationResult) {
    throw new Error("Session expired. Sign in again.");
  }
  return tokensFrom(response.AuthenticationResult, refreshToken);
}
