"""Cognito JWT verification for `/api/*`. `/health` stays public."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any, Optional

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

SCHEDULER_SECRET_HEADER = "X-Recon-Scheduler-Secret"
_jwks_client: PyJWKClient | None = None
_jwks_url: str | None = None


@dataclass(frozen=True)
class AuthContext:
    sub: str
    username: str
    email: str
    token_use: str

    @property
    def actor(self) -> str:
        return (self.email or self.username or self.sub)[:128]


def auth_disabled() -> bool:
    raw = (os.environ.get("AUTH_DISABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "y"}


def auth_is_required() -> bool:
    if auth_disabled():
        return False
    return bool((os.environ.get("COGNITO_USER_POOL_ID") or "").strip())


def scheduler_secret_from_env() -> str:
    return (os.environ.get("RECON_SCHEDULER_SECRET") or "").strip()


def _cognito_region() -> str:
    return (os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION") or "us-east-1").strip()


def _issuer(pool_id: str) -> str:
    return f"https://cognito-idp.{_cognito_region()}.amazonaws.com/{pool_id}"


def _jwks(pool_id: str) -> PyJWKClient:
    global _jwks_client, _jwks_url
    url = f"{_issuer(pool_id)}/.well-known/jwks.json"
    if _jwks_client is None or _jwks_url != url:
        _jwks_client = PyJWKClient(url, cache_jwk_set=True, lifespan=3600)
        _jwks_url = url
    return _jwks_client


def reset_jwks_client() -> None:
    """Tests only."""
    global _jwks_client, _jwks_url
    _jwks_client = None
    _jwks_url = None


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def scheduler_authorized(request: Request) -> bool:
    expected = scheduler_secret_from_env()
    if not expected:
        return False
    provided = (request.headers.get(SCHEDULER_SECRET_HEADER) or "").strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def _scheduler_path_allowed(request: Request) -> bool:
    path = request.url.path.rstrip("/") or "/"
    return request.method.upper() == "POST" and path in {
        "/api/recon/run",
        "/api/ops/memory-write",
    }


def verify_cognito_jwt(token: str) -> AuthContext:
    """Verify a Cognito ID or access token. Raises HTTPException on failure."""
    pool_id = (os.environ.get("COGNITO_USER_POOL_ID") or "").strip()
    client_id = (os.environ.get("COGNITO_CLIENT_ID") or "").strip()
    if not pool_id or not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cognito is not configured",
        )
    try:
        signing_key = _jwks(pool_id).get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_issuer(pool_id),
            options={"verify_aud": False, "require": ["exp", "iat", "iss", "sub"]},
            leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    token_use = str(claims.get("token_use") or "")
    if token_use == "id":
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if client_id not in audiences:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token audience mismatch",
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif token_use == "access":
        if claims.get("client_id") != client_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token client mismatch",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username = str(claims.get("cognito:username") or claims.get("username") or "")
    email = str(claims.get("email") or username)
    return AuthContext(
        sub=str(claims.get("sub") or ""),
        username=username,
        email=email,
        token_use=token_use,
    )


def require_api_auth(request: Request) -> Optional[AuthContext]:
    """FastAPI dependency for `/api/*`: Cognito JWT, or scheduler secret on recon/memory."""
    if scheduler_authorized(request):
        if not _scheduler_path_allowed(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Scheduler secret is not valid for this route",
            )
        ctx = AuthContext(sub="scheduler", username="scheduler", email="scheduler", token_use="internal")
        request.state.auth = ctx
        return ctx

    if not auth_is_required():
        request.state.auth = None
        return None

    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    ctx = verify_cognito_jwt(token)
    request.state.auth = ctx
    return ctx
