"""Preview FITS images that live in S3 without downloading them.

A 200 MB Roman/WFI frame takes ~90 s to pull in full. But FITS is a simple,
self-describing format: fixed 2880-byte header blocks followed by row-major
binary data. So we can read the header with one small ranged GET, compute
exactly where each image row starts, and fetch only the rows the preview
actually needs -- in parallel. A full-frame 256x256 look at a 4088^2 float32
image costs ~4 MB and ~2 s instead of 200 MB and 88 s.
"""

import hashlib
import io
import math
import os
import struct

from s3view import config
from s3view.s3client import POOL

BLOCK = 2880  # FITS logical record size
CARD = 80

# Whole-file GET is faster than many ranged reads below this size.
SMALL_FILE = 16 * 1024 * 1024
HEADER_CHUNK = 64 * 1024
# Merging adjacent ranged reads only pays when the skipped gap is small next to
# the data we actually want. The threshold is therefore derived from the row
# size at call time, never a fixed constant: with a fixed 128 KB gap a 4088-wide
# float32 image at 512 rows has a ~130 KB stride, which merges every range into
# a single read of the entire 67 MB HDU -- the opposite of the intent.
COALESCE_GAP = 128 * 1024  # absolute ceiling on the gap

DTYPES = {8: ">u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8"}


class FitsError(Exception):
    pass


def _hdr_from_bytes(raw):
    """Parse a header out of raw bytes; returns (Header, bytes_consumed) or None."""
    from astropy.io import fits

    text = raw.decode("latin-1")
    for i in range(0, len(text) - CARD + 1, CARD):
        if text[i : i + CARD].startswith("END" + " " * 5) or text[i : i + CARD].rstrip() == "END":
            consumed = int(math.ceil((i + CARD) / BLOCK) * BLOCK)
            return fits.Header.fromstring(text[:consumed]), consumed
    return None


def _data_nbytes(hdr):
    naxis = hdr.get("NAXIS", 0)
    if not naxis:
        return 0
    n = 1
    for i in range(1, naxis + 1):
        n *= hdr["NAXIS%d" % i]
    gcount = hdr.get("GCOUNT", 1) or 1
    pcount = hdr.get("PCOUNT", 0) or 0
    return abs(hdr["BITPIX"]) // 8 * gcount * (pcount + n)


class RemoteFITS:
    """Lazily walks the HDU chain of a remote FITS file using ranged reads."""

    def __init__(self, s3, bucket, key, size=None):
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.size = size if size is not None else s3.head(bucket, key)["size"]
        self._hdus = None

    def _read(self, start, length):
        end = min(start + length, self.size) - 1
        if end < start:
            return b""
        return self.s3.get_range(self.bucket, self.key, start, end)

    def hdus(self, max_hdus=16):
        """[{index, header, data_offset, data_nbytes}] for each HDU."""
        if self._hdus is not None:
            return self._hdus
        out = []
        offset = 0
        while offset < self.size and len(out) < max_hdus:
            raw = b""
            parsed = None
            for _ in range(24):  # up to 1.5 MB of header, far beyond any real file
                chunk = self._read(offset + len(raw), HEADER_CHUNK)
                if not chunk:
                    break
                raw += chunk
                parsed = _hdr_from_bytes(raw)
                if parsed or len(chunk) < HEADER_CHUNK:
                    break
            if not parsed:
                break
            hdr, consumed = parsed
            nbytes = _data_nbytes(hdr)
            out.append(
                {
                    "index": len(out),
                    "header": hdr,
                    "data_offset": offset + consumed,
                    "data_nbytes": nbytes,
                }
            )
            offset = offset + consumed + int(math.ceil(nbytes / BLOCK) * BLOCK)
            if not hdr.get("EXTEND", False) and len(out) == 1 and nbytes == 0:
                continue
        if not out:
            raise FitsError("could not parse any FITS header")
        self._hdus = out
        return out

    def image_hdu(self, index=None):
        """The requested HDU, or the first one holding a >=2-D image."""
        hdus = self.hdus()
        if index is not None:
            if index >= len(hdus):
                raise FitsError("HDU %d not found" % index)
            return hdus[index]
        for h in hdus:
            hdr = h["header"]
            if hdr.get("NAXIS", 0) >= 2 and hdr.get("BITPIX") in DTYPES and h["data_nbytes"]:
                if hdr.get("XTENSION", "").strip() in ("BINTABLE", "TABLE"):
                    continue
                return h
        raise FitsError("no image HDU in this file")

    def header_text(self, index=None):
        hdus = self.hdus()
        parts = []
        for h in hdus:
            hdr = h["header"]
            title = hdr.get("EXTNAME") or ("PRIMARY" if h["index"] == 0 else hdr.get("XTENSION", ""))
            parts.append(
                "=== HDU %d  %s  (offset %d, %d bytes) ===\n%s"
                % (h["index"], title, h["data_offset"], h["data_nbytes"], repr(hdr))
            )
        return "\n\n".join(parts)

    def read_preview_array(self, size=256, index=None, plane=0):
        """Decimated 2-D array via strided ranged reads."""
        import numpy as np

        hdu = self.image_hdu(index)
        hdr = hdu["header"]
        bitpix = hdr["BITPIX"]
        if bitpix not in DTYPES:
            raise FitsError("unsupported BITPIX %s" % bitpix)
        dtype = np.dtype(DTYPES[bitpix])
        itemsize = dtype.itemsize
        width = int(hdr["NAXIS1"])
        height = int(hdr["NAXIS2"])
        if width < 1 or height < 1:
            raise FitsError("degenerate image dimensions")

        base = hdu["data_offset"]
        if int(hdr.get("NAXIS", 2)) > 2:  # cube: offset to the requested plane
            base += int(plane) * width * height * itemsize

        rows = sorted(set(np.linspace(0, height - 1, min(size, height)).astype(int).tolist()))
        row_bytes = width * itemsize

        if self.size <= SMALL_FILE:
            blob = self._read(base, row_bytes * height)
            flat = np.frombuffer(blob[: row_bytes * height], dtype=dtype)
            if flat.size < width * height:
                height = flat.size // width
                flat = flat[: width * height]
                rows = [r for r in rows if r < height] or [0]
            arr = flat.reshape(height, width)[rows, :]
        else:
            wanted = [(base + r * row_bytes, base + (r + 1) * row_bytes - 1) for r in rows]
            merged = _coalesce(wanted, min(COALESCE_GAP, row_bytes))
            blobs = self.s3.get_ranges(self.bucket, self.key, merged)
            buf = {}
            for (mstart, _mend), blob in zip(merged, blobs):
                buf[mstart] = blob
            out = np.empty((len(rows), width), dtype=dtype)
            for i, (rstart, rend) in enumerate(wanted):
                mstart = max(m for m in buf if m <= rstart)
                off = rstart - mstart
                chunk = buf[mstart][off : off + row_bytes]
                if len(chunk) < row_bytes:  # truncated object; pad rather than fail
                    chunk = chunk + b"\0" * (row_bytes - len(chunk))
                out[i] = np.frombuffer(chunk, dtype=dtype)
            arr = out

        arr = arr.astype("f4")
        # Every column of each sampled row is already in memory, so bin them by
        # averaging instead of keeping only every Nth pixel. It costs nothing
        # extra and removes the horizontal aliasing in a dense star field.
        arr = _bin_columns(arr, min(size, width))
        bzero = float(hdr.get("BZERO", 0.0) or 0.0)
        bscale = float(hdr.get("BSCALE", 1.0) or 1.0)
        if bscale != 1.0 or bzero != 0.0:
            arr = arr * bscale + bzero
        return arr, hdr


def _bin_columns(arr, ncols):
    """Average adjacent columns down to `ncols` (anti-aliased decimation)."""
    import numpy as np

    width = arr.shape[1]
    if ncols >= width:
        return arr
    edges = np.linspace(0, width, ncols + 1).astype(int)
    edges[-1] = width
    starts = edges[:-1]
    counts = np.diff(edges).astype("f4")
    counts[counts == 0] = 1.0
    summed = np.add.reduceat(np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0),
                             starts, axis=1)
    return (summed / counts).astype("f4")


def _coalesce(ranges, gap):
    """Merge byte ranges separated by at most `gap` bytes, to cut request count.

    `gap` must be small relative to the payload: every merge downloads the
    skipped bytes too, so a generous threshold silently turns a sparse strided
    read into a full-object download.
    """
    merged = []
    for start, end in ranges:
        if merged and 0 <= start - merged[-1][1] - 1 <= gap:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def render_png(arr, stretch="zscale", cmap="gray", invert=False):
    """Scale a float array to 8-bit and encode as PNG."""
    import numpy as np
    from PIL import Image

    finite = np.isfinite(arr)
    if not finite.any():
        arr = np.zeros_like(arr)
        finite = np.ones_like(arr, dtype=bool)
    vals = arr[finite]

    # Limits are chosen per stretch. A nonlinear stretch needs a wide range to
    # work against; running asinh on top of zscale-clipped limits just amplifies
    # noise, which is not what anyone means by "asinh".
    if stretch == "minmax":
        lo, hi = float(vals.min()), float(vals.max())
    elif stretch == "99.5":
        lo, hi = (float(x) for x in np.percentile(vals, [0.25, 99.75]))
    elif stretch in ("asinh", "log"):
        lo, hi = (float(x) for x in np.percentile(vals, [1.0, 99.9]))
    else:
        try:
            from astropy.visualization import ZScaleInterval

            lo, hi = ZScaleInterval().get_limits(vals)
        except Exception:
            lo, hi = (float(x) for x in np.percentile(vals, [1, 99]))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(vals.min()), float(vals.max()) or 1.0
        if hi <= lo:
            hi = lo + 1.0

    norm = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    norm[~finite] = 0.0
    if stretch == "asinh":
        norm = np.arcsinh(norm * 10.0) / math.asinh(10.0)
    elif stretch == "log":
        norm = np.log1p(norm * 999.0) / math.log(1000.0)
    if invert:
        norm = 1.0 - norm

    norm = np.flipud(norm)  # FITS origin is bottom-left
    eight = (norm * 255.0 + 0.5).astype("u1")

    if cmap and cmap != "gray":
        rgb = _apply_cmap(eight, cmap)
        img = Image.fromarray(rgb, mode="RGB")
    else:
        img = Image.fromarray(eight, mode="L")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False, compress_level=1)
    return out.getvalue(), (lo, hi)


def _apply_cmap(eight, name):
    import numpy as np

    lut = None
    try:  # matplotlib >= 3.9 removed matplotlib.cm.get_cmap
        from matplotlib import colormaps

        lut = (colormaps[name](np.arange(256))[:, :3] * 255).astype("u1")
    except Exception:
        try:
            import matplotlib.cm as cm

            lut = (cm.get_cmap(name)(np.arange(256))[:, :3] * 255).astype("u1")
        except Exception:
            lut = None
    if lut is None:
        lut = np.stack([np.arange(256)] * 3, axis=1).astype("u1")
    return lut[eight]


def cache_path(bucket, key, etag, params):
    digest = hashlib.sha1(
        ("%s/%s/%s/%s" % (bucket, key, etag, params)).encode("utf-8")
    ).hexdigest()
    return os.path.join(config.THUMB_DIR, digest + ".png")


def preview_png(s3, bucket, key, size=256, index=None, plane=0, stretch="zscale",
                cmap="gray", etag="", filesize=None, use_cache=True):
    """Cached FITS preview PNG."""
    params = "%s|%s|%s|%s|%s" % (size, index, plane, stretch, cmap)
    path = cache_path(bucket, key, etag, params)
    if use_cache and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    rf = RemoteFITS(s3, bucket, key, filesize)
    arr, _hdr = rf.read_preview_array(size=size, index=index, plane=plane)
    png, _limits = render_png(arr, stretch=stretch, cmap=cmap)
    try:
        os.makedirs(config.THUMB_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, path)
    except OSError:
        pass
    return png
