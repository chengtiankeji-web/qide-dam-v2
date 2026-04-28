"""Sync DB helpers for Celery tasks — Celery doesn't play nice with asyncpg."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_engine = None
_factory: sessionmaker | None = None


def get_sync_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.DATABASE_URL_SYNC, pool_pre_ping=True, future=True
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_sync_engine(), expire_on_commit=False, future=True)
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    sess = get_session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
