"""QR codes on invoice pages, and the Indian GST e-invoice QR payload.

An e-invoice's signed QR code is a JWT issued by the Invoice Registration Portal (IRP). Its payload's
`data` claim holds the key invoice facts (seller/buyer GSTIN, document number, date, total, IRN).
We decode the payload to cross-check it with what is printed. The signature itself is not verified:
that needs the IRP's public key, which is not bundled (see README).
"""
from __future__ import annotations

import base64
import json

import pymupdf as fitz


def _decode(detector, img) -> list[str]:
    import cv2

    try:
        ok, texts, _, _ = detector.detectAndDecodeMulti(img)
        if ok and any(texts):
            return [t for t in texts if t]
    except cv2.error:
        pass
    try:
        text, _, _ = detector.detectAndDecode(img)
    except cv2.error:
        return []
    return [text] if text else []


def decode_qr_codes(pdf_bytes: bytes, max_pages: int = 10, dpis: tuple[int, ...] = (200, 300)) -> list[str]:
    """Signed e-invoice QR codes are dense (500+ characters), so a page that shows no code at 200 dpi
    is retried at a higher resolution."""
    import cv2
    import numpy as np

    out: list[str] = []
    # the ArUco-based detector finds dense codes far more reliably; the classic one is the fallback
    detectors = [d() for d in (getattr(cv2, "QRCodeDetectorAruco", None), cv2.QRCodeDetector) if d]
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in list(doc)[:max_pages]:
            for dpi in dpis:
                pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
                found = next((f for f in (_decode(d, img) for d in detectors) if f), [])
                if found:
                    out += [t for t in found if t not in out]
                    break
    return out


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def parse_einvoice_qr(text: str) -> dict | None:
    """Returns the e-invoice facts from a signed-QR JWT, or None if the text is not one."""
    parts = (text or "").strip().split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(_b64(parts[1]))
        data = claims.get("data", claims)
        if isinstance(data, str):
            data = json.loads(data)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not ({"Irn", "SellerGstin", "DocNo"} & set(data)):
        return None
    return {"seller_gstin": data.get("SellerGstin"), "buyer_gstin": data.get("BuyerGstin"),
            "doc_no": data.get("DocNo"), "doc_type": data.get("DocTyp"), "doc_date": data.get("DocDt"),
            "total": data.get("TotInvVal"), "irn": data.get("Irn"), "irn_date": data.get("IrnDt"),
            "item_count": data.get("ItemCnt"), "issuer": claims.get("iss")}
