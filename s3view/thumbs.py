"""Image thumbnails with an on-disk cache.

Thumbnails are only ever generated for tiles the user can actually see; the
frontend drives that with an IntersectionObserver. On a slow link this is the
difference between browsing a 1000-image prefix and downloading a gigabyte.
"""

import io
import os

from s3view import asdfview, config, fitsview

# Above this we refuse to thumbnail rather than pull a huge object over the wire.
MAX_SOURCE_BYTES = 64 * 1024 * 1024

RASTER_EXT = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ppm", "pgm", "ico",
}
FITS_EXT = {"fits", "fit", "fts", "fz"}
ASDF_EXT = {"asdf"}


def ext_of(key):
    name = key.rsplit("/", 1)[-1].lower()
    if name.endswith(".fits.gz") or name.endswith(".fits.fz"):
        return "fits"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def can_thumb(key):
    e = ext_of(key)
    if e in FITS_EXT or e in ASDF_EXT:
        return True
    if e in RASTER_EXT:
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False
        return True
    return False


def thumb(s3, bucket, key, size=256, etag="", filesize=None, **fits_kw):
    """-> (bytes, content_type). Raises on unsupported/oversized input."""
    e = ext_of(key)
    if e in FITS_EXT:
        png = fitsview.preview_png(
            s3, bucket, key, size=size, etag=etag, filesize=filesize, **fits_kw
        )
        return png, "image/png"

    if e in ASDF_EXT:
        png = asdfview.preview_png(
            s3, bucket, key, size=size, etag=etag, filesize=filesize, **fits_kw
        )
        return png, "image/png"

    if e not in RASTER_EXT:
        raise ValueError("not a thumbnailable type: %s" % e)

    path = fitsview.cache_path(bucket, key, etag, "raster|%d" % size)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read(), "image/png"

    if filesize is not None and filesize > MAX_SOURCE_BYTES:
        raise ValueError("source too large to thumbnail (%d bytes)" % filesize)

    from PIL import Image

    blob, _ = s3.get_object(bucket, key, max_bytes=MAX_SOURCE_BYTES)
    img = Image.open(io.BytesIO(blob))
    try:
        img.draft("RGB", (size, size))  # lets libjpeg decode at reduced scale
    except Exception:
        pass
    img = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") else img.convert("RGB")
    img.thumbnail((size, size), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False, compress_level=3)
    data = out.getvalue()
    try:
        os.makedirs(config.THUMB_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        pass
    return data, "image/png"


def cache_stats():
    n = size = 0
    try:
        for entry in os.scandir(config.THUMB_DIR):
            if entry.is_file():
                n += 1
                size += entry.stat().st_size
    except OSError:
        pass
    return {"count": n, "bytes": size}


def clear_cache():
    removed = 0
    try:
        for entry in os.scandir(config.THUMB_DIR):
            if entry.is_file():
                try:
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed
