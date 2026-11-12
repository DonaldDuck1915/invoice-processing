"""Image invoices (photos / scans as JPG, PNG, TIFF ...) are converted to PDF and processed like scans."""
import io

import pymupdf as fitz
import pytest
from PIL import Image
from sqlalchemy import select

from app import models as m
from app.email_intake import pdf_attachments
from app.extraction.pdf_reader import DocumentError
from app.image_to_pdf import image_to_pdf
from app.ingestion import ingest

from .conftest import SAMPLES
from .test_gaps import _mail


def page_image(fmt: str, frames: int = 1) -> bytes:
    """Render the digital happy-path invoice to an image, like a phone photo or a scanner would."""
    doc = fitz.open(SAMPLES / "01_happy_path_digital.pdf")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    out = io.BytesIO()
    if frames > 1:
        img.save(out, fmt, save_all=True, append_images=[img] * (frames - 1))
    else:
        img.save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,name", [("JPEG", "photo.jpg"), ("PNG", "scan.png"), ("WEBP", "x.webp"),
                                      ("BMP", "x.bmp"), ("TIFF", "x.tiff")])
def test_image_formats_become_a_pdf(fmt, name):
    pdf, pages = image_to_pdf(page_image(fmt))
    assert pdf[:5] == b"%PDF-" and pages == 1
    assert fitz.open(stream=pdf, filetype="pdf").page_count == 1


def test_multi_page_tiff_keeps_every_page():
    pdf, pages = image_to_pdf(page_image("TIFF", frames=3))
    assert pages == 3 and fitz.open(stream=pdf, filetype="pdf").page_count == 3


def test_image_upload_is_ingested_as_a_scanned_pdf(db):
    inv = ingest(db, page_image("JPEG"), "IMG_2041.jpg", "image/jpeg", "test")
    doc = db.get(m.Document, inv.document_id)
    assert doc.content[:5] == b"%PDF-" and inv.original_filename == "IMG_2041.jpg"
    events = db.scalars(select(m.AuditEvent.event_type).where(m.AuditEvent.invoice_id == inv.id)).all()
    assert "IMAGE_CONVERTED" in events


def test_same_photo_twice_has_the_same_fingerprint(db):
    data = page_image("PNG")
    a = ingest(db, data, "a.png", "image/png", "test")
    b = ingest(db, data, "b.png", "image/png", "test")
    assert a.file_sha256 == b.file_sha256  # identical upload -> confirmed duplicate later


@pytest.mark.parametrize("data,code", [(b"\x89PNG\r\n\x1a\n" + b"broken" * 20, "CORRUPT_IMAGE"),
                                       (b"just some text, not an invoice" * 5, "UNSUPPORTED_TYPE")])
def test_bad_images_are_refused_clearly(db, data, code):
    with pytest.raises(DocumentError) as e:
        ingest(db, data, "x.png", "image/png", "test")
    assert e.value.code == code


def test_email_image_attachments_are_picked_up_but_not_inline_logos():
    msg = _mail(("invoice-photo.jpg", page_image("JPEG"), "image/jpeg"))
    logo = msg.add_attachment(b"\x89PNG....", maintype="image", subtype="png")  # no filename = inline logo
    assert logo is None
    names = [n for n, _ in pdf_attachments(msg)]
    assert names == ["invoice-photo.jpg"]
