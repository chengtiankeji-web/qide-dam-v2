"""Worker-only async DB helper · 避免 Celery prefork + asyncio.run 的跨 event loop bug

问题：`app.db.session.get_session_factory()` 在模块层缓存 engine + session factory。
Celery worker (prefork pool) 同一个子进程会跑多个 task · 每次 `asyncio.run()` 起新 loop
· 但 cached engine 的 connection pool attached to 旧 loop · 第二次 task 调用就报：

    RuntimeError: got Future attached to a different loop

解法：每个 task 开始时建独立 engine · 跑完 dispose · 不缓存。

用法：

    async def _my_task_async():
        async with task_session_scope() as session_factory:
            async with session_factory() as db:
                ...

性能：每次 task 多 1 个连接建立成本 (~10ms)。对 30s 周期的 drain 完全不影响。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings


@asynccontextmanager
async def task_session_scope():
    """Enter: build engine + factory · Exit: dispose engine"""
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=3,
        future=True,
    )
    factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False,
    )
    try:
        yield factory
    finally:
        await engine.dispose()
