"""The local filesystem behind the (bucket, key) surface.

Worth having twice over. It is useful by itself -- s3view over a directory of
pipeline products is the same program, and the FITS and ASDF readers do not
care that the ranged reads are now ``pread`` calls -- and it is what an NFS or
sshfs mount looks like, so a mounted remote tree needs no transport at all.

Nothing here is a security boundary: it runs as the user, who could read these
files anyway. Paths are normalised because a browser will happily send ``..``,
not because normalising them protects anything.
"""

import mimetypes
import os
import time

from s3view import config
from s3view.store import TTLCache, etag_of, parse_byte_range


class LocalStore:
    scheme = "file"

    def __init__(self, page_size=1000):
        self.page_size = page_size
        # Cheap here, but /api/refresh has to have something to clear, and a
        # directory of 100k files is not free to re-stat on every scroll.
        self.list_cache = TTLCache(maxsize=256, ttl=15.0)

    # -- paths -----------------------------------------------------------
    @staticmethod
    def _path(key):
        return os.path.normpath(os.path.expanduser(config.path_of(key)))

    # -- listing ---------------------------------------------------------
    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        limit = limit or self.page_size
        offset = int(token or 0)
        t0 = time.time()
        entries = self._entries(prefix)
        page = entries[offset:offset + limit]
        more = offset + limit < len(entries)
        return {
            "bucket": bucket,
            "prefix": prefix,
            "folders": [
                {"name": name, "prefix": prefix + name + "/"}
                for name, _size, _mtime, isdir in page if isdir
            ],
            "files": [
                {
                    "name": name, "key": prefix + name, "size": size,
                    "mtime": mtime, "etag": etag_of(size, mtime),
                    "storage": "STANDARD",
                }
                for name, size, mtime, isdir in page if not isdir
            ],
            "truncated": more,
            "next_token": str(offset + limit) if more else None,
            "ms": round((time.time() - t0) * 1000),
        }

    def _entries(self, prefix):
        """[(name, size, mtime, isdir)] for one directory, directories first.

        Cached against the directory's own mtime: paging is offset-based, so
        without this a second page would re-stat every entry, and the order
        has to stay stable between pages anyway.
        """
        path = self._path(prefix)
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            stamp = None
        hit = self.list_cache.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        dirs, files = [], []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    isdir = entry.is_dir()
                except OSError:  # a broken symlink; list it, do not crash on it
                    isdir = False
                try:
                    st = entry.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                (dirs if isdir else files).append(
                    (entry.name, 0 if isdir else size, mtime, isdir)
                )
        dirs.sort(key=lambda e: e[0])
        files.sort(key=lambda e: e[0])
        entries = dirs + files
        self.list_cache.put(path, (stamp, entries))
        return entries

    def search(self, bucket, prefix, query, max_keys=50000, limit=500, deadline=15.0):
        needle = query.lower()
        root = self._path(prefix)
        out, scanned = [], 0
        end = time.time() + deadline
        complete = True
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                scanned += 1
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if needle not in rel.lower():
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                out.append({
                    "name": rel, "key": prefix + rel, "size": st.st_size,
                    "mtime": st.st_mtime, "etag": etag_of(st.st_size, st.st_mtime),
                })
                if len(out) >= limit:
                    return {"files": out, "scanned": scanned, "complete": False}
            if scanned >= max_keys or time.time() > end:
                complete = False
                break
        return {"files": out, "scanned": scanned, "complete": complete}

    # -- objects ---------------------------------------------------------
    def head(self, bucket, key):
        st = os.stat(self._path(key))
        return {
            "key": key,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "etag": etag_of(st.st_size, st.st_mtime),
            "content_type": mimetypes.guess_type(key)[0] or "",
            "storage": "STANDARD",
            "metadata": {},
        }

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        raise NotImplementedError(
            "file:// has no presigned URLs; the server proxies these instead"
        )

    def get_range(self, bucket, key, start, end):
        return self.get_ranges(bucket, key, [(start, end)])[0]

    def get_ranges(self, bucket, key, ranges, workers=48):
        fd = os.open(self._path(key), os.O_RDONLY)
        try:
            return [_pread(fd, int(a), int(b)) for a, b in ranges]
        finally:
            os.close(fd)

    def get_object(self, bucket, key, max_bytes=None):
        path = self._path(key)
        with open(path, "rb") as fh:
            blob = fh.read(max_bytes) if max_bytes else fh.read()
        return blob, mimetypes.guess_type(path)[0] or ""

    def get_stream(self, bucket, key, byte_range=None):
        path = self._path(key)
        size = os.path.getsize(path)
        start, end = parse_byte_range(byte_range, size)
        fh = open(path, "rb")
        fh.seek(start)
        return {
            "Body": _Slice(fh, max(0, end - start + 1)),
            "ContentLength": max(0, end - start + 1),
            "ContentType": mimetypes.guess_type(path)[0] or "application/octet-stream",
            "ContentRange": ("bytes %d-%d/%d" % (start, end, size)) if byte_range else None,
        }

    def list_buckets(self):
        return []

    def close(self):
        pass


def _pread(fd, start, end):
    """One inclusive byte range, whatever short reads the kernel hands back."""
    if end < start:
        return b""
    want = end - start + 1
    chunks, got = [], 0
    while got < want:
        chunk = os.pread(fd, want - got, start + got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


class _Slice:
    """A bounded view of an open file, in the read/close shape the proxy wants."""

    def __init__(self, fh, remaining):
        self._fh = fh
        self._left = remaining

    def read(self, n=-1):
        if self._left <= 0:
            return b""
        want = self._left if n is None or n < 0 else min(n, self._left)
        blob = self._fh.read(want)
        self._left -= len(blob)
        return blob

    def close(self):
        self._left = 0
        self._fh.close()
