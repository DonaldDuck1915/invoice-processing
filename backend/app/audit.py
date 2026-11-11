"""Append-only audit trail."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m


def log(db: Session, invoice_id: int | None, event_type: str, message: str, payload: dict | None = None,
        run_no: int | None = None, actor: str = "SYSTEM") -> m.AuditEvent:
    seq = (db.scalar(select(func.max(m.AuditEvent.seq)).where(m.AuditEvent.invoice_id == invoice_id)) or 0) + 1
    ev = m.AuditEvent(invoice_id=invoice_id, run_no=run_no, seq=seq, event_type=event_type, actor=actor,
                      message=message, payload=payload or {})
    db.add(ev)
    db.flush()
    return ev
