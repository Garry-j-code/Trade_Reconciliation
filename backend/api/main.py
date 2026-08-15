"""FastAPI app. Local uvicorn by default; Mangum entry in ``lambda_handler`` (Phase 2)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.auth import require_api_auth
from backend.api.deps import reset_session_factory
from backend.api.routes import breaks, health, recon, resolutions
from backend.db.session import (
    database_url_from_env,
    ensure_agent_schema_patches,
    get_engine,
    get_session_factory,
)

logger = logging.getLogger(__name__)


def _is_testing() -> bool:
    return bool(os.environ.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST"))


def _cors_allow_origins() -> list[str]:
    origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://d1a8rtzx54qkw.cloudfront.net",
    ]
    extra = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if extra:
        origins.extend(o.strip() for o in extra.split(",") if o.strip())
    cf = os.environ.get("CLOUDFRONT_ORIGIN", "").strip()
    if cf:
        origins.append(cf.rstrip("/"))
    return origins


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not _is_testing():
        load_dotenv()
    url = database_url_from_env()
    if url and not _is_testing():
        engine = get_engine(url)
        app.state.engine = engine
        app.state.session_factory = get_session_factory(engine)
        try:
            ensure_agent_schema_patches(engine)
        except Exception as exc:  # noqa: BLE001 — sqlite / missing table
            logger.warning("Could not apply schema patches: %s", exc)
    else:
        app.state.engine = None
        app.state.session_factory = None
    try:
        yield
    finally:
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            engine.dispose()
        reset_session_factory()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Trade Reconciliation API",
        description=(
            "FastAPI for the recon dashboard. Talks to RDS via DATABASE_URL. "
            "Local: uvicorn. Hosted: EC2 in the RDS VPC (see infra/README.md)."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow_origins(),
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://[a-z0-9]+\.cloudfront\.net",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(health.router)

    api = APIRouter(prefix="/api", dependencies=[Depends(require_api_auth)])
    api.include_router(recon.router)
    api.include_router(breaks.router)
    api.include_router(resolutions.router)
    application.include_router(api)
    return application


app = create_app()

# Lambda (Phase 2): backend.api.lambda_handler.handler  (Mangum)


def main() -> None:
    """Console script ``serve-api`` — local uvicorn with reload."""
    import uvicorn

    load_dotenv()
    uvicorn.run(
        "backend.api.main:app",
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("API_PORT", "8000")),
        reload=True,
    )


if __name__ == "__main__":
    main()
