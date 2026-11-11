"""Upload ingestion: validate the file, store it, create the invoice record."""
from __future__ import annotations

import hashlib
import re

from sqlalchemy.orm import Session

from . import models as m
from .audit import log
from .config import get_settings
from .extraction.pdf_reader import DocumentError, open_pdf


def safe_filename(name: str | None) -> str:
    base = (name or "invoice.pdf").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", base).strip() or "invoice.pdf"
    return base[:200]


def ingest(db: Session, data: bytes, filename: str | None, content_type: str | None, user: str) -> m.Invoice:
    settings = get_settings()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise DocumentError("FILE_TOO_LARGE", f"The file exceeds the {settings.max_upload_mb} MB limit.")
    name = safe_filename(filename)
    if not name.lower().endswith(".pdf") and content_type not in ("application/pdf", "application/x-pdf"):
        raise DocumentError("UNSUPPORTED_TYPE", "Only PDF invoices are supported.")
    doc = open_pdf(data, settings.max_pages)  # raises DocumentError for corrupt/encrypted/empty files
    pages = doc.page_count
    doc.close()
    sha = hashlib.sha256(data).hexdigest()
    stored = m.Document(content=data, content_type="application/pdf")
    db.add(stored)
    db.flush()
    inv = m.Invoice(original_filename=name, document_id=stored.id, file_sha256=sha, file_size=len(data),
                    page_count=pages, status="RECEIVED", stage="QUEUED")
    db.add(inv)
    db.flush()
    log(db, inv.id, "DOCUMENT_UPLOADED", f"{name} uploaded by {user} ({len(data) / 1024:.0f} KB, {pages} page(s)).",
        {"sha256": sha, "size": len(data)}, None, user)
    db.commit()
    return inv
