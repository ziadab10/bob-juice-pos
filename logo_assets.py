"""Master BOB JUICE logo — circular teal variant for PDF, thermal, and web."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from config import BRAND_LOGO_PATH, BRAND_WORDMARK_PATH, CIRCULAR_LOGO_PATH, STATIC_DIR

logger = logging.getLogger("bob_juice.logo")

TEAL_RGB = (13, 79, 79)  # deep teal — matches brand circular lockup
SVG_LOGO_PATH = STATIC_DIR / "logo.svg"
_CIRCLE_SIZE = 512
_PDF_BYTES: bytes | None = None
_RGB_CACHE: object | None = None


def _draw_fallback_circular_logo(size: int):
    """Programmatic teal circular badge when raster brand assets are unavailable."""
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.new("RGB", (size, size), TEAL_RGB)
    draw = ImageDraw.Draw(canvas)
    margin = int(size * 0.06)
    draw.ellipse((margin, margin, size - margin, size - margin), fill=TEAL_RGB, outline=(20, 110, 110), width=max(2, size // 128))
    gold = (250, 204, 21)
    white = (255, 255, 255)
    try:
        font_bob = ImageFont.truetype("arialbd.ttf", size=int(size * 0.16))
        font_juice = ImageFont.truetype("arialbd.ttf", size=int(size * 0.12))
    except OSError:
        try:
            font_bob = ImageFont.truetype("DejaVuSans-Bold.ttf", size=int(size * 0.16))
            font_juice = ImageFont.truetype("DejaVuSans-Bold.ttf", size=int(size * 0.12))
        except OSError:
            font_bob = ImageFont.load_default()
            font_juice = font_bob
    draw.text((size // 2, int(size * 0.40)), "BOB", fill=gold, anchor="mm", font=font_bob)
    draw.text((size // 2, int(size * 0.58)), "JUICE", fill=white, anchor="mm", font=font_juice)
    return canvas


def _composite_wordmark_on_teal(source: Path, size: int):
    """Build opaque circular teal PNG from a brand raster wordmark."""
    from PIL import Image

    canvas = Image.new("RGB", (size, size), TEAL_RGB)
    with Image.open(source) as raw:
        wordmark = _strip_dark_background(raw)
    max_w = int(size * 0.82)
    max_h = int(size * 0.82)
    wordmark.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    x = (size - wordmark.width) // 2
    y = (size - wordmark.height) // 2
    canvas.paste(wordmark, (x, y), wordmark)
    return canvas


def _strip_dark_background(img) -> object:
    """Remove black matte from wordmark before compositing onto teal."""
    from PIL import Image

    rgba = img.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if r < 48 and g < 48 and b < 48:
                px[x, y] = (0, 0, 0, 0)
    return rgba


def materialize_circular_logo(*, force_refresh: bool = False) -> Path | None:
    """Build opaque circular teal PNG — universal asset for web, PDF, and thermal."""
    global _PDF_BYTES, _RGB_CACHE
    if CIRCULAR_LOGO_PATH.is_file() and not force_refresh:
        return CIRCULAR_LOGO_PATH

    size = _CIRCLE_SIZE
    source = BRAND_WORDMARK_PATH if BRAND_WORDMARK_PATH.is_file() else BRAND_LOGO_PATH
    if source.is_file():
        canvas = _composite_wordmark_on_teal(source, size)
    else:
        if SVG_LOGO_PATH.is_file():
            logger.info("Raster brand assets missing — using programmatic teal circular logo")
        else:
            logger.warning("No brand assets found — using programmatic teal circular logo")
        canvas = _draw_fallback_circular_logo(size)

    CIRCULAR_LOGO_PATH.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(CIRCULAR_LOGO_PATH, format="PNG", optimize=True)
    _RGB_CACHE = canvas.copy()
    _PDF_BYTES = None
    logger.info("Circular teal logo ready: %s", CIRCULAR_LOGO_PATH)
    return CIRCULAR_LOGO_PATH


def load_logo_rgb(*, force_refresh: bool = False):
    """Load opaque circular logo RGB — always visible on light and dark surfaces."""
    global _RGB_CACHE
    if _RGB_CACHE is not None and not force_refresh:
        return _RGB_CACHE.copy()

    from PIL import Image

    materialize_circular_logo(force_refresh=force_refresh)
    if not CIRCULAR_LOGO_PATH.is_file():
        return None
    with Image.open(CIRCULAR_LOGO_PATH) as img:
        _RGB_CACHE = img.convert("RGB").copy()
        return _RGB_CACHE.copy()


def materialize_transparent_logo() -> Path | None:
    """Ensure circular logo exists (legacy entry point for startup)."""
    return materialize_circular_logo()


def logo_pdf_png_bytes(*, size_px: int = 400) -> bytes | None:
    """Square RGB PNG for fpdf2 — equal width/height prevents stretch on receipts."""
    global _PDF_BYTES
    if _PDF_BYTES is not None:
        return _PDF_BYTES
    rgb = load_logo_rgb()
    if rgb is None:
        return None
    from PIL import Image

    img = rgb.copy()
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((size_px, size_px), Image.Resampling.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    _PDF_BYTES = out.getvalue()
    return _PDF_BYTES


def logo_thermal_escpos_bytes(*, target_width: int = 384) -> bytes | None:
    """Raster circular logo for ESC/POS — grayscale from opaque RGB."""
    rgb = load_logo_rgb()
    if rgb is None:
        return None
    try:
        from PIL import Image

        gray = rgb.convert("L")
        ratio = target_width / max(gray.width, 1)
        height = max(1, int(gray.height * ratio))
        gray = gray.resize((target_width, height), Image.Resampling.LANCZOS)
        bw = gray.point(lambda p: 0 if p < 185 else 255, mode="1")

        width_bytes = (target_width + 7) // 8
        raster = bytearray()
        for y in range(height):
            row = bytearray(width_bytes)
            for x in range(target_width):
                if bw.getpixel((x, y)) == 0:
                    row[x // 8] |= 0x80 >> (x % 8)
            raster.extend(row)

        GS, ESC = b"\x1d", b"\x1b"
        out = bytearray()
        out += ESC + b"a" + b"\x01"
        out += GS + b"v0" + bytes([0, width_bytes, height & 0xFF, (height >> 8) & 0xFF]) + raster
        out += ESC + b"a" + b"\x00"
        return bytes(out)
    except Exception as exc:
        logger.debug("Thermal logo skipped: %s", exc)
        return None


def logo_receipt_png_bytes(*, max_width: int = 280) -> bytes | None:
    """Opaque PNG for HTML/thermal browser receipts."""
    rgb = load_logo_rgb()
    if rgb is None:
        return None
    from PIL import Image

    img = rgb.copy()
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
