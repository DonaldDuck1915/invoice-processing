"""Turn PDF bytes into positioned words, using the text layer or OCR per page."""
from __future__ import annotations

import io
import statistics
from dataclasses import dataclass, field

import pymupdf as fitz


class DocumentError(Exception):
    """Raised for invalid, corrupted or unsupported documents."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0   # 0..1
    page: int = 1

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Line:
    words: list[Word]
    page: int

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def conf(self) -> float:
        return statistics.fmean(w.conf for w in self.words) if self.words else 0.0

    @property
    def y(self) -> float:
        return statistics.fmean(w.yc for w in self.words)

    def clusters(self, gap_factor: float = 2.5) -> list[Line]:
        """Split a visual line into column segments separated by wide gaps
        (e.g. a left address block and a right 'Invoice No' block on the same row)."""
        if len(self.words) < 2:
            return [self]
        char_w = statistics.median([(w.x1 - w.x0) / max(len(w.text), 1) for w in self.words])
        groups = [[self.words[0]]]
        for a, b in zip(self.words, self.words[1:]):
            if b.x0 - a.x1 > char_w * gap_factor:
                groups.append([b])
            else:
                groups[-1].append(b)
        return [Line(g, self.page) for g in groups]


@dataclass
class PageText:
    number: int
    words: list[Word]
    is_ocr: bool
    width: float
    height: float
    mean_conf: float = 1.0
    lines: list[Line] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class DocumentText:
    pages: list[PageText]
    page_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def is_scanned(self) -> bool:
        return any(p.is_ocr for p in self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def lines(self) -> list[Line]:
        return [ln for p in self.pages for ln in p.lines]

    @property
    def ocr_mean_conf(self) -> float | None:
        ocr = [p.mean_conf for p in self.pages if p.is_ocr]
        return statistics.fmean(ocr) if ocr else None


def group_lines(words: list[Word], page: int) -> list[Line]:
    if not words:
        return []
    heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    tol = (statistics.median(heights) if heights else 10) * 0.55
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(statistics.fmean(x.yc for x in rows[-1]) - w.yc) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [Line(sorted(r, key=lambda w: w.x0), page) for r in rows]


def open_pdf(data: bytes, max_pages: int = 10) -> fitz.Document:
    if not data or len(data) < 64:
        raise DocumentError("EMPTY_FILE", "The uploaded file is empty.")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise DocumentError("NOT_A_PDF", "The file is not a PDF (missing %PDF header).")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise DocumentError("CORRUPT_PDF", f"The PDF could not be opened: {e}") from e
    if doc.needs_pass:
        raise DocumentError("ENCRYPTED_PDF", "The PDF is password-protected.")
    if doc.page_count == 0:
        raise DocumentError("NO_PAGES", "The PDF has no pages.")
    if doc.page_count > max_pages:
        raise DocumentError("TOO_MANY_PAGES", f"The PDF has {doc.page_count} pages; the limit is {max_pages}.")
    return doc


def _text_layer_words(page: fitz.Page, number: int) -> list[Word]:
    out = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        t = text.strip()
        if t:
            out.append(Word(t, x0, y0, x1, y1, 0.99, number))
    return out


def _meaningful_chars(words: list[Word]) -> int:
    return sum(sum(ch.isalnum() for ch in w.text) for w in words)


def _ocr_page(page: fitz.Page, number: int, tesseract_cmd: str | None = None) -> tuple[list[Word], float]:
    import numpy as np
    import pytesseract
    from PIL import Image

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    zoom = 300 / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        import cv2

        arr = np.array(img)
        arr = _deskew(arr, cv2)
        # median blur removes salt-and-pepper scanner noise; Otsu binarises without hand-tuned thresholds
        arr = cv2.threshold(cv2.medianBlur(arr, 3), 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
        img = Image.fromarray(arr)
    except Exception:  # noqa: BLE001 - preprocessing is best-effort
        pass
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 6")
    words, confs = [], []
    for i, text in enumerate(data["text"]):
        t = (text or "").strip()
        conf = float(data["conf"][i])
        if not t or conf < 0 or (conf < 20 and not any(ch.isalnum() for ch in t)):
            continue
        x, y, w, h = (data[k][i] / zoom for k in ("left", "top", "width", "height"))
        words.append(Word(t, x, y, x + w, y + h, conf / 100, number))
        confs.append(conf / 100)
    return words, (statistics.fmean(confs) if confs else 0.0)


def _deskew(arr, cv2):
    """Projection-profile deskew: pick the small rotation that makes text rows sharpest."""
    import numpy as np

    h, w = arr.shape
    scale = 1000 / max(h, w)
    small = cv2.resize(arr, (int(w * scale), int(h * scale)))
    inv = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    sh, sw = inv.shape
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-3.0, 3.01, 0.2):
        m = cv2.getRotationMatrix2D((sw / 2, sh / 2), float(angle), 1.0)
        rot = cv2.warpAffine(inv, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    if abs(best_angle) < 0.1:
        return arr
    m = cv2.getRotationMatrix2D((w / 2, h / 2), best_angle, 1.0)
    return cv2.warpAffine(arr, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def ocr_available(tesseract_cmd: str | None = None) -> bool:
    try:
        import pytesseract

        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def read_document(data: bytes, max_pages: int = 10, tesseract_cmd: str | None = None) -> DocumentText:
    doc = open_pdf(data, max_pages)
    pages, warnings = [], []
    for i, page in enumerate(doc, start=1):
        words = _text_layer_words(page, i)
        if _meaningful_chars(words) >= 30:
            pt = PageText(i, words, False, page.rect.width, page.rect.height, 0.99)
        else:
            if not ocr_available(tesseract_cmd):
                warnings.append(f"Page {i} is a scanned image but OCR (Tesseract) is not available.")
                pt = PageText(i, words, True, page.rect.width, page.rect.height, 0.0)
            else:
                ocr_words, conf = _ocr_page(page, i, tesseract_cmd)
                pt = PageText(i, ocr_words, True, page.rect.width, page.rect.height, conf)
        pt.lines = group_lines(pt.words, i)
        pages.append(pt)
    count = doc.page_count
    doc.close()
    return DocumentText(pages, count, warnings)
