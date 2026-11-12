"""E-mail intake: invoices arrive as PDF attachments in an AP mailbox (IMAP).

Optional - enabled when IMAP_HOST is set. A background poller checks for UNSEEN messages, takes
every PDF attachment, ingests it exactly like an upload (same validation, same pipeline) and marks
the message as read. Sender and subject are kept in the audit trail. Messages without a PDF are
left unread for a person. Only the Python standard library is used.
"""
from __future__ import annotations

import email
import imaplib
import logging
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr

from .audit import log
from .config import get_settings
from .db import SessionLocal
from .extraction.pdf_reader import DocumentError
from .image_to_pdf import IMAGE_EXTENSIONS, IMAGE_TYPES
from .ingestion import ingest
from .jobs import process_many

log_ = logging.getLogger(__name__)

MIN_INVOICE_IMAGE_BYTES = 20_000
STATUS: dict ={"enabled": False, "last_poll": None, "last_error": None, "messages_seen": 0, "invoices_created": 0}
_poll_lock = threading.Lock()


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    """(filename, bytes) of every invoice attachment: PDFs (by type, extension or %PDF header) and
    image attachments (photos / scans), which ingestion converts to PDF. Inline images without a
    filename (logos, signatures) are ignored."""
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = _decode(part.get_filename()) or ""
        ctype = part.get_content_type()
        if name and (ctype in IMAGE_TYPES or name.lower().endswith(IMAGE_EXTENSIONS)):
            data = part.get_payload(decode=True) or b""
            if len(data) >= MIN_INVOICE_IMAGE_BYTES:  # smaller ones are logos / signatures, not invoices
                out.append((name, data))
            continue
        if ctype not in ("application/pdf", "application/x-pdf", "application/octet-stream") \
                and not name.lower().endswith(".pdf"):
            continue
        data = part.get_payload(decode=True) or b""
        if ctype == "application/octet-stream" and not name.lower().endswith(".pdf") and not data[:5] == b"%PDF-":
            continue
        out.append((name or "attachment.pdf", data))
    return out


def ingest_message(raw: bytes) -> tuple[list[int], list[str]]:
    """Ingest the PDFs of one raw RFC 822 message. Returns (invoice ids, per-attachment errors)."""
    msg = email.message_from_bytes(raw)
    sender = parseaddr(msg.get("From", ""))[1] or "unknown sender"
    subject = _decode(msg.get("Subject"))[:200]
    ids, errors = [], []
    db = SessionLocal()
    try:
        for name, data in pdf_attachments(msg):
            try:
                inv = ingest(db, data, name, "application/pdf", f"email:{sender}")
            except DocumentError as e:
                errors.append(f"{name}: {e.message}")
                continue
            log(db, inv.id, "RECEIVED_BY_EMAIL", f"Received by e-mail from {sender}: {subject!r}.",
                {"from": sender, "subject": subject, "message_id": msg.get("Message-ID")}, None, f"email:{sender}")
            db.commit()
            ids.append(inv.id)
    finally:
        db.close()
    return ids, errors


def poll_once() -> dict:
    """Fetch UNSEEN messages once. Safe to call from the API ('Check inbox now') and the poller."""
    s = get_settings()
    if not s.imap_host:
        return {"ok": False, "error": "E-mail intake is not configured (set IMAP_HOST, IMAP_USER, IMAP_PASSWORD)."}
    if not _poll_lock.acquire(blocking=False):
        return {"ok": False, "error": "A poll is already running."}
    created, problems, seen = [], [], 0
    try:
        conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port) if s.imap_ssl else imaplib.IMAP4(s.imap_host, s.imap_port)
        try:
            conn.login(s.imap_user or "", s.imap_password or "")
            conn.select(s.imap_folder)
            _, data = conn.search(None, "UNSEEN")
            for num in (data[0] or b"").split()[: s.imap_max_per_poll]:
                _, parts = conn.fetch(num, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                seen += 1
                ids, errors = ingest_message(raw)
                problems += errors
                if ids or errors:  # handled (or rejected with a recorded reason) -> mark read
                    conn.store(num, "+FLAGS", "\\Seen")
                created += ids
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        STATUS.update(last_error=None)
    except Exception as e:  # noqa: BLE001 - a mailbox outage must never take the API down
        log_.exception("e-mail poll failed")
        STATUS.update(last_error=str(e))
        return {"ok": False, "error": f"Mailbox error: {e}"}
    finally:
        STATUS.update(last_poll=datetime.now(timezone.utc).isoformat())
        STATUS["messages_seen"] += seen
        STATUS["invoices_created"] += len(created)
        _poll_lock.release()
    if created:
        threading.Thread(target=process_many, args=(created, "INITIAL", "SYSTEM"), daemon=True).start()
    return {"ok": True, "messages": seen, "invoice_ids": created, "errors": problems}


def start_poller() -> None:
    s = get_settings()
    STATUS["enabled"] = bool(s.imap_host)
    if not s.imap_host:
        return

    def loop():
        while True:
            poll_once()
            time.sleep(max(15, s.imap_poll_seconds))

    threading.Thread(target=loop, daemon=True, name="email-intake").start()
    log_.info("e-mail intake polling %s/%s every %ss", s.imap_host, s.imap_folder, s.imap_poll_seconds)
