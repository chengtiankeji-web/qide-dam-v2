"""HTTP/SSE transport for the MCP server.

Run alongside the main FastAPI app on port MCP_HTTP_PORT (default 8001).

Usage from a remote MCP client (e.g., Claude Desktop / Cursor):
    {
      "name": "qide-dam",
      "transport": { "type": "sse", "url": "https://dam-api.qide.com/mcp/sse" },
      "headers": { "X-DAM-API-Key": "dam_live_xxxxx" }
    }

The DAM API key supplied via the `X-DAM-API-Key` header is read from
contextvars by tools at call time (replaces the env-var lookup used in stdio).
"""
from __future__ import annotations

import contextvars

from fastapi import FastAPI, Request

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.mcp.server import mcp

configure_logging()
logger = get_logger("mcp.http")


# Bridge: the stdio server reads the key from os.environ; the HTTP server
# reads it from this contextvar (set by middleware on every request).
api_key_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dam_api_key", default=None
)


def get_runtime_api_key_http() -> str:
    key = api_key_ctx.get()
    if not key:
        raise RuntimeError("Missing X-DAM-API-Key header")
    return key


# Patch the stdio resolver if running under HTTP — done via a small wrapper.
import app.mcp.server as _server_module  # noqa: E402

_original_get_key = _server_module._get_runtime_api_key


def _resolve_key():
    """Try header context first, then env."""
    try:
        return get_runtime_api_key_http()
    except RuntimeError:
        return _original_get_key()


_server_module._get_runtime_api_key = _resolve_key  # type: ignore[assignment]


def build_http_app() -> FastAPI:
    app = FastAPI(title="QideDAM MCP HTTP", version="2.0.0")

    @app.middleware("http")
    async def capture_api_key(request: Request, call_next):
        token = request.headers.get(settings.MCP_API_KEY_HEADER)
        if token:
            api_key_ctx.set(token)
        return await call_next(request)

    # Mount the FastMCP SSE app
    sse_app = mcp.sse_app()
    app.mount("/mcp", sse_app)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "qide-dam-mcp", "transport": "sse"}

    return app


http_app = build_http_app()


def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run(http_app, host=settings.MCP_HTTP_HOST, port=settings.MCP_HTTP_PORT)


if __name__ == "__main__":  # pragma: no cover
    main()
