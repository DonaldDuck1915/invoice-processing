from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..config import get_settings, load_default_rules
from ..db import SessionLocal, get_db
from ..extraction.pdf_reader import DocumentError, ocr_available
from ..ingestion import ingest
from ..ledger import consumption_by_line, po_snapshot
from ..llm import AI_DISCLAIMER
from ..pipeline import (confirm_po, correct_fields, effective_rules, final_decision, latest_run, process_invoice,
                        resolve_duplicate)
from ..seed import import_procurement, seed_demo
from .auth import check_credentials, current_user, issue_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def jsonable(obj: Any) -> Any:
    """Decimals are serialised as strings so no precision is lost to floats."""
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(jsonable(data), status_code=status_code)


def err(status_code: int, code: str, message: str, details: Any = None):
    raise HTTPException(status_code, detail={"error_code": code, "message": message, "details": details})


# ============================================================================ auth / health

class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginBody):
    if not check_credentials(body.username, body.password):
        err(401, "INVALID_CREDENTIALS", "Incorrect username or password.")
    s = get_settings()
    return {"token": issue_token(body.username), "user": {"username": body.username, "name": s.reviewer_display_name}}


@router.get("/auth/me")
def me(user: str = Depends(current_user)):
    return {"username": user, "name": get_settings().reviewer_display_name}


@router.get("/health")
def health(db: Session = Depends(get_db)):
    s = get_settings()
    db.execute(select(1))
    return {"ok": True, "ocr_available": ocr_available(s.tesseract_cmd), "ai_configured": s.llm_available,
            "ai_model": s.llm_model if s.llm_available else None,
            "database": "postgresql" if s.database_url.startswith("postgresql") else "sqlite"}


# ============================================================================ invoices

def _run_in_background(invoice_id: int, trigger: str = "INITIAL", actor: str = "SYSTEM"):
    db = SessionLocal()
    try:
        process_invoice(db, invoice_id, trigger, actor)
    except Exception:  # noqa: BLE001
        log.exception("background processing failed")
    finally:
        db.close()


def _invoice_summary(inv: m.Invoice) -> dict:
    return {"id": inv.id, "filename": inv.original_filename, "status": inv.status, "stage": inv.stage,
            "error": inv.error, "decision": inv.decision, "decided_by": inv.decided_by,
            "system_decision": inv.system_decision, "vendor_name": inv.vendor_name, "vendor_tax_id": inv.vendor_tax_id,
            "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date, "currency": inv.currency,
            "grand_total": inv.grand_total, "po_number": inv.po.po_number if inv.po else inv.po_reference,
            "is_scanned": inv.is_scanned, "page_count": inv.page_count, "created_at": inv.created_at,
            "updated_at": inv.updated_at}


@router.post("/invoices", status_code=202)
async def upload_invoice(background: BackgroundTasks, file: UploadFile = File(...), db: Session = Depends(get_db),
                         user: str = Depends(current_user)):
    data = await file.read()
    try:
        inv = ingest(db, data, file.filename, file.content_type, user)
    except DocumentError as e:
        err(400, e.code, e.message)
    background.add_task(_run_in_background, inv.id, "INITIAL", user)
    return ok(_invoice_summary(inv), 202)


@router.get("/invoices")
def list_invoices(decision: str | None = None, status: str | None = None, q: str | None = None,
                  limit: int = Query(200, le=500), db: Session = Depends(get_db), user: str = Depends(current_user)):
    stmt = select(m.Invoice).options(selectinload(m.Invoice.po)).order_by(m.Invoice.id.desc()).limit(limit)
    if decision:
        stmt = stmt.where(m.Invoice.decision == decision)
    if status:
        stmt = stmt.where(m.Invoice.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((m.Invoice.original_filename.ilike(like)) | (m.Invoice.vendor_name.ilike(like))
                          | (m.Invoice.invoice_number.ilike(like)) | (m.Invoice.po_reference.ilike(like)))
    return ok([_invoice_summary(i) for i in db.scalars(stmt)])


def _get_invoice(db: Session, invoice_id: int) -> m.Invoice:
    inv = db.get(m.Invoice, invoice_id)
    if not inv:
        err(404, "NOT_FOUND", f"Invoice {invoice_id} not found.")
    return inv


def _run_payload(run: m.ProcessingRun | None) -> dict | None:
    if not run:
        return None
    calls = run.llm_calls or []
    return {"run_no": run.run_no, "trigger": run.trigger, "rules_hash": run.rules_hash, "started_at": run.started_at,
            "finished_at": run.finished_at, "extraction": (run.extraction or {}).get("result"),
            "validation": run.validation, "matching": run.matching, "duplicates": run.duplicates,
            "decision": run.decision, "ai_summary": run.ai_summary,
            "ai_calls": [{k: c.get(k) for k in ("purpose", "model", "served_by", "ok", "error", "latency_ms",
                                                "input_tokens", "output_tokens", "cached")} for c in calls],
            "rules": run.rules_snapshot}


@router.get("/invoices/{invoice_id}")
def invoice_detail(invoice_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    run = latest_run(db, inv.id)
    runs = db.scalars(select(m.ProcessingRun.run_no, ).where(m.ProcessingRun.invoice_id == inv.id)).all()
    po = None
    if inv.po_id:
        p = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.id == inv.po_id)
                      .options(selectinload(m.PurchaseOrder.lines), selectinload(m.PurchaseOrder.vendor)))
        consumed_all = consumption_by_line(db)
        consumed_others = consumption_by_line(db, exclude_invoice_id=inv.id)
        po = po_snapshot(p, consumed_others)
        po["after_this_invoice"] = po_snapshot(p, consumed_all)["remaining_amount"]
        po["counts_this_invoice"] = any(r.status == "ACTIVE" for r in db.scalars(
            select(m.POConsumption).where(m.POConsumption.invoice_id == inv.id)))
    dup_ref = None
    if run and run.duplicates and run.duplicates.get("best"):
        other = db.get(m.Invoice, run.duplicates["best"]["invoice_id"])
        if other:
            orun = latest_run(db, other.id)
            dup_ref = {**_invoice_summary(other),
                       "extraction": (orun.extraction or {}).get("result") if orun else None}
    reviews = db.scalars(select(m.ReviewAction).where(m.ReviewAction.invoice_id == inv.id)
                         .order_by(m.ReviewAction.id)).all()
    vendor_pos = []
    if inv.vendor_id:
        consumed = consumption_by_line(db, exclude_invoice_id=inv.id)
        for p in db.scalars(select(m.PurchaseOrder).where(m.PurchaseOrder.vendor_id == inv.vendor_id)
                            .options(selectinload(m.PurchaseOrder.lines), selectinload(m.PurchaseOrder.vendor))):
            sn = po_snapshot(p, consumed)
            vendor_pos.append({k: sn[k] for k in ("id", "po_number", "status", "description", "po_date", "currency",
                                                  "subtotal", "remaining_amount")})
    return ok({"invoice": _invoice_summary(inv), "run": _run_payload(run), "runs": list(runs), "po": po,
               "duplicate_of": dup_ref, "vendor_pos": vendor_pos,
               "reviews": [{"id": r.id, "action": r.action_type, "reason": r.reason, "user": r.user_name,
                            "before": r.before, "after": r.after, "created_at": r.created_at} for r in reviews],
               "corrections": inv.corrections, "confirmed_po_id": inv.confirmed_po_id,
               "duplicate_resolution": inv.duplicate_resolution, "ai_disclaimer": AI_DISCLAIMER})


@router.get("/invoices/{invoice_id}/file")
def invoice_file(invoice_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    doc = db.get(m.Document, inv.document_id)
    return Response(doc.content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="invoice-{inv.id}.pdf"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/invoices/{invoice_id}/runs/{run_no}")
def invoice_run(invoice_id: int, run_no: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    run = db.scalar(select(m.ProcessingRun).where(m.ProcessingRun.invoice_id == invoice_id,
                                                  m.ProcessingRun.run_no == run_no))
    if not run:
        err(404, "NOT_FOUND", "Run not found.")
    return ok(_run_payload(run))


@router.get("/invoices/{invoice_id}/audit")
def invoice_audit(invoice_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    _get_invoice(db, invoice_id)
    events = db.scalars(select(m.AuditEvent).where(m.AuditEvent.invoice_id == invoice_id).order_by(m.AuditEvent.seq))
    return ok([{"seq": e.seq, "run_no": e.run_no, "event_type": e.event_type, "actor": e.actor, "message": e.message,
                "payload": e.payload, "created_at": e.created_at} for e in events])


@router.get("/invoices/{invoice_id}/report")
def invoice_report(invoice_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    run = latest_run(db, inv.id)
    events = db.scalars(select(m.AuditEvent).where(m.AuditEvent.invoice_id == invoice_id).order_by(m.AuditEvent.seq))
    report = {"invoice": _invoice_summary(inv), "final_decision": inv.decision, "decided_by": inv.decided_by,
              "run": _run_payload(run),
              "audit_trail": [{"seq": e.seq, "event": e.event_type, "actor": e.actor, "message": e.message,
                               "at": e.created_at} for e in events]}
    body = json.dumps(jsonable(report), indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="decision-report-{inv.id}.json"'})


class ReasonBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class ReprocessBody(BaseModel):
    reason: str = "Re-run requested"
    re_extract: bool = False


@router.post("/invoices/{invoice_id}/reprocess")
def reprocess(invoice_id: int, body: ReprocessBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    process_invoice(db, inv.id, "RE_EXTRACT" if body.re_extract else "REPROCESS", user)
    return invoice_detail(invoice_id, db, user)


class FieldsBody(BaseModel):
    fields: dict[str, str | None] = {}
    line_items: list[dict] | None = None
    reason: str = Field(min_length=3, max_length=2000)


@router.patch("/invoices/{invoice_id}/fields")
def patch_fields(invoice_id: int, body: FieldsBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    from ..schemas import HEADER_FIELDS

    bad = [k for k in body.fields if k not in HEADER_FIELDS]
    if bad:
        err(422, "UNKNOWN_FIELD", f"Unknown field(s): {', '.join(bad)}")
    correct_fields(db, inv, body.fields, body.line_items, body.reason, user)
    return invoice_detail(invoice_id, db, user)


class ConfirmPOBody(BaseModel):
    po_number: str
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/invoices/{invoice_id}/confirm-po")
def confirm_po_route(invoice_id: int, body: ConfirmPOBody, db: Session = Depends(get_db),
                     user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    po = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.po_number == body.po_number.strip().upper()))
    if not po:
        err(404, "PO_NOT_FOUND", f"PO {body.po_number} not found.")
    if inv.vendor_id and po.vendor_id != inv.vendor_id:
        err(422, "PO_VENDOR_MISMATCH", f"{po.po_number} belongs to a different vendor.")
    confirm_po(db, inv, po, body.reason, user)
    return invoice_detail(invoice_id, db, user)


class DuplicateBody(BaseModel):
    resolution: Literal["NOT_DUPLICATE", "IS_DUPLICATE"]
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/invoices/{invoice_id}/resolve-duplicate")
def resolve_dup(invoice_id: int, body: DuplicateBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    resolve_duplicate(db, inv, body.resolution, body.reason, user)
    return invoice_detail(invoice_id, db, user)


class FinalBody(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/invoices/{invoice_id}/final-decision")
def final(invoice_id: int, body: FinalBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    inv = _get_invoice(db, invoice_id)
    if inv.status != "PROCESSED":
        err(409, "NOT_PROCESSED", "The invoice has not finished processing.")
    final_decision(db, inv, body.decision, body.reason, user)
    return invoice_detail(invoice_id, db, user)


# ============================================================================ dashboard

@router.get("/dashboard/stats")
def stats(db: Session = Depends(get_db), user: str = Depends(current_user)):
    invs = db.scalars(select(m.Invoice).where(~m.Invoice.file_sha256.like("historical-%"))).all()
    by = {"APPROVED": 0, "REJECTED": 0, "MANUAL_REVIEW": 0}
    value: dict[str, Decimal] = {}
    value_by_decision: dict[str, dict[str, Decimal]] = {}
    for i in invs:
        if i.decision in by:
            by[i.decision] += 1
        if i.grand_total is not None and i.currency:
            value[i.currency] = value.get(i.currency, Decimal(0)) + i.grand_total
            if i.decision:
                d = value_by_decision.setdefault(i.decision, {})
                d[i.currency] = d.get(i.currency, Decimal(0)) + i.grand_total
    activity = db.scalars(select(m.AuditEvent).where(m.AuditEvent.event_type.in_(
        ["DOCUMENT_UPLOADED", "DECISION_GENERATED", "HUMAN_ACTION", "FINAL_DECISION", "PROCESSING_FAILED"]))
        .order_by(m.AuditEvent.id.desc()).limit(15)).all()
    names = {i.id: i.original_filename for i in invs}
    return ok({"total": len(invs), "approved": by["APPROVED"], "rejected": by["REJECTED"],
               "manual_review": by["MANUAL_REVIEW"],
               "processing": sum(1 for i in invs if i.status in ("RECEIVED", "PROCESSING")),
               "failed": sum(1 for i in invs if i.status == "FAILED"),
               "human_decided": sum(1 for i in invs if i.decided_by == "HUMAN"),
               "total_value": value, "value_by_decision": value_by_decision,
               "recent_activity": [{"invoice_id": e.invoice_id, "filename": names.get(e.invoice_id),
                                    "event_type": e.event_type, "message": e.message, "actor": e.actor,
                                    "created_at": e.created_at} for e in activity]})


# ============================================================================ master data

@router.get("/vendors")
def vendors(db: Session = Depends(get_db), user: str = Depends(current_user)):
    rows = db.scalars(select(m.Vendor).order_by(m.Vendor.code)).all()
    counts = dict(db.execute(select(m.PurchaseOrder.vendor_id, func.count()).group_by(m.PurchaseOrder.vendor_id)).all())
    return ok([{"id": v.id, "code": v.code, "name": v.name, "tax_id": v.tax_id, "tax_id_type": v.tax_id_type,
                "country": v.country, "status": v.status, "default_currency": v.default_currency, "address": v.address,
                "po_count": counts.get(v.id, 0)} for v in rows])


@router.get("/purchase-orders")
def purchase_orders(db: Session = Depends(get_db), user: str = Depends(current_user)):
    consumed = consumption_by_line(db)
    pos = db.scalars(select(m.PurchaseOrder).options(selectinload(m.PurchaseOrder.lines),
                                                     selectinload(m.PurchaseOrder.vendor)).order_by(m.PurchaseOrder.id))
    return ok([po_snapshot(p, consumed) for p in pos])


@router.get("/purchase-orders/{po_number}")
def purchase_order(po_number: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    p = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.po_number == po_number.upper())
                  .options(selectinload(m.PurchaseOrder.lines), selectinload(m.PurchaseOrder.vendor)))
    if not p:
        err(404, "NOT_FOUND", f"PO {po_number} not found.")
    snap = po_snapshot(p, consumption_by_line(db))
    line_ids = {ln.id: ln.line_no for ln in p.lines}
    rows = db.execute(select(m.POConsumption, m.Invoice).join(m.Invoice, m.Invoice.id == m.POConsumption.invoice_id)
                      .where(m.POConsumption.po_line_id.in_(line_ids))).all()
    snap["consumption"] = [{"invoice_id": inv.id, "filename": inv.original_filename,
                            "invoice_number": inv.invoice_number, "po_line_no": line_ids[c.po_line_id],
                            "quantity": c.quantity, "amount": c.amount, "status": c.status,
                            "created_at": c.created_at} for c, inv in rows]
    return ok(snap)


# ============================================================================ rules

@router.get("/config/rules")
def get_rules(db: Session = Depends(get_db), user: str = Depends(current_user)):
    ov = db.scalar(select(m.RuleOverride).order_by(m.RuleOverride.id.desc()).limit(1))
    return ok({"effective": effective_rules(db), "defaults": load_default_rules(),
               "overrides": ov.overrides if ov else {}, "updated_by": ov.updated_by if ov else None,
               "updated_at": ov.updated_at if ov else None})


class RulesBody(BaseModel):
    overrides: dict


@router.put("/config/rules")
def put_rules(body: RulesBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    defaults = load_default_rules()

    def validate(o, d, path=""):
        for k, v in o.items():
            if k not in d:
                err(422, "UNKNOWN_RULE", f"Unknown rule {path}{k}")
            if isinstance(v, dict):
                if not isinstance(d[k], dict):
                    err(422, "INVALID_RULE", f"{path}{k} is not a group")
                validate(v, d[k], f"{path}{k}.")
            elif isinstance(d[k], (int, float)) and not isinstance(d[k], bool):
                if not isinstance(v, (int, float)) or v < 0:
                    err(422, "INVALID_RULE", f"{path}{k} must be a non-negative number")

    validate(body.overrides, defaults)
    db.add(m.RuleOverride(overrides=body.overrides, updated_by=user))
    db.commit()
    return get_rules(db, user)


# ============================================================================ admin / demo

@router.post("/admin/reset-demo")
def reset_demo(db: Session = Depends(get_db), user: str = Depends(current_user)):
    return seed_demo(db, reset=True)


class ImportBody(BaseModel):
    vendors: list[dict] = []
    purchase_orders: list[dict] = []
    replace: bool = False


@router.post("/admin/import")
def import_data(body: ImportBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    try:
        return import_procurement(db, body.model_dump(), replace=body.replace)
    except (KeyError, ValueError) as e:
        db.rollback()
        err(422, "IMPORT_FAILED", f"Import failed: {e}")


@router.get("/admin/sample-invoices")
def samples(user: str = Depends(current_user)):
    if not SAMPLES_DIR.exists():
        return []
    return [p.name for p in sorted(SAMPLES_DIR.glob("*.pdf"))]


@router.get("/admin/sample-invoices/{name}")
def sample_file(name: str, user: str = Depends(current_user)):
    path = (SAMPLES_DIR / name).resolve()
    if path.parent != SAMPLES_DIR.resolve() or not path.exists() or path.suffix != ".pdf":
        err(404, "NOT_FOUND", "Sample not found.")
    return Response(path.read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})
