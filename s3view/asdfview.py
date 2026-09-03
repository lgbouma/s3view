"""Preview ASDF arrays that live in S3 without downloading them.

Same idea as `fitsview`, different container. An ASDF file is a YAML tree
describing the data, followed by binary blocks, and (usually) a block index at
the very end listing where each block starts. So two small ranged reads -- the
head for the tree, the tail for the index -- are enough to compute the exact
byte offset of any row of any array in the file. A 200 MB Roman WFI `_cal`
product then previews by reading a few MB of scattered rows.

Parsed directly rather than via the `asdf` package: the format's addressing is
simple, and the library would want to materialise whole arrays, which is the
one thing this program exists to avoid.
"""

import io
import re
import struct

from s3view import fitsview

BLOCK_MAGIC = b"\xd3BLK"
BLOCK_INDEX_MAGIC = b"#ASDF BLOCK INDEX"
TREE_END = b"\n..."

HEAD_CHUNK = 256 * 1024
TAIL_CHUNK = 64 * 1024
MAX_TREE = 16 * 1024 * 1024
MAX_BLOCK_READ = 256 * 1024 * 1024  # ceiling when a block must be read whole

# ASDF datatype name -> numpy base type. Byte order comes from the tree.
DTYPES = {
    "int8": "i1", "uint8": "u1", "bool8": "u1",
    "int16": "i2", "uint16": "u2",
    "int32": "i4", "uint32": "u4",
    "int64": "i8", "uint64": "u8",
    "float16": "f2", "float32": "f4", "float64": "f8",
    "complex64": "c8", "complex128": "c16",
}


class AsdfError(Exception):
    pass


def _yaml_loader():
    """A SafeLoader that tolerates ASDF's custom tags instead of refusing them."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def unknown(loader, tag_suffix, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    Loader.add_multi_constructor("", unknown)
    return Loader


def _walk(obj, path=""):
    """Yield (path, node) for every ndarray-looking mapping in the tree."""
    if isinstance(obj, dict):
        if "source" in obj and "datatype" in obj and "shape" in obj:
            yield path, obj
        for k, v in obj.items():
            yield from _walk(v, "%s.%s" % (path, k) if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, "%s[%d]" % (path, i))


class RemoteASDF:
    def __init__(self, s3, bucket, key, size=None):
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.size = size if size is not None else s3.head(bucket, key)["size"]
        self._tree = None
        self._blocks = None
        self._raw_tree = None

    def _read(self, start, length):
        start = max(0, start)
        end = min(start + length, self.size) - 1
        if end < start:
            return b""
        return self.s3.get_range(self.bucket, self.key, start, end)

    # -- tree ------------------------------------------------------------
    def tree(self):
        if self._tree is not None:
            return self._tree
        import yaml

        buf = b""
        while len(buf) < MAX_TREE:
            chunk = self._read(len(buf), HEAD_CHUNK)
            if not chunk:
                break
            buf += chunk
            cut = buf.find(TREE_END)
            if cut >= 0:
                break
            if len(chunk) < HEAD_CHUNK:
                break
        if not buf.startswith(b"#ASDF"):
            raise AsdfError("not an ASDF file (missing #ASDF signature)")
        cut = buf.find(TREE_END)
        if cut < 0:
            raise AsdfError("could not find the end of the ASDF tree")
        self._raw_tree = buf[:cut].decode("utf-8", errors="replace")
        self._tree = yaml.load(io.BytesIO(buf[:cut]), Loader=_yaml_loader()) or {}
        return self._tree

    def tree_text(self):
        self.tree()
        return self._raw_tree or ""

    # -- blocks ----------------------------------------------------------
    def block_offsets(self):
        """Start offset of every binary block, cheaply."""
        if self._blocks is not None:
            return self._blocks
        offsets = self._block_index_from_tail()
        if offsets is None:
            offsets = self._walk_block_headers()
        self._blocks = offsets
        return offsets

    def _block_index_from_tail(self):
        import yaml

        want = TAIL_CHUNK
        while want <= 4 * 1024 * 1024:
            tail = self._read(self.size - want, want)
            pos = tail.find(BLOCK_INDEX_MAGIC)
            if pos >= 0:
                text = tail[pos:].decode("latin-1")
                body = text.split("---", 1)
                if len(body) < 2:
                    return None
                try:
                    idx = yaml.safe_load(body[1].split("\n...")[0])
                except Exception:
                    return None
                if isinstance(idx, list) and all(isinstance(v, int) for v in idx):
                    return idx
                return None
            if want >= self.size:
                return None
            want *= 4
        return None

    def _walk_block_headers(self):
        """Fallback: hop block to block using each header's allocated_size."""
        first = None
        buf = self._read(0, HEAD_CHUNK)
        first = buf.find(BLOCK_MAGIC)
        if first < 0:
            raise AsdfError("no binary blocks found")
        offsets = []
        pos = first
        while pos < self.size and len(offsets) < 4096:
            hdr = self._read(pos, 64)
            if not hdr.startswith(BLOCK_MAGIC):
                break
            offsets.append(pos)
            hsize = struct.unpack(">H", hdr[4:6])[0]
            _flags, _comp, allocated, _used, _dsize = struct.unpack(">I4sQQQ", hdr[6:38])
            pos = pos + 6 + hsize + allocated
        return offsets

    def block_header(self, source):
        offsets = self.block_offsets()
        if source >= len(offsets):
            raise AsdfError("block %d not present" % source)
        start = offsets[source]
        hdr = self._read(start, 64)
        if not hdr.startswith(BLOCK_MAGIC):
            raise AsdfError("block %d is not where the index says" % source)
        hsize = struct.unpack(">H", hdr[4:6])[0]
        flags, comp, allocated, used, dsize = struct.unpack(">I4sQQQ", hdr[6:38])
        comp = comp.rstrip(b"\x00")
        return {
            "start": start,
            "data_offset": start + 6 + hsize,
            "compression": comp.decode("latin-1"),
            "allocated": allocated,
            "used": used,
            "data_size": dsize,
        }

    # -- arrays ----------------------------------------------------------
    def arrays(self):
        """Every ndarray in the tree, largest first."""
        out = []
        for path, node in _walk(self.tree()):
            shape = node.get("shape") or []
            if not isinstance(node.get("source"), int):
                continue  # inline or external array; not a local block
            dt = str(node.get("datatype"))
            if dt not in DTYPES:
                continue
            n = 1
            for d in shape:
                n *= int(d)
            out.append(
                {
                    "path": path,
                    "source": node["source"],
                    "shape": [int(d) for d in shape],
                    "datatype": dt,
                    "byteorder": node.get("byteorder", "little"),
                    "offset": int(node.get("offset", 0) or 0),
                    "strides": node.get("strides"),
                    "elements": n,
                }
            )
        out.sort(key=lambda a: -a["elements"])
        return out

    def image_array(self, sel=None):
        arrays = self.arrays()
        images = [a for a in arrays if len(a["shape"]) >= 2]
        if not images:
            raise AsdfError("no 2-D array in this file")
        if sel in (None, "", "auto"):
            return images[0]  # largest: the science image in practice
        for a in arrays:
            if a["path"] == sel:
                return a
        raise AsdfError("no array at %r" % sel)

    def read_preview_array(self, size=256, sel=None, plane=0):
        """Decimated 2-D array via strided ranged reads."""
        import numpy as np

        arr_meta = self.image_array(sel)
        blk = self.block_header(arr_meta["source"])
        if blk["compression"]:
            return self._read_compressed(arr_meta, blk, size, plane)

        shape = arr_meta["shape"]
        height, width = int(shape[-2]), int(shape[-1])
        order = "<" if str(arr_meta["byteorder"]).startswith("little") else ">"
        dtype = np.dtype(order + DTYPES[arr_meta["datatype"]])
        itemsize = dtype.itemsize
        row_bytes = width * itemsize

        base = blk["data_offset"] + arr_meta["offset"]
        if len(shape) > 2:  # cube: skip to the requested plane
            base += int(plane) * height * width * itemsize

        rows = sorted(set(np.linspace(0, height - 1, min(size, height)).astype(int).tolist()))
        wanted = [(base + r * row_bytes, base + (r + 1) * row_bytes - 1) for r in rows]
        merged = fitsview._coalesce(wanted, min(fitsview.COALESCE_GAP, row_bytes))
        blobs = self.s3.get_ranges(self.bucket, self.key, merged)
        buf = {m[0]: b for m, b in zip(merged, blobs)}

        out = np.empty((len(rows), width), dtype=dtype)
        for i, (rstart, _rend) in enumerate(wanted):
            mstart = max(m for m in buf if m <= rstart)
            off = rstart - mstart
            chunk = buf[mstart][off : off + row_bytes]
            if len(chunk) < row_bytes:
                chunk = chunk + b"\0" * (row_bytes - len(chunk))
            out[i] = np.frombuffer(chunk, dtype=dtype)

        arr = out.astype("f4")
        arr = fitsview._bin_columns(arr, min(size, width))
        return arr, arr_meta

    def _read_compressed(self, arr_meta, blk, size, plane):
        """Compressed blocks cannot be strided: decompress, then decimate."""
        import numpy as np

        if blk["used"] > MAX_BLOCK_READ:
            raise AsdfError("compressed block is too large to preview (%d bytes)" % blk["used"])
        raw = self._read(blk["data_offset"], blk["used"])
        comp = blk["compression"]
        if comp == "zlib":
            import zlib

            raw = zlib.decompress(raw)
        elif comp == "bzp2":
            import bz2

            raw = bz2.decompress(raw)
        else:
            raise AsdfError("unsupported ASDF compression %r" % comp)

        shape = arr_meta["shape"]
        height, width = int(shape[-2]), int(shape[-1])
        order = "<" if str(arr_meta["byteorder"]).startswith("little") else ">"
        dtype = np.dtype(order + DTYPES[arr_meta["datatype"]])
        flat = np.frombuffer(raw, dtype=dtype, offset=arr_meta["offset"])
        planesz = height * width
        if len(shape) > 2:
            flat = flat[int(plane) * planesz : (int(plane) + 1) * planesz]
        flat = flat[:planesz]
        full = flat.reshape(height, width)
        rows = np.linspace(0, height - 1, min(size, height)).astype(int)
        arr = full[rows, :].astype("f4")
        return fitsview._bin_columns(arr, min(size, width)), arr_meta


def preview_png(s3, bucket, key, size=256, sel=None, plane=0, stretch="zscale",
                cmap="gray", etag="", filesize=None, use_cache=True):
    """Cached ASDF preview PNG."""
    import os

    params = "asdf|%s|%s|%s|%s|%s" % (size, sel, plane, stretch, cmap)
    path = fitsview.cache_path(bucket, key, etag, params)
    if use_cache and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    ra = RemoteASDF(s3, bucket, key, filesize)
    arr, _meta = ra.read_preview_array(size=size, sel=sel, plane=plane)
    png, _limits = fitsview.render_png(arr, stretch=stretch, cmap=cmap)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, path)
    except OSError:
        pass
    return png
