"""The PO-ledger lock must be released on the connection that took it, even though the pipeline commits
(and so returns the Session's connection to the pool) in the middle of the locked section.

Postgres advisory locks belong to one connection. SQLite has none, so this test registers stand-in
pg_try_advisory_lock / pg_advisory_unlock functions that behave the same way (owned per connection)."""
import tempfile

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.pipeline import LedgerSection

HOLDER: dict[int, int] = {}   # lock key -> id of the DBAPI connection holding it


def _engine():
    eng = create_engine(f"sqlite:///{tempfile.mkdtemp()}/lock.db", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _register(dbapi_conn, _):
        me = id(dbapi_conn)

        def try_lock(k):
            if HOLDER.get(k) in (None, me):
                HOLDER[k] = me
                return 1
            return 0

        def unlock(k):
            if HOLDER.get(k) == me:
                del HOLDER[k]
                return 1
            return 0  # like Postgres: unlocking from another connection does nothing

        dbapi_conn.create_function("pg_try_advisory_lock", 1, try_lock)
        dbapi_conn.create_function("pg_advisory_unlock", 1, unlock)

    return eng


def test_lock_survives_commits_inside_the_section_and_is_released():
    HOLDER.clear()
    Session = sessionmaker(bind=_engine())
    for _ in range(3):  # three invoices in a row: each must get the lock
        db = Session()
        section = LedgerSection(db)
        section.pg = True
        with section:
            assert HOLDER, "lock taken"
            for _stage in range(4):          # the pipeline commits after every stage
                db.execute(text("SELECT 1"))
                db.commit()
        assert not HOLDER, "lock released on the connection that took it"
        db.close()


def test_lock_is_released_when_the_section_fails():
    HOLDER.clear()
    db = sessionmaker(bind=_engine())()
    section = LedgerSection(db)
    section.pg = True
    try:
        with section:
            db.commit()
            raise RuntimeError("stage failed")
    except RuntimeError:
        pass
    assert not HOLDER
