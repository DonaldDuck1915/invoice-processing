"""Running the pipeline outside the request that queued it (uploads, batches, e-mail, restarts)."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from . import models as m
from .audit import log
from .config import get_settings
from .pipeline import ReadResult, decide_invoice, read_invoice, summarise_invoice

log_ = logging.getLogger(__name__)


_read_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()


def read_slots() -> threading.BoundedSemaphore:
    """Process-wide cap on invoices being read at once (OCR / AI), shared by every batch and upload."""
    global _read_slots
    with _slots_lock:
        if _read_slots is None:
            _read_slots = threading.BoundedSemaphore(max(1, get_settings().read_concurrency))
        return _read_slots


def _read(invoice_id: int, trigger: str, actor: str, workspace: str | None):
    """Read one invoice in its own session. Returns (session, ReadResult | failed run)."""
    from .workspaces import open_session

    db = open_session(workspace)
    try:
        with read_slots():
            return db, read_invoice(db, invoice_id, trigger, actor)
    except Exception:  # noqa: BLE001 - read_invoice records its own failures; this is a last resort
        log_.exception("background read failed for invoice %s", invoice_id)
        return db, None


def process_many(invoice_ids: list[int], trigger: str = "INITIAL", actor: str = "SYSTEM",
                 workspace: str | None = None) -> None:
    """Process a batch: invoices are READ in parallel (up to READ_CONCURRENCY at once - OCR and AI calls are
    mostly waiting, so they overlap well) and DECIDED one by one in upload order, as soon as each one and
    everything uploaded before it has been read. Upload order matters for decisions: split invoices on one
    PO and duplicates are judged against what was decided before them. One failure never stops the rest."""
    if not invoice_ids:
        return
    workers = min(len(invoice_ids), max(1, get_settings().read_concurrency))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="read") as pool:
        futures = [pool.submit(_read, i, trigger, actor, workspace) for i in invoice_ids]
        summaries = []
        for invoice_id, fut in zip(invoice_ids, futures):   # upload order
            db, res = fut.result()
            facts = None
            try:
                if isinstance(res, ReadResult):
                    facts = decide_invoice(db, res, summarise=False)
            except Exception:  # noqa: BLE001 - decide_invoice records its own failures; last resort
                log_.exception("background decision failed for invoice %s", invoice_id)
            if facts is None:
                db.close()
                continue
            # the AI summary does not affect later decisions, so it is written in parallel
            summaries.append(pool.submit(_summarise, db, res, facts))
        for s in summaries:
            s.result()


def _summarise(db, res, facts) -> None:
    try:
        with read_slots():
            summarise_invoice(db, res, facts)
    except Exception:  # noqa: BLE001
        log_.exception("background summary failed for invoice %s", res.invoice.id)
    finally:
        db.close()


def process_in_thread(invoice_ids: list[int], trigger: str = "INITIAL", actor: str = "SYSTEM",
                      workspace: str | None = None) -> threading.Thread:
    t = threading.Thread(target=process_many, args=(invoice_ids, trigger, actor, workspace), daemon=True,
                         name=f"process-{invoice_ids[0] if invoice_ids else 'none'}")
    t.start()
    return t


def recover_interrupted(workspace: str | None = None) -> list[int]:
    """Invoices left RECEIVED/PROCESSING by a restart (e.g. the host slept mid-run) are re-queued,
    so nothing stays 'processing' forever. Returns the ids that were re-queued."""
    from .workspaces import open_session

    db = open_session(workspace)
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
        process_in_thread(ids, "INITIAL", "SYSTEM", workspace)
    return ids
