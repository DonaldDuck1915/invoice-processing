"""Invoice photos and scans as image files (JPG, PNG, WEBP, TIFF, BMP, HEIC) -> one PDF.

The rest of the pipeline only knows PDFs, so an image upload is wrapped into an image-only PDF
(one page per image frame, e.g. multi-page TIFF) and then follows the scanned-PDF path:
OCR + AI reading + the same verification. The page size keeps the image's aspect ratio.
"""
from __future__ import annotations

import io

from .extraction.pdf_reader import DocumentError

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/bmp", "image/heic", "image/heif"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif")
MAX_PIXELS = 60_000_000  # refuse absurd images (decompression bombs) instead of exhausting memory


def looks_like_image(data: bytes, filename: str | None, content_type: str | None) -> bool:
    head = data[:16]
    magic = (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG") or head[:4] in (b"II*\x00", b"MM\x00*")
             or head.startswith(b"BM") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
             or head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif", b"ftyphevc"))
    return magic or (content_type or "") in IMAGE_TYPES or (filename or "").lower().endswith(IMAGE_EXTENSIONS)


def image_to_pdf(data: bytes, max_pages: int = 10) -> tuple[bytes, int]:
    """Returns (pdf_bytes, page_count). Raises DocumentError for unreadable images."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:  # HEIC/HEIF (iPhone photos) needs an extra decoder; everything else is built into Pillow
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ImportError:
        pass
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        pages = []
        for i in range(getattr(img, "n_frames", 1)):
            if i >= max_pages:
                raise DocumentError("TOO_MANY_PAGES", f"The image has more than {max_pages} pages.")
            img.seek(i)
            frame = ImageOps.exif_transpose(img)  # phone photos: honour the rotation flag
            pages.append(frame.convert("RGB") if frame.mode not in ("RGB", "L") else frame.copy())
    except DocumentError:
        raise
    except Image.DecompressionBombError as e:
        raise DocumentError("IMAGE_TOO_LARGE", "The image resolution is too large.") from e
    except UnidentifiedImageError as e:
        raise DocumentError("UNSUPPORTED_TYPE", "The file is not a readable PDF or image (JPG, PNG, WEBP, TIFF, "
                                                "BMP, HEIC).") from e
    except Exception as e:  # noqa: BLE001 - truncated / corrupt image files
        raise DocumentError("CORRUPT_IMAGE", f"The image could not be read: {e}") from e
    out = io.BytesIO()
    # 200 dpi: an A4 photo of ~1650 px width becomes a normal-size page
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=200.0)
    return out.getvalue(), len(pages)
