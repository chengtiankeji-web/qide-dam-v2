"""Async SQLAlchemy engine + session factory.

Loop-aware engine caching (2026-05-21 v1 fix):
  - API server: 一个常驻 main loop · 一直复用同一个 engine（性能 OK）
  - Celery worker: prefork child 每次 task 用 asyncio.run() 起新 loop · 拿独立 engine
    避免 "Future attached to a different loop" 跨 loop bug。
  - 当 loop 关闭时 · weakref + finalizer 自动清理对应 engine。
"""
from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# loop_id → (engine, session_factory) · 用 dict + finalizer 弱引用
_engine_cache: dict[int, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG and not settings.is_production,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
    )


def _current_loop_id() -> int | None:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None


def get_engine() -> AsyncEngine:
    """每个 event loop 一个独立 engine · 避免跨 loop bug"""
    loop_id = _current_loop_id()
    if loop_id is not None and loop_id in _engine_cache:
        return _engine_cache[loop_id][0]

    engine = _make_engine()
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False,
    )
    if loop_id is not None:
        _engine_cache[loop_id] = (engine, factory)

        # loop 被 GC 时自动清理 engine entry
        loop = asyncio.get_running_loop()
        weakref.finalize(loop, _engine_cache.pop, loop_id, None)

    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """每个 event loop 一个独立 factory"""
    loop_id = _current_loop_id()
    if loop_id is not None and loop_id in _engine_cache:
        return _engine_cache[loop_id][1]

    # trigger 创建（顺便也填了 factory）
    get_engine()
    if loop_id is not None and loop_id in _engine_cache:
        return _engine_cache[loop_id][1]

    # fallback (no running loop) · 极少触发 · sync test 之类
    engine = _make_engine()
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


class _LazySessionFactory:
    """Backward compat: `AsyncSessionLocal()` callable"""

    def __call__(self, *args, **kwargs) -> AsyncSession:
        return get_session_factory()(*args, **kwargs)


AsyncSessionLocal = _LazySessionFactory()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
