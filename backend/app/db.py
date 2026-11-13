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
    add_missing_columns()


def add_missing_columns(eng=None, schema: str | None = None) -> list[str]:
    """Light-weight migration: add the nullable columns newer models define but an existing database
    (created by an older version of the app) lacks. Never drops or changes existing columns.
    `schema` is a Postgres schema (guest workspaces); `eng` defaults to the main engine."""
    from sqlalchemy import inspect, text

    eng = eng if eng is not None else engine
    added = []
    insp = inspect(eng)
    prefix = f'"{schema}".' if schema else ""
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name, schema=schema):
                continue
            have = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                ddl = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    if isinstance(v, bool):
                        default = " DEFAULT " + ("TRUE" if v else "FALSE")
                    elif isinstance(v, str):
                        default = " DEFAULT '" + v.replace("'", "''") + "'"
                conn.execute(text(f"ALTER TABLE {prefix}{table.name} ADD COLUMN {col.name} {ddl}{default}"))
                added.append(f"{table.name}.{col.name}")
    return added
