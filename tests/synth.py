"""Synthetic FITS/ASDF files and an in-memory S3, so tests need no network.

The point of these fixtures is that they exercise the real byte-offset
arithmetic: `RemoteFITS` and `RemoteASDF` run unmodified against `FakeS3` and
issue exactly the ranged reads they would issue against AWS.
"""

import io
import struct
import time

import numpy as np


def star_field(height, width, seed=7):
    """A small image with a smooth background and a few bright point sources."""
    rng = np.random.default_rng(seed)
    img = rng.normal(100.0, 3.0, size=(height, width)).astype("f4")
    ys = rng.integers(2, height - 2, size=25)
    xs = rng.integers(2, width - 2, size=25)
    img[ys, xs] += rng.uniform(200, 4000, size=25)
    return img


def fits_bytes(height=64, width=48, n_extra=2, seed=7):
    """A multi-HDU FITS file as raw bytes."""
    from astropy.io import fits

    primary = fits.PrimaryHDU(data=star_field(height, width, seed))
    primary.header["OBJECT"] = "SYNTH"
    hdus = [primary]
    for i in range(n_extra):
        hdu = fits.ImageHDU(data=star_field(height, width, seed + i + 1))
        hdu.header["EXTNAME"] = "EXT%d" % (i + 1)
        hdus.append(hdu)
    buf = io.BytesIO()
    fits.HDUList(hdus).writeto(buf)
    return buf.getvalue()


def _asdf_block(payload):
    """One binary block: magic + header_size + 48-byte header + data."""
    header = struct.pack(
        ">I4sQQQ", 0, b"\x00\x00\x00\x00", len(payload), len(payload), len(payload)
    ) + b"\x00" * 16
    assert len(header) == 48
    return b"\xd3BLK" + struct.pack(">H", len(header)) + header + payload


def asdf_bytes(height=64, width=48, with_index=True, seed=11):
    """An ASDF file with two named arrays, optionally with a block index.

    `with_index=False` exercises the fallback that walks block headers when the
    trailing index is missing.
    """
    data = star_field(height, width, seed).astype("<f4")
    err = (star_field(height, width, seed + 1) / 10.0).astype("<f4")

    # NB: f-string, not %-formatting -- the header legitimately contains "%YAML".
    tree = (
        "#ASDF 1.0.0\n"
        "#ASDF_STANDARD 1.6.0\n"
        "%YAML 1.1\n"
        "%TAG ! tag:stsci.edu:asdf/\n"
        "--- !core/asdf-1.1.0\n"
        "asdf_library: !core/software-1.0.0 {name: synth, version: 0.0.1}\n"
        "roman:\n"
        "  data: !core/ndarray-1.0.0\n"
        "    source: 0\n"
        "    datatype: float32\n"
        "    byteorder: little\n"
        f"    shape: [{height}, {width}]\n"
        "  err: !core/ndarray-1.0.0\n"
        "    source: 1\n"
        "    datatype: float32\n"
        "    byteorder: little\n"
        f"    shape: [{height}, {width}]\n"
        "  meta:\n"
        "    instrument: SYNTH\n"
    )
    head = tree.encode("utf-8") + b"\n...\n"

    offsets = []
    body = b""
    for payload in (data.tobytes(), err.tobytes()):
        offsets.append(len(head) + len(body))
        body += _asdf_block(payload)

    out = head + body
    if with_index:
        idx = "#ASDF BLOCK INDEX\n%YAML 1.1\n---\n"
        idx += "".join("- %d\n" % o for o in offsets)
        idx += "...\n"
        out += idx.encode("utf-8")
    return out, data, err


class FakeS3:
    """In-memory stand-in for `s3view.s3client.S3`.

    Records every ranged read so tests can assert on *how much* was fetched --
    the property the whole program exists to protect.
    """

    def __init__(self, objects=None):
        # {bucket: {key: bytes}}
        self.objects = objects or {}
        self.reads = []          # (key, start, end)
        self.page_size = 1000
        self.list_cache = _NoCache()

    # -- helpers ---------------------------------------------------------
    def put(self, bucket, key, blob):
        self.objects.setdefault(bucket, {})[key] = blob

    @property
    def bytes_read(self):
        return sum(end - start + 1 for _k, start, end in self.reads)

    def _blob(self, bucket, key):
        try:
            return self.objects[bucket][key]
        except KeyError:
            raise FileNotFoundError("%s/%s" % (bucket, key))

    # -- the S3 surface actually used by the app --------------------------
    def head(self, bucket, key):
        blob = self._blob(bucket, key)
        return {
            "key": key, "size": len(blob), "mtime": 1700000000.0,
            "etag": "etag-%d" % len(blob), "content_type": "application/octet-stream",
            "storage": "STANDARD", "metadata": {},
        }

    def get_range(self, bucket, key, start, end):
        blob = self._blob(bucket, key)
        end = min(end, len(blob) - 1)
        self.reads.append((key, start, end))
        return blob[start : end + 1]

    def get_ranges(self, bucket, key, ranges, workers=8):
        return [self.get_range(bucket, key, a, b) for a, b in ranges]

    def get_object(self, bucket, key, max_bytes=None):
        blob = self._blob(bucket, key)
        if max_bytes:
            blob = blob[:max_bytes]
        self.reads.append((key, 0, len(blob) - 1))
        return blob, ""

    def get_stream(self, bucket, key, byte_range=None):
        blob = self._blob(bucket, key)
        start, end = 0, len(blob) - 1
        crange = None
        if byte_range and byte_range.startswith("bytes="):
            lo, _, hi = byte_range[6:].partition("-")
            start = int(lo or 0)
            end = int(hi) if hi else len(blob) - 1
            end = min(end, len(blob) - 1)
            crange = "bytes %d-%d/%d" % (start, end, len(blob))
        chunk = blob[start : end + 1]
        return {
            "Body": io.BytesIO(chunk), "ContentLength": len(chunk),
            "ContentType": "application/octet-stream", "ContentRange": crange,
        }

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        return "https://%s.s3.amazonaws.com/%s?X-Amz-Signature=fake" % (bucket, key)

    def list_buckets(self):
        return [{"name": b, "created": 1700000000.0} for b in sorted(self.objects)]

    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        keys = sorted(self.objects.get(bucket, {}))
        folders, files = [], []
        seen = set()
        for k in keys:
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if delimiter and delimiter in rest:
                name = rest.split(delimiter)[0]
                if name not in seen:
                    seen.add(name)
                    folders.append({"name": name, "prefix": prefix + name + "/"})
            else:
                files.append({
                    "name": rest, "key": k, "size": len(self.objects[bucket][k]),
                    "mtime": 1700000000.0, "etag": "etag-%d" % len(self.objects[bucket][k]),
                    "storage": "STANDARD",
                })
        return {
            "bucket": bucket, "prefix": prefix, "folders": folders, "files": files,
            "truncated": False, "next_token": None, "ms": 1,
        }

    def search(self, bucket, prefix, query, max_keys=50000, limit=500, deadline=15.0):
        out = []
        for k in sorted(self.objects.get(bucket, {})):
            if k.startswith(prefix) and query.lower() in k.lower():
                out.append({
                    "name": k[len(prefix):], "key": k,
                    "size": len(self.objects[bucket][k]), "mtime": 1700000000.0,
                    "etag": "e",
                })
        return {"files": out[:limit], "scanned": len(self.objects.get(bucket, {})),
                "complete": True}


class _NoCache:
    def get(self, key):
        return None

    def put(self, key, value):
        pass

    def clear(self):
        pass
