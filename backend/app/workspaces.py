"""Private guest sandboxes.

Every guest (an evaluator who clicks "Continue as guest") gets a workspace of their own, seeded with the
demo vendors and purchase orders. Their uploads, reviews, rule changes and profile never touch anyone
else's data, and a workspace is deleted after `GUEST_TTL_HOURS` without use.

  Postgres (hosted): one schema per workspace ("guest_<id>") in the same database, selected with
                     SQLAlchemy's schema_translate_map, so all ORM code runs unchanged.
  SQLite (local):    one database file per workspace in GUEST_DATA_DIR.

The real (signed-in) users keep using the main database; a guest token carries its workspace id and a
request is routed to that workspace's database by `get_db`.
"""
from __future__ import annotations

import logging
import re
import secrets
import shutil
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import models as m
from .api.auth import bearer, token_payload
from .config import get_settings
from .db import Base, SessionLocal, add_missing_columns, engine

log = logging.getLogger(__name__)
WS_RE = re.compile(r"^[a-f0-9]{12}$")
_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _schema(ws: str) -> str:
    return f"guest_{ws}"


def _sqlite_path(ws: str) -> Path:
    return Path(get_settings().guest_data_dir).resolve() / f"guest_{ws}.db"


def engine_for(ws: str | None) -> Engine:
    if ws is None:
        return engine
    if not WS_RE.match(ws):
        raise ValueError("invalid workspace id")
    with _lock:
        eng = _engines.get(ws)
        if eng is None:
            if is_postgres():
                eng = engine.execution_options(schema_translate_map={None: _schema(ws)})
            else:
                path = _sqlite_path(ws)
                path.parent.mkdir(parents=True, exist_ok=True)
                eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
            _engines[ws] = eng
        return eng


def open_session(ws: str | None = None) -> Session:
    s = sessionmaker(bind=engine_for(ws), autoflush=False, expire_on_commit=False)()
    s.info["workspace"] = ws
    return s


def _tables_exist(ws: str) -> bool:
    if is_postgres():
        with engine.connect() as c:
            return bool(c.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                                  {"s": _schema(ws)}).first())
    return _sqlite_path(ws).exists()


# ----------------------------------------------------------------------------------------------- lifecycle

def create_workspace(client: str | None = None) -> str:
    """Create, migrate and seed a fresh guest workspace; returns its id."""
    from .seed import seed_demo

    ws = secrets.token_hex(6)
    if is_postgres():
        with engine.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{_schema(ws)}"'))
    Base.metadata.create_all(engine_for(ws))
    db = open_session(ws)
    try:
        seed_demo(db, reset=True)
    finally:
        db.close()
    main = SessionLocal()
    try:
        main.add(m.GuestWorkspace(id=ws, client=(client or "")[:64]))
        main.commit()
    finally:
        main.close()
    log.info("guest workspace %s created", ws)
    return ws


def drop_workspace(ws: str) -> None:
    with _lock:
        eng = _engines.pop(ws, None)
    if is_postgres():
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{_schema(ws)}" CASCADE'))
    else:
        if eng is not None:
            eng.dispose()
        _sqlite_path(ws).unlink(missing_ok=True)
    main = SessionLocal()
    try:
        row = main.get(m.GuestWorkspace, ws)
        if row:
            main.delete(row)
            main.commit()
    finally:
        main.close()


def reset_workspace(ws: str) -> None:
    """Back to the fresh demo data (used by the guest's own 'Reset demo' button)."""
    from .seed import seed_demo

    db = open_session(ws)
    try:
        seed_demo(db, reset=True)
        for p in db.scalars(select(m.UserProfile)):
            db.delete(p)
        db.commit()
    finally:
        db.close()


def sweep_expired() -> int:
    ttl = timedelta(hours=get_settings().guest_ttl_hours)
    cutoff = datetime.now(timezone.utc) - ttl
    main = SessionLocal()
    try:
        old = [w.id for w in main.scalars(select(m.GuestWorkspace)) if _aware(w.last_used_at) < cutoff]
    finally:
        main.close()
    for ws in old:
        try:
            drop_workspace(ws)
        except Exception:  # noqa: BLE001 - one bad workspace must not stop the sweep
            log.exception("could not delete guest workspace %s", ws)
    if old:
        log.info("deleted %d expired guest workspace(s)", len(old))
    return len(old)


def active_workspaces() -> list[str]:
    main = SessionLocal()
    try:
        return [w.id for w in main.scalars(select(m.GuestWorkspace))]
    finally:
        main.close()


def migrate_workspaces() -> None:
    """Startup: bring every live guest workspace up to the current schema (new nullable columns)."""
    for ws in active_workspaces():
        try:
            if not _tables_exist(ws):
                drop_workspace(ws)
                continue
            Base.metadata.create_all(engine_for(ws))
            add_missing_columns(engine_for(ws), _schema(ws) if is_postgres() else None)
        except Exception:  # noqa: BLE001
            log.exception("could not migrate guest workspace %s", ws)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_touched: dict[str, datetime] = {}


def touch(ws: str) -> bool:
    """Mark the workspace as used (at most once a minute). False if it no longer exists."""
    now = datetime.now(timezone.utc)
    last = _touched.get(ws)
    if last and now - last < timedelta(minutes=1):
        return True
    main = SessionLocal()
    try:
        row = main.get(m.GuestWorkspace, ws)
        if row is None:
            return False
        row.last_used_at = now
        main.commit()
    finally:
        main.close()
    _touched[ws] = now
    return True


# ----------------------------------------------------------------------------------------------- request wiring

def get_db(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Iterator[Session]:
    """Session for the caller's workspace: the main database, or the guest's own sandbox."""
    payload = token_payload(creds.credentials) if creds else None
    ws = (payload or {}).get("w")
    if ws:
        if not WS_RE.match(ws) or not touch(ws):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={
                "error_code": "GUEST_EXPIRED",
                "message": "This guest session has expired. Start a new one from the sign-in page."})
    db = open_session(ws)
    try:
        yield db
    finally:
        db.close()


def remove_all_sqlite_guest_files() -> None:
    """Tests only: wipe the SQLite guest directory."""
    shutil.rmtree(Path(get_settings().guest_data_dir).resolve(), ignore_errors=True)
