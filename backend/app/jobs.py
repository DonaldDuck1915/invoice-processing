"""Running the pipeline outside the request that queued it (uploads, batches, e-mail, restarts)."""
from __future__ import annotations

import logging
import threading

from sqlalchemy import select

from . import models as m
from .audit import log
from .db import SessionLocal
from .pipeline import process_invoice

log_ = logging.getLogger(__name__)


def process_many(invoice_ids: list[int], trigger: str = "INITIAL", actor: str = "SYSTEM") -> None:
    """Process invoices one after another, each in its own session. One failure never stops the rest."""
    for invoice_id in invoice_ids:
        db = SessionLocal()
        try:
            process_invoice(db, invoice_id, trigger, actor)
        except Exception:  # noqa: BLE001 - process_invoice records its own failures; this is a last resort
            log_.exception("background processing failed for invoice %s", invoice_id)
        finally:
            db.close()


def process_in_thread(invoice_ids: list[int], trigger: str = "INITIAL", actor: str = "SYSTEM") -> threading.Thread:
    t = threading.Thread(target=process_many, args=(invoice_ids, trigger, actor), daemon=True,
                         name=f"process-{invoice_ids[0] if invoice_ids else 'none'}")
    t.start()
    return t


def recover_interrupted() -> list[int]:
    """Invoices left RECEIVED/PROCESSING by a restart (e.g. the host slept mid-run) are re-queued,
    so nothing stays 'processing' forever. Returns the ids that were re-queued."""
    db = SessionLocal()
    try:
        stuck = db.scalars(select(m.Invoice).where(m.Invoice.status.in_(("RECEIVED", "PROCESSING")))).all()
        ids = [i.id for i in stuck]
        for inv in stuck:
            log(db, inv.id, "PROCESSING_RESUMED",
                f"Processing was interrupted (stage {inv.stage or 'queued'}) - re-queued after a restart.", {})
            inv.status, inv.stage = "RECEIVED", "QUEUED"
        db.commit()
    finally:
        db.close()
    if ids:
        log_.warning("re-queueing %d interrupted invoice(s): %s", len(ids), ids)
        process_in_thread(ids, "INITIAL", "SYSTEM")
    return ids
