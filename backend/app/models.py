"""Persistence model.

Invoice 1─N ProcessingRun (each run stores extraction, validation, matching, duplicate and
decision results as JSON, plus the rules snapshot it used). Human corrections create a new run,
so history is never overwritten. PO consumption is a ledger: remaining balance is always
computed from ACTIVE rows, never stored.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, DecimalString


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    tax_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    tax_id_type: Mapped[str | None] = mapped_column(String(16))  # GSTIN | EU_VAT | US_EIN
    country: Mapped[str] = mapped_column(String(2), default="IN")
    address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="APPROVED")  # APPROVED | BLOCKED | PENDING
    default_currency: Mapped[str] = mapped_column(String(3), default="INR")
    # Payment / compliance master data
    bank_account: Mapped[str | None] = mapped_column(String(40))       # account no. or IBAN, no spaces
    bank_ifsc: Mapped[str | None] = mapped_column(String(16))          # IFSC (India) / SWIFT / routing no.
    einvoice_required: Mapped[bool | None] = mapped_column(Boolean, default=False)  # GST e-invoicing (IRN + QR)
    tds_section: Mapped[str | None] = mapped_column(String(8))         # 194C | 194J | 194I | 194Q | None
    purchase_orders: Mapped[list[PurchaseOrder]] = relationship(back_populates="vendor")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    po_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="APPROVED")  # APPROVED | CANCELLED | CLOSED
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    description: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[object] = mapped_column(DecimalString)
    tax_total: Mapped[object] = mapped_column(DecimalString)
    total_amount: Mapped[object] = mapped_column(DecimalString)
    qty_tolerance_pct: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    price_tolerance_pct: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    amount_tolerance_pct: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    vendor: Mapped[Vendor] = relationship(back_populates="purchase_orders")
    lines: Mapped[list[POLine]] = relationship(back_populates="po", order_by="POLine.line_no",
                                               cascade="all, delete-orphan")


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"))
    line_no: Mapped[int] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    uom: Mapped[str | None] = mapped_column(String(16))
    quantity: Mapped[object] = mapped_column(DecimalString)
    unit_price: Mapped[object] = mapped_column(DecimalString)
    tax_rate: Mapped[object] = mapped_column(DecimalString)
    line_amount: Mapped[object] = mapped_column(DecimalString)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class Document(Base):
    """Uploaded file bytes live in the DB, so hosting needs no persistent disk."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(64), default="application/pdf")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    is_scanned: Mapped[bool | None] = mapped_column(Boolean)

    status: Mapped[str] = mapped_column(String(16), default="RECEIVED")  # RECEIVED|PROCESSING|PROCESSED|FAILED
    stage: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)

    decision: Mapped[str | None] = mapped_column(String(16))           # latest system/human decision
    decided_by: Mapped[str | None] = mapped_column(String(16))         # SYSTEM | HUMAN
    system_decision: Mapped[str | None] = mapped_column(String(16))

    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))

    # Denormalised, normalised header used for duplicate search and the dashboard
    vendor_tax_id: Mapped[str | None] = mapped_column(String(32), index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    invoice_number_norm: Mapped[str | None] = mapped_column(String(64), index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    grand_total: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    po_reference: Mapped[str | None] = mapped_column(String(40))
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    document_type: Mapped[str | None] = mapped_column(String(16), default="INVOICE")  # INVOICE | CREDIT_NOTE | DEBIT_NOTE
    original_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))  # for credit / debit notes
    bank_account: Mapped[str | None] = mapped_column(String(40))       # bank account printed on the document
    bank_ifsc: Mapped[str | None] = mapped_column(String(16))
    tds_section: Mapped[str | None] = mapped_column(String(8))
    tds_base: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    tds_amount: Mapped[object | None] = mapped_column(DecimalString, nullable=True)
    approval_level: Mapped[str | None] = mapped_column(String(8))     # AUTO | MANAGER (from the latest run)
    risk_flags: Mapped[list | None] = mapped_column(JSON)              # fraud / control flags of the latest run

    # Human-review overrides carried into re-runs
    confirmed_po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))
    duplicate_resolution: Mapped[str | None] = mapped_column(String(16))  # NOT_DUPLICATE | IS_DUPLICATE
    corrections: Mapped[dict | None] = mapped_column(JSON)               # field -> value

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    runs: Mapped[list[ProcessingRun]] = relationship(back_populates="invoice", order_by="ProcessingRun.run_no",
                                                     cascade="all, delete-orphan")
    vendor: Mapped[Vendor | None] = relationship(foreign_keys=[vendor_id])
    po: Mapped[PurchaseOrder | None] = relationship(foreign_keys=[po_id])
    original_invoice: Mapped[Invoice | None] = relationship(foreign_keys=[original_invoice_id], remote_side=[id])


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    run_no: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(32))   # INITIAL | CORRECTION | PO_CONFIRMED | DUPLICATE_RESOLVED | REPROCESS
    rules_snapshot: Mapped[dict] = mapped_column(JSON)
    rules_hash: Mapped[str] = mapped_column(String(16))
    extraction: Mapped[dict | None] = mapped_column(JSON)
    validation: Mapped[list | None] = mapped_column(JSON)
    matching: Mapped[dict | None] = mapped_column(JSON)
    duplicates: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[dict | None] = mapped_column(JSON)
    controls: Mapped[dict | None] = mapped_column(JSON)   # approval level, fraud signals, e-invoice, TDS, notes
    ai_summary: Mapped[dict | None] = mapped_column(JSON)
    llm_calls: Mapped[list | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoice: Mapped[Invoice] = relationship(back_populates="runs")


class POConsumption(Base):
    __tablename__ = "po_consumption"
    __table_args__ = (UniqueConstraint("po_line_id", "invoice_id", name="uq_consumption_line_invoice"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"))
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    quantity: Mapped[object] = mapped_column(DecimalString)
    amount: Mapped[object] = mapped_column(DecimalString)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | REVERSED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    po_line: Mapped[POLine] = relationship()


class AuditEvent(Base):
    """Append-only. There is no API to update or delete audit rows."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    run_no: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(64), default="SYSTEM")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewAction(Base):
    __tablename__ = "review_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    user_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuleOverride(Base):
    __tablename__ = "rule_overrides"
    id: Mapped[int] = mapped_column(primary_key=True)
    overrides: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserProfile(Base):
    """Display name, designation and photo a signed-in user chose for themselves (accounts come from config)."""
    __tablename__ = "user_profiles"
    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(80))
    designation: Mapped[str | None] = mapped_column(String(80))
    avatar: Mapped[str | None] = mapped_column(Text)   # small data:image/... URL, resized in the browser
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
