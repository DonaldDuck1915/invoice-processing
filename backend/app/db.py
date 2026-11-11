from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

from sqlalchemy import String, TypeDecorator, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class DecimalString(TypeDecorator):
    """Stores Decimals as exact strings — identical behaviour on SQLite and Postgres."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(Decimal(str(value)), "f")

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
