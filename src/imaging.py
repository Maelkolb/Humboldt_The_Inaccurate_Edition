"""PIL helpers: page loading/encoding and per-region cropping.

``bbox`` values are ``[y_min, x_min, y_max, x_max]`` on a 0–1000 grid relative to
the full page (the region detector's output format).
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageOps

logger = logging.getLogger(__name__)

JPEG_MIME = "image/jpeg"

# Defaults for the per-region crop. A margin keeps the region's own edge
# strokes (ascenders/descenders, the last word on a line) inside the frame;
# the detector's boxes are deliberately tight. Margin is expressed as a
# fraction of the *page* dimension plus a pixel floor, so a page-number box
# and a full-text column both get sensible breathing room.
DEFAULT_CROP_MARGIN_FRAC = 0.028
DEFAULT_CROP_MARGIN_MIN_PX = 22
# Cap the longest side of a crop to keep per-region token cost bounded on
# very high-resolution scans. ``None`` disables the cap.
DEFAULT_CROP_MAX_PX = 1600


# ---------------------------------------------------------------------------
# Loading / encoding
# ---------------------------------------------------------------------------

def load_image_rgb(path: str | Path) -> Image.Image:
    """Open ``path``, honour EXIF orientation, return an RGB copy."""
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img.copy()


def encode_jpeg(img: Image.Image, quality: int = 95) -> bytes:
    """Encode a PIL image to JPEG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def page_image_bytes(
    path: str | Path,
    *,
    max_px: Optional[int] = None,
    quality: int = 95,
) -> Tuple[bytes, str]:
    """Return ``(jpeg_bytes, mime)`` for the full page.

    Used by stages that reason over the whole page (region detection,
    consistency check). ``max_px`` optionally downscales the longest side.
    """
    img = load_image_rgb(path)
    if max_px:
        img = fit_longest_side(img, max_px)
    return encode_jpeg(img, quality), JPEG_MIME


def measure_aspect(path: str | Path) -> Optional[float]:
    """Return width/height for ``path`` (used for Document-view geometry)."""
    try:
        with Image.open(path) as img:
            w, h = img.size
        return (w / h) if h else None
    except Exception as exc:  # unreadable image — caller falls back to default
        logger.debug("Could not measure aspect for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Region cropping (transcription stage)
# ---------------------------------------------------------------------------

def bbox_to_pixels(
    bbox: Sequence[float], width: int, height: int
) -> Optional[Tuple[int, int, int, int]]:
    """Convert a ``[y_min, x_min, y_max, x_max]`` 0–1000 bbox to a pixel
    ``(left, top, right, bottom)`` tuple, or ``None`` if malformed."""
    if not bbox or len(bbox) != 4:
        return None
    y1, x1, y2, x2 = bbox
    left = int(round(min(x1, x2) / 1000.0 * width))
    right = int(round(max(x1, x2) / 1000.0 * width))
    top = int(round(min(y1, y2) / 1000.0 * height))
    bottom = int(round(max(y1, y2) / 1000.0 * height))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def crop_region(
    image: Image.Image,
    bbox: Sequence[float],
    *,
    margin_frac: float = DEFAULT_CROP_MARGIN_FRAC,
    margin_min_px: int = DEFAULT_CROP_MARGIN_MIN_PX,
    max_px: Optional[int] = DEFAULT_CROP_MAX_PX,
    mask_bboxes: Optional[Sequence[Sequence[float]]] = None,
) -> Optional[Image.Image]:
    """Cut the region described by ``bbox`` out of an already-loaded page
    image, padded with a margin and clamped to the page. Returns ``None``
    when the bbox is unusable.

    When ``mask_bboxes`` (other regions' bboxes on the same page) is given, the
    pixels belonging to those neighbours are painted white inside the crop so the
    region's own ink is isolated — this removes the neighbour bleed (e.g. a
    heading above a text column) that otherwise contaminates the reading. The
    region's own ``bbox`` area is always restored on top, so it is never erased
    even where boxes overlap.
    """
    w, h = image.size
    px = bbox_to_pixels(bbox, w, h)
    if px is None:
        return None
    own = px  # tight region rect in full-image pixels (pre-margin)
    left, top, right, bottom = px

    pad_x = max(margin_min_px, int(round(margin_frac * w)))
    pad_y = max(margin_min_px, int(round(margin_frac * h)))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(w, right + pad_x)
    bottom = min(h, bottom + pad_y)

    crop = image.crop((left, top, right, bottom))

    if mask_bboxes:
        draw = ImageDraw.Draw(crop)
        for ob in mask_bboxes:
            opx = bbox_to_pixels(ob, w, h)
            if opx is None:
                continue
            ix1, iy1 = max(opx[0], left), max(opx[1], top)
            ix2, iy2 = min(opx[2], right), min(opx[3], bottom)
            if ix2 > ix1 and iy2 > iy1:
                draw.rectangle([ix1 - left, iy1 - top, ix2 - left, iy2 - top],
                               fill=(255, 255, 255))
        # Restore this region's own pixels on top (overlaps with neighbours win).
        ox1, oy1 = max(own[0], left), max(own[1], top)
        ox2, oy2 = min(own[2], right), min(own[3], bottom)
        if ox2 > ox1 and oy2 > oy1:
            patch = image.crop((ox1, oy1, ox2, oy2))
            crop.paste(patch, (ox1 - left, oy1 - top))

    if max_px:
        crop = fit_longest_side(crop, max_px)
    return crop


def crop_region_bytes(
    image: Image.Image,
    bbox: Sequence[float],
    *,
    margin_frac: float = DEFAULT_CROP_MARGIN_FRAC,
    margin_min_px: int = DEFAULT_CROP_MARGIN_MIN_PX,
    max_px: Optional[int] = DEFAULT_CROP_MAX_PX,
    quality: int = 98,
    mask_bboxes: Optional[Sequence[Sequence[float]]] = None,
) -> Optional[Tuple[bytes, str]]:
    """Convenience wrapper: ``crop_region`` → ``(jpeg_bytes, mime)``.

    Crops are the highest-detail, accuracy-critical input and the source scans are
    already low-resolution, so they are encoded at high JPEG quality (98) to avoid
    compounding compression artifacts on marginal ink."""
    crop = crop_region(
        image, bbox,
        margin_frac=margin_frac,
        margin_min_px=margin_min_px,
        max_px=max_px,
        mask_bboxes=mask_bboxes,
    )
    if crop is None:
        return None
    return encode_jpeg(crop, quality), JPEG_MIME


# ---------------------------------------------------------------------------
# HTML-embed downscaling
# ---------------------------------------------------------------------------

def embed_jpeg_base64(
    path: str | Path,
    *,
    max_width: int = 1100,
    quality: int = 76,
) -> Tuple[str, float]:
    """Return ``(base64_jpeg, aspect)`` for inline facsimile embedding."""
    import base64

    img = load_image_rgb(path)
    w, h = img.size
    aspect = (w / h) if h else 0.72
    if w > max_width:
        new_h = int(h * (max_width / w))
        img = img.resize((max_width, new_h), Image.LANCZOS)
    data = encode_jpeg(img, quality)
    return base64.b64encode(data).decode("ascii"), aspect


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def fit_longest_side(img: Image.Image, max_px: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= max_px:
        return img
    scale = max_px / longest
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
