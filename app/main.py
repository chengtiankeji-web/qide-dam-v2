"""FastAPI entry — `uvicorn app.main:app`."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app.startup", env=settings.APP_ENV, debug=settings.DEBUG)
    # Best-effort bucket bootstrap. Failures (e.g., R2 not yet provisioned) shouldn't
    # crash boot — the failure will surface on first upload anyway.
    try:
        from app.services import storage
        storage.ensure_bucket()
    except Exception as e:  # noqa: BLE001
        logger.warning("app.startup.bucket_check_failed", error=str(e))
    yield
    logger.info("app.shutdown")


app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    description="AI-native multi-tenant Digital Asset Management",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    response = await call_next(request)
    logger.info(
        "http.request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
    )
    return response


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router)

# Public (unauthenticated) share-link resolver mounted at /p/...
from app.api.v1.share_links import public_router as _share_public_router  # noqa: E402

app.include_router(_share_public_router, prefix="/p", tags=["public"])


@app.get("/")
async def root() -> dict:
    return {
        "service": settings.APP_NAME,
        "version": app.version,
        "env": settings.APP_ENV,
        "docs": "/docs" if not settings.is_production else "disabled",
    }
