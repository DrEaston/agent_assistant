"""Normalize uploaded recipe photos before storage and OCR.

Phone photos arrive large, rotated via EXIF, and sometimes as HEIC. We store a
right-side-up JPEG no larger than MAX_EDGE on its longest side.
"""

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_EDGE = 2400
JPEG_QUALITY = 88
HEIC_EXTENSIONS = {".heic", ".heif"}

try:  # Optional: HEIC decoding when pillow-heif is installed.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # pragma: no cover - depends on environment
    HEIC_SUPPORTED = False


class UnsupportedRecipeImage(ValueError):
    """Raised when an upload can't be turned into a usable image."""


def is_heic(filename, content_type=""):
    return Path(filename or "").suffix.lower() in HEIC_EXTENSIONS or (content_type or "").lower() in {
        "image/heic", "image/heif",
    }


def normalize_recipe_image(data, filename="", content_type=""):
    """Return (jpeg_bytes, ".jpg") for an uploaded image, or raise UnsupportedRecipeImage."""
    if is_heic(filename, content_type) and not HEIC_SUPPORTED:
        raise UnsupportedRecipeImage(
            f"{filename or 'This photo'} is HEIC, which this server can't read yet. "
            "Upload it as JPEG instead (on iPhone: Settings > Camera > Formats > Most Compatible, "
            "or share/export the photo as JPEG)."
        )
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedRecipeImage(f"{filename or 'This file'} could not be read as an image.") from exc

    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            image = background
        else:
            image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), ".jpg"


def downscaled_data_for_model(path, max_edge=2000):
    """Bytes + mime type suitable for sending to a vision model; falls back to the raw file."""
    path = Path(path)
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if max(image.size) <= max_edge and path.suffix.lower() in {".jpg", ".jpeg"}:
                return path.read_bytes(), "image/jpeg"
            image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return None, ""
