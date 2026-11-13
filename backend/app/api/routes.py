from __future__ import annotations

import json
import re
import logging
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..config import deep_merge, get_settings, load_default_rules
from .. import email_intake
from ..csv_import import TEMPLATE as CSV_TEMPLATE
from ..csv_import import parse_procurement_csv
from ..workspaces import create_workspace, get_db, sweep_expired
from ..extraction.parsers import normalize_ref
from ..extraction.pdf_reader import DocumentError, ocr_available
from ..ingestion import ingest
from ..jobs import process_many
from ..ledger import consumption_by_line, po_snapshot
from ..llm import AI_DISCLAIMER
from ..notes import adjustments
from ..pipeline import (ManagerApprovalRequired, POBalanceExceeded, confirm_po, manager_required, correct_fields, effective_rules, final_decision, latest_run,
                        resolve_duplicate)
from ..audit import log as audit_log
from ..money import supported_currencies
from ..seed import import_procurement, po_from_dict, seed_demo
from .auth import bearer, check_credentials, current_user, is_guest, issue_token, token_payload, user_info, user_role

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def jsonable(obj: Any) -> Any:
    """Decimals are serialised as strings so no precision is lost to floats."""
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime) and obj.tzinfo is None:
        # timestamps are stored in UTC; SQLite drops the zone, so restore it or browsers read local time
        return obj.replace(tzinfo=timezone.utc).isoformat()
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


_failed_logins: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _throttle(client: str) -> None:
    """Simple per-client brute-force guard: N failures inside the window lock that client out until
    the oldest failure ages out."""
    s = get_settings()
    now = time.monotonic()
    with _login_lock:
        recent = [t for t in _failed_logins.get(client, []) if now - t < s.login_window_seconds]
        _failed_logins[client] = recent
        if len(recent) >= s.login_max_attempts:
            wait = int(s.login_window_seconds - (now - recent[0])) + 1
            err(429, "TOO_MANY_ATTEMPTS", f"Too many failed sign-ins. Try again in {wait} seconds.")


@router.post("/auth/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    client = request.client.host if request.client else "unknown"
    _throttle(client)
    if not check_credentials(body.username, body.password):
        with _login_lock:
            _failed_logins.setdefault(client, []).append(time.monotonic())
        err(401, "INVALID_CREDENTIALS", "Incorrect username or password.")
    with _login_lock:
        _failed_logins.pop(client, None)
    return {"token": issue_token(body.username), "user": user_info(body.username, db.get(m.UserProfile, body.username))}


_guest_starts: dict[str, list[float]] = {}


@router.post("/auth/guest")
def guest_login(request: Request):
    """'Continue as guest': a private sandbox seeded with the demo data, signed in as a reviewer."""
    s = get_settings()
    if not s.guest_mode:
        err(403, "GUEST_DISABLED", "Guest access is switched off on this instance.")
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _login_lock:
        recent = [x for x in _guest_starts.get(client, []) if now - x < 3600]
        if len(recent) >= s.guest_per_ip_per_hour:
            err(429, "TOO_MANY_GUESTS", "Too many guest sessions started from this network - try again later "
                                        "or keep using your current one.")
        recent.append(now)
        _guest_starts[client] = recent
    sweep_expired()
    db = next(get_db(None))
    try:
        live = db.scalar(select(func.count()).select_from(m.GuestWorkspace)) or 0
    finally:
        db.close()
    if live >= s.guest_max_workspaces:
        err(503, "GUEST_FULL", "All guest sandboxes are in use right now. Please try again later.")
    ws = create_workspace(client)
    return {"token": issue_token("guest-reviewer", ws), "user": user_info("guest-reviewer"),
            "guest": {"expires_hours": s.guest_ttl_hours}}


class GuestRoleBody(BaseModel):
    role: Literal["REVIEWER", "MANAGER"]


@router.post("/auth/guest/role")
def guest_role(body: GuestRoleBody, creds=Depends(bearer), db: Session = Depends(get_db)):
    """Guests can switch between the reviewer and manager view of their own sandbox."""
    payload = token_payload(creds.credentials) if creds else None
    if not payload or not payload.get("w") or not is_guest(payload["u"]):
        err(403, "NOT_A_GUEST", "Only guest sessions can switch role.")
    username = "guest-manager" if body.role == "MANAGER" else "guest-reviewer"
    return {"token": issue_token(username, payload["w"]), "user": user_info(username, db.get(m.UserProfile, username))}


@router.get("/auth/me")
def me(db: Session = Depends(get_db), user: str = Depends(current_user)):
    return user_info(user, db.get(m.UserProfile, user))


class ProfileBody(BaseModel):
    name: str | None = Field(None, max_length=80)
    designation: str | None = Field(None, max_length=80)
    avatar: str | None = Field(None, max_length=400_000)   # data URL; "" removes the photo


@router.patch("/auth/me")
def update_me(body: ProfileBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Change the display name, designation and photo shown in the sidebar (the account itself is unchanged)."""
    prof = db.get(m.UserProfile, user) or m.UserProfile(username=user)
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            err(422, "INVALID_PROFILE", "Name cannot be empty.")
        prof.display_name = name
    if "designation" in data:
        prof.designation = (data["designation"] or "").strip() or None
    if "avatar" in data:
        av = data["avatar"] or None
        if av and not av.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
            err(422, "INVALID_PROFILE", "The photo must be a PNG, JPEG or WebP image.")
        prof.avatar = av
    db.add(prof)
    audit_log(db, None, "PROFILE_UPDATED", f"{user} updated their profile ({', '.join(data) or 'nothing'}).",
              {"fields": list(data)}, None, user)
    db.commit()
    return user_info(user, prof)


@router.get("/health")
def health(db: Session = Depends(get_db)):
    s = get_settings()
    db.execute(select(1))
    return {"ok": True, "ocr_available": ocr_available(s.tesseract_cmd), "ai_configured": s.llm_available,
            "ai_model": s.ai_model, "ai_provider": s.ai_provider,
            "ai_fast_model": (s.openai_fast_model or s.openai_model) if s.ai_provider else None,
            "database": "postgresql" if s.database_url.startswith("postgresql") else "sqlite",
            "email_intake": bool(s.imap_host), "insecure_defaults": s.insecure_defaults,
            "guest_mode": s.guest_mode, "guest_ttl_hours": s.guest_ttl_hours}


# ============================================================================ invoices

async def _read_upload(file: UploadFile) -> bytes:
    """Read at most the size limit (+1 byte) so an oversized upload is refused without buffering all of it."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        err(413, "FILE_TOO_LARGE", f"{file.filename}: the file exceeds the {get_settings().max_upload_mb} MB limit.")
    return data


def _invoice_summary(inv: m.Invoice) -> dict:
    return {"id": inv.id, "filename": inv.original_filename, "status": inv.status, "stage": inv.stage,
            "error": inv.error, "decision": inv.decision, "decided_by": inv.decided_by,
            "system_decision": inv.system_decision, "vendor_name": inv.vendor_name, "vendor_tax_id": inv.vendor_tax_id,
            "invoice_number": inv.invoice_number, "invoice_date": inv.invoice_date, "currency": inv.currency,
            "grand_total": inv.grand_total, "po_number": inv.po.po_number if inv.po else inv.po_reference,
            "is_scanned": inv.is_scanned, "page_count": inv.page_count, "created_at": inv.created_at,
            "document_type": inv.document_type or "INVOICE", "original_invoice_id": inv.original_invoice_id,
            "approval_level": inv.approval_level, "risk_flags": inv.risk_flags or [],
            "tds_section": inv.tds_section, "tds_amount": inv.tds_amount,
            "updated_at": inv.updated_at}


def _guest_quota(db: Session, user: str, adding: int) -> None:
    """Guests share the AI budget, so each sandbox may hold a limited number of invoices."""
    if not is_guest(user):
        return
    cap = get_settings().guest_max_invoices
    have = db.scalar(select(func.count()).select_from(m.Invoice)
                     .where(~m.Invoice.file_sha256.like("historical-%"))) or 0
    if have + adding > cap:
        err(429, "GUEST_LIMIT", f"Guest sandboxes hold up to {cap} invoices ({have} uploaded). "
                                "Use 'Reset demo' on Rules & data to start over.")


@router.post("/invoices", status_code=202)
async def upload_invoice(background: BackgroundTasks, file: UploadFile = File(...), db: Session = Depends(get_db),
                         user: str = Depends(current_user)):
    _guest_quota(db, user, 1)
    data = await _read_upload(file)
    try:
        inv = ingest(db, data, file.filename, file.content_type, user)
    except DocumentError as e:
        err(400, e.code, e.message)
    background.add_task(process_many, [inv.id], "INITIAL", user, db.info.get("workspace"))
    return ok(_invoice_summary(inv), 202)


MAX_BATCH = 25


@router.post("/invoices/batch", status_code=202)
async def upload_batch(background: BackgroundTasks, files: list[UploadFile] = File(...),
                       db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Several PDFs at once. Each file is validated on its own; accepted ones are processed one after
    another (in upload order, so split invoices and duplicates are evaluated in sequence)."""
    if len(files) > MAX_BATCH:
        err(400, "TOO_MANY_FILES", f"Upload at most {MAX_BATCH} files at a time.")
    _guest_quota(db, user, len(files))
    accepted, rejected = [], []
    for f in files:
        try:
            data = await _read_upload(f)
            inv = ingest(db, data, f.filename, f.content_type, user)
            accepted.append(_invoice_summary(inv))
        except DocumentError as e:
            db.rollback()
            rejected.append({"filename": f.filename, "error_code": e.code, "message": e.message})
        except HTTPException as e:
            rejected.append({"filename": f.filename, **(e.detail if isinstance(e.detail, dict) else {})})
    if accepted:
        background.add_task(process_many, [a["id"] for a in accepted], "INITIAL", user, db.info.get("workspace"))
    return ok({"accepted": accepted, "rejected": rejected}, 202)


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


def ai_usage(runs) -> dict:
    """Token and cost totals over processing runs (cost is known for providers with configured prices)."""
    calls = [c for r in runs for c in (r.llm_calls or []) if not c.get("cached")]
    made = [c for c in calls if c.get("input_tokens") is not None]
    runs_with_ai = sum(1 for r in runs if any(c.get("input_tokens") is not None for c in (r.llm_calls or [])))
    cost = sum(c.get("cost_usd") or 0 for c in made)
    return {"runs": runs_with_ai, "calls": len(made), "failed_calls": sum(1 for c in calls if not c.get("ok")),
            "input_tokens": sum(c.get("input_tokens") or 0 for c in made),
            "output_tokens": sum(c.get("output_tokens") or 0 for c in made),
            "cost_usd": round(cost, 6) if any(c.get("cost_usd") is not None for c in made) else None,
            "avg_cost_per_run_usd": round(cost / runs_with_ai, 6)
            if runs_with_ai and any(c.get("cost_usd") is not None for c in made) else None}


def _run_payload(run: m.ProcessingRun | None) -> dict | None:
    if not run:
        return None
    calls = run.llm_calls or []
    return {"run_no": run.run_no, "trigger": run.trigger, "rules_hash": run.rules_hash, "started_at": run.started_at,
            "finished_at": run.finished_at, "extraction": (run.extraction or {}).get("result"),
            "validation": run.validation, "matching": run.matching, "duplicates": run.duplicates,
            "decision": run.decision, "controls": run.controls, "ai_summary": run.ai_summary,
            "ai_calls": [{k: c.get(k) for k in ("purpose", "model", "served_by", "ok", "error", "latency_ms",
                                                "input_tokens", "output_tokens", "cached", "cached_input_tokens",
                                                "reasoning_tokens", "cost_usd")} for c in calls],
            "ai_usage": ai_usage([run]),
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
               "duplicate_resolution": inv.duplicate_resolution, "ai_disclaimer": AI_DISCLAIMER,
               "adjustments": adjustments(db, inv),
               "original_invoice": _invoice_summary(inv.original_invoice) if inv.original_invoice else None,
               "viewer": user_info(user, db.get(m.UserProfile, user)),
               "manager_required": manager_required(db, run)})


@router.get("/invoices/{invoice_id}/progress")
def invoice_progress(invoice_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Small payload for the live run view: current stage plus every audit event of the current run
    (each stage commits as it finishes, so this fills in while the pipeline is running)."""
    inv = _get_invoice(db, invoice_id)
    run = latest_run(db, inv.id)
    stmt = select(m.AuditEvent).where(m.AuditEvent.invoice_id == invoice_id).order_by(m.AuditEvent.seq)
    if run:
        stmt = stmt.where((m.AuditEvent.run_no == run.run_no) | (m.AuditEvent.run_no.is_(None)))
    events = db.scalars(stmt).all()
    return ok({"invoice": _invoice_summary(inv), "run_no": run.run_no if run else None,
               "started_at": run.started_at if run else None, "finished_at": run.finished_at if run else None,
               "events": [{"seq": e.seq, "event_type": e.event_type, "message": e.message, "actor": e.actor,
                           "created_at": e.created_at} for e in events]})


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


@router.post("/invoices/{invoice_id}/reprocess", status_code=202)
def reprocess(invoice_id: int, body: ReprocessBody, background: BackgroundTasks, db: Session = Depends(get_db),
              user: str = Depends(current_user)):
    """Runs in the background so the live run view can follow it; poll /progress or the detail."""
    inv = _get_invoice(db, invoice_id)
    if inv.status in ("RECEIVED", "PROCESSING"):
        err(409, "ALREADY_PROCESSING", "This invoice is already being processed.")
    inv.status, inv.stage = "PROCESSING", "QUEUED"
    db.commit()
    background.add_task(process_many, [inv.id], "RE_EXTRACT" if body.re_extract else "REPROCESS", user,
                        db.info.get("workspace"))
    res = invoice_detail(invoice_id, db, user)
    res.status_code = 202
    return res


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
    if not inv.vendor_id and inv.vendor_tax_id and po.vendor.tax_id \
            and normalize_ref(inv.vendor_tax_id) != normalize_ref(po.vendor.tax_id):
        # vendor not in the master data: the printed tax ID must still agree with the PO's vendor
        err(422, "PO_VENDOR_MISMATCH", f"{po.po_number} belongs to {po.vendor.name} (tax ID {po.vendor.tax_id}), "
                                       f"but the invoice shows tax ID {inv.vendor_tax_id}.")
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
    try:
        final_decision(db, inv, body.decision, body.reason, user, user_role(user))
    except ManagerApprovalRequired as e:
        db.rollback()
        err(403, "MANAGER_APPROVAL_REQUIRED", e.message)
    except POBalanceExceeded as e:
        db.rollback()
        err(409, "PO_BALANCE_EXCEEDED", e.message, e.evidence)
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
            amt = -abs(i.grand_total) if i.document_type == "CREDIT_NOTE" else i.grand_total  # credits reduce value
            value[i.currency] = value.get(i.currency, Decimal(0)) + amt
            if i.decision:
                d = value_by_decision.setdefault(i.decision, {})
                d[i.currency] = d.get(i.currency, Decimal(0)) + amt
    activity = db.scalars(select(m.AuditEvent).where(m.AuditEvent.event_type.in_(
        ["DOCUMENT_UPLOADED", "DECISION_GENERATED", "HUMAN_ACTION", "FINAL_DECISION", "PROCESSING_FAILED"]))
        .order_by(m.AuditEvent.id.desc()).limit(15)).all()
    names = {i.id: i.original_filename for i in invs}
    return ok({"total": len(invs), "approved": by["APPROVED"], "rejected": by["REJECTED"],
               "manual_review": by["MANUAL_REVIEW"],
               "processing": sum(1 for i in invs if i.status in ("RECEIVED", "PROCESSING")),
               "failed": sum(1 for i in invs if i.status == "FAILED"),
               "human_decided": sum(1 for i in invs if i.decided_by == "HUMAN"),
               "awaiting_manager": sum(1 for i in invs if i.approval_level == "MANAGER" and i.decision == "MANUAL_REVIEW"),
               "risk_flagged": sum(1 for i in invs if i.risk_flags),
               "notes": sum(1 for i in invs if (i.document_type or "INVOICE") != "INVOICE"),
               "tds_withheld": {"INR": sum((i.tds_amount for i in invs if i.decision == "APPROVED" and i.tds_amount),
                                           Decimal(0))},
               "ai_usage": ai_usage(db.scalars(select(m.ProcessingRun)).all()),
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
                "po_count": counts.get(v.id, 0), "bank_account": v.bank_account, "bank_ifsc": v.bank_ifsc,
                "einvoice_required": bool(v.einvoice_required), "tds_section": v.tds_section} for v in rows])


class VendorPatch(BaseModel):
    bank_account: str | None = Field(None, max_length=40)
    bank_ifsc: str | None = Field(None, max_length=16)
    einvoice_required: bool | None = None
    tds_section: str | None = Field(None, max_length=8)
    reason: str = Field(min_length=3, max_length=500)


@router.patch("/vendors/{code}")
def update_vendor(code: str, body: VendorPatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Payment / compliance master data. Bank details are a fraud target, so only a manager may change them."""
    v = db.scalar(select(m.Vendor).where(m.Vendor.code == code))
    if not v:
        err(404, "NOT_FOUND", f"Vendor {code} not found.")
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    if not changes:
        err(422, "NOTHING_TO_CHANGE", "No fields to change.")
    if ({"bank_account", "bank_ifsc"} & set(changes)) and user_role(user) != "MANAGER":
        err(403, "MANAGER_REQUIRED", "Only a manager can change a vendor's bank details.")
    if "tds_section" in changes and changes["tds_section"]:
        sections = effective_rules(db)["tds"]["sections"]
        changes["tds_section"] = changes["tds_section"].strip().upper()
        if changes["tds_section"] not in sections:
            err(422, "UNKNOWN_TDS_SECTION", f"TDS section must be one of {', '.join(sections)}.")
    for k in ("bank_account", "bank_ifsc"):
        if k in changes and changes[k] is not None:
            changes[k] = "".join(changes[k].split()).replace("-", "").upper() or None
    before = {k: getattr(v, k) for k in changes}
    for k, val in changes.items():
        setattr(v, k, bool(val) if k == "einvoice_required" else (val or None))
    masked = lambda d: {k: ("••••" + str(x)[-4:] if k == "bank_account" and x else x) for k, x in d.items()}  # noqa: E731
    audit_log(db, None, "VENDOR_UPDATED", f"{user} updated vendor {v.code} ({', '.join(changes)}): {body.reason}",
              {"vendor": v.code, "before": masked(before), "after": masked(changes)}, None, user)
    db.commit()
    return vendors(db, user)


@router.get("/purchase-orders")
def purchase_orders(db: Session = Depends(get_db), user: str = Depends(current_user)):
    consumed = consumption_by_line(db)
    pos = db.scalars(select(m.PurchaseOrder).options(selectinload(m.PurchaseOrder.lines),
                                                     selectinload(m.PurchaseOrder.vendor)).order_by(m.PurchaseOrder.id))
    return ok([po_snapshot(p, consumed) for p in pos])


class POLineIn(BaseModel):
    item_code: str | None = Field(None, max_length=40)
    description: str = Field(min_length=1, max_length=500)
    uom: str | None = Field(None, max_length=16)
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=4)
    unit_price: Decimal = Field(ge=0, max_digits=16, decimal_places=4)
    tax_rate: Decimal = Field(Decimal("0"), ge=0, le=100, decimal_places=3)


class POIn(BaseModel):
    po_number: str = Field(min_length=2, max_length=40, pattern=r"^\s*[A-Za-z0-9][A-Za-z0-9\-/_.]*\s*$")
    vendor_code: str = Field(min_length=1, max_length=32)
    po_date: date
    currency: str | None = Field(None, min_length=3, max_length=3)
    status: Literal["APPROVED", "CLOSED", "CANCELLED"] = "APPROVED"
    description: str | None = Field(None, max_length=500)
    qty_tolerance_pct: Decimal | None = Field(None, ge=0, le=100)
    price_tolerance_pct: Decimal | None = Field(None, ge=0, le=100)
    amount_tolerance_pct: Decimal | None = Field(None, ge=0, le=100)
    lines: list[POLineIn] = Field(min_length=1, max_length=100)


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(body: POIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Enter one PO by hand. Line amounts and totals are computed here (never taken from the client),
    and the PO is used by matching from the next invoice on."""
    number = body.po_number.strip().upper()
    if db.scalar(select(m.PurchaseOrder.id).where(m.PurchaseOrder.po_number == number)):
        err(409, "PO_EXISTS", f"{number} already exists.")
    vendor = db.scalar(select(m.Vendor).where(m.Vendor.code == body.vendor_code.strip()))
    if vendor is None:
        err(422, "UNKNOWN_VENDOR", f"Vendor {body.vendor_code} does not exist - add or import the vendor first.")
    if vendor.status == "BLOCKED":
        err(422, "VENDOR_BLOCKED", f"{vendor.name} is BLOCKED; a purchase order cannot be raised for it.")
    currency = (body.currency or vendor.default_currency).upper()
    allowed = supported_currencies(effective_rules(db))
    if currency not in allowed:
        err(422, "UNSUPPORTED_CURRENCY", f"Currency must be one of {', '.join(sorted(allowed))} - a manager can add "
                                         "more currencies (with an approval limit) on the Rules & data page.")
    if body.po_date > date.today():
        err(422, "FUTURE_DATE", "The PO date cannot be in the future.")
    po = po_from_dict(db, {
        "po_number": number, "vendor": vendor.code, "po_date": body.po_date.isoformat(), "status": body.status,
        "currency": currency, "description": (body.description or "").strip() or None,
        "qty_tolerance_pct": body.qty_tolerance_pct, "price_tolerance_pct": body.price_tolerance_pct,
        "amount_tolerance_pct": body.amount_tolerance_pct,
        "lines": [{"item_code": (ln.item_code or "").strip() or None, "description": ln.description.strip(),
                   "uom": (ln.uom or "").strip().upper() or None, "quantity": ln.quantity,
                   "unit_price": ln.unit_price, "tax_rate": ln.tax_rate} for ln in body.lines]},
        {vendor.code: vendor})
    db.flush()
    audit_log(db, None, "PO_CREATED", f"{number} created by {user} for {vendor.name}: {len(po.lines)} line(s), "
                                      f"net {po.subtotal} {currency}.", {"po_number": number}, None, user)
    db.commit()
    po = db.scalar(select(m.PurchaseOrder).where(m.PurchaseOrder.id == po.id)
                   .options(selectinload(m.PurchaseOrder.lines), selectinload(m.PurchaseOrder.vendor)))
    return ok(po_snapshot(po, consumption_by_line(db)), 201)


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
    eff = effective_rules(db)
    return ok({"effective": eff, "defaults": load_default_rules(), "supported_currencies": sorted(supported_currencies(eff)),
               "overrides": ov.overrides if ov else {}, "updated_by": ov.updated_by if ov else None,
               "updated_at": ov.updated_at if ov else None})


class RulesBody(BaseModel):
    overrides: dict


POLICY_VALUES = {"MANUAL_REVIEW", "REJECT"}
FLAG_ALLOWED = {"policy.near_limit", "policy.month_end_arrival", "policy.debit_note"}  # may also be FLAG (no review)
# values that are fractions / probabilities and must stay within 0..1
FRACTION_RULES = {"extraction.min_confidence", "duplicates.probable_threshold", "matching.line_min_similarity",
                  "matching.min_description_evidence"}


def validate_rule_overrides(overrides: dict, defaults: dict) -> None:
    """Every override must name an existing rule and keep that rule's type; numbers must be in range;
    policy outcomes must be MANUAL_REVIEW or REJECT; duplicate weights must still add up to 1."""
    def walk(o, d, path=""):
        if not isinstance(o, dict):
            err(422, "INVALID_RULE", f"{path or 'overrides'} must be an object")
        for k, v in o.items():
            name = f"{path}{k}"
            if path == "approval.auto_approve_limit.":
                # any ISO 4217 code; null removes a currency's limit (its invoices then need a manager)
                if not re.fullmatch(r"[A-Z]{3}", k):
                    err(422, "INVALID_RULE", f"{k!r} is not a 3-letter currency code (e.g. GBP)")
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                    err(422, "INVALID_RULE", f"The {k} approval limit must be a non-negative number")
                continue
            if k not in d:
                err(422, "UNKNOWN_RULE", f"Unknown rule {name}")
            ref = d[k]
            if isinstance(ref, dict):
                walk(v, ref, f"{name}.")
            elif isinstance(ref, bool):
                if not isinstance(v, bool):
                    err(422, "INVALID_RULE", f"{name} must be true or false")
            elif isinstance(ref, (int, float)):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    err(422, "INVALID_RULE", f"{name} must be a non-negative number")
                if name in FRACTION_RULES and v > 1:
                    err(422, "INVALID_RULE", f"{name} must be between 0 and 1")
            elif isinstance(ref, list):
                if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                    err(422, "INVALID_RULE", f"{name} must be a list of text values")
            elif isinstance(ref, str):
                if not isinstance(v, str) or not v.strip():
                    err(422, "INVALID_RULE", f"{name} must be text")
                allowed = POLICY_VALUES | ({"FLAG"} if name in FLAG_ALLOWED else set())
                if path == "policy." and v not in allowed:
                    err(422, "INVALID_RULE", f"{name} must be one of {', '.join(sorted(allowed))}")

    walk(overrides, defaults)
    merged = deep_merge(defaults, overrides)
    weights = merged["duplicates"]["weights"]
    if abs(sum(float(x) for x in weights.values()) - 1.0) > 0.001:
        err(422, "INVALID_RULE", f"duplicates.weights must add up to 1 (they add up to "
                                 f"{sum(float(x) for x in weights.values()):.2f})")
    if sum(float(x) for x in merged["matching"]["weights"].values()) <= 0:
        err(422, "INVALID_RULE", "matching.weights cannot all be zero")


@router.put("/config/rules")
def put_rules(body: RulesBody, db: Session = Depends(get_db), user: str = Depends(current_user)):
    defaults = load_default_rules()

    validate_rule_overrides(body.overrides, defaults)
    # Approval limits decide who may approve what, so a reviewer cannot raise their own limit.
    before, after = effective_rules(db), deep_merge(defaults, body.overrides)
    approval_keys = ("above_approval_limit", "near_limit", "split_below_limit")
    if after.get("approval", {}).get("above_limit_approver") not in ("REVIEWER", "MANAGER"):
        err(422, "INVALID_RULE", "approval.above_limit_approver must be REVIEWER or MANAGER")
    approval_changed = before.get("approval") != after.get("approval") or any(
        before["policy"].get(k) != after["policy"].get(k) for k in approval_keys)
    if approval_changed and user_role(user) != "MANAGER":
        err(403, "MANAGER_REQUIRED", "Only a manager can change the approval limits.")
    db.add(m.RuleOverride(overrides=body.overrides, updated_by=user))
    if approval_changed:
        audit_log(db, None, "APPROVAL_LIMITS_CHANGED",
                  f"{user} changed the approval limits: {before.get('approval', {}).get('auto_approve_limit')} -> "
                  f"{after['approval']['auto_approve_limit']}.",
                  {"before": before.get("approval"), "after": after["approval"]}, None, user)
    db.commit()
    return get_rules(db, user)


# ============================================================================ admin / demo

@router.post("/admin/reset-demo")
def reset_demo(db: Session = Depends(get_db), user: str = Depends(current_user)):
    if not is_guest(user) and not get_settings().allow_demo_reset:
        err(403, "RESET_DISABLED", "Resetting the data is disabled on this instance (ALLOW_DEMO_RESET=false).")
    return seed_demo(db, reset=True)  # a guest's session points at their own sandbox, so only it is reset


@router.post("/admin/import-csv")
async def import_csv(file: UploadFile = File(...), replace: bool = False, db: Session = Depends(get_db),
                     user: str = Depends(current_user)):
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        err(413, "FILE_TOO_LARGE", "The CSV exceeds 5 MB.")
    try:
        payload = parse_procurement_csv(data)
        result = import_procurement(db, payload, replace=replace)
    except UnicodeDecodeError:
        err(422, "IMPORT_FAILED", "The file is not UTF-8 text. Save it from your spreadsheet as 'CSV UTF-8'.")
    except (KeyError, ValueError) as e:
        db.rollback()
        err(422, "IMPORT_FAILED", f"Import failed: {e}")
    return {**result, "rows_read": sum(len(p["lines"]) for p in payload["purchase_orders"]),
            "pos_in_file": len(payload["purchase_orders"])}


@router.get("/admin/import-csv/template")
def csv_template(user: str = Depends(current_user)):
    return Response(CSV_TEMPLATE, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="purchase-orders-template.csv"'})


@router.get("/admin/email")
def email_status(user: str = Depends(current_user)):
    s = get_settings()
    if is_guest(user):
        return {"enabled": False, "guest": True, "last_error": None, "last_poll": None, "processed": 0}
    return {**email_intake.STATUS, "enabled": bool(s.imap_host), "mailbox": s.imap_user, "folder": s.imap_folder,
            "poll_seconds": s.imap_poll_seconds}


@router.post("/admin/email/poll")
def email_poll(user: str = Depends(current_user)):
    if is_guest(user):
        err(403, "GUEST_NOT_ALLOWED", "E-mail intake is not available in guest mode.")
    res = email_intake.poll_once()
    if not res["ok"]:
        err(409 if "configured" in res["error"] or "already" in res["error"] else 502, "EMAIL_POLL_FAILED",
            res["error"])
    return res


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
