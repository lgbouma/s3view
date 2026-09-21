"""The far end of the SSH transport: a small file server on stdin/stdout.

``s3view.sshstore`` compresses this module's own source into a single
``python3 -c`` invocation, so the remote machine needs nothing installed --
which is the whole point, since the machine holding the data usually is not
the machine you installed s3view on. That constrains what this file may be:
standard library only, no imports from the s3view package, and nothing on
stdout that is not protocol.

The protocol is one JSON request per line in, one JSON response line out. A
response carrying file data declares ``lens`` and is followed immediately by
exactly ``sum(lens)`` raw bytes. Two consequences matter:

* Bytes are never escaped or encoded, so a read costs what it weighs.
* A whole list of ranges is answered in one round trip. That is why a strided
  FITS preview of 500 rows is one request here rather than 500 invocations of
  ``dd``, and it is the difference between "as fast as S3" and "unusable".

The header is written only once every blob is in hand, so a failure mid-read
cannot leave the caller reading file data as if it were JSON.
"""

import json
import os
import stat as statmod
import sys
import time

PROTOCOL = 1
# One response may not exceed this. The caller batches to stay well under it;
# the limit is here so a bad request fails loudly instead of by OOM.
MAX_PAYLOAD = 128 * 1024 * 1024
MAX_LIST_CACHE = 4

_LIST_CACHE = {}


def _expand(path):
    return os.path.expanduser(path or "/") or "/"


def _entries(path):
    """[(name, size, mtime, isdir)] for one directory, directories first.

    Cached against the directory's own mtime, because paging is offset-based:
    without this, page two would re-stat every entry, and the order has to
    stay stable between pages regardless.
    """
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        stamp = None
    hit = _LIST_CACHE.get(path)
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
            if isdir:
                dirs.append((entry.name, 0, mtime, True))
            else:
                files.append((entry.name, size, mtime, False))
    dirs.sort(key=lambda e: e[0])
    files.sort(key=lambda e: e[0])
    entries = dirs + files
    if len(_LIST_CACHE) >= MAX_LIST_CACHE:
        _LIST_CACHE.clear()
    _LIST_CACHE[path] = (stamp, entries)
    return entries


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


def op_hello(req):
    return {
        "protocol": PROTOCOL,
        "python": sys.version.split()[0],
        "uname": " ".join(os.uname()[:3]) if hasattr(os, "uname") else "",
        "home": os.path.expanduser("~"),
    }


def op_stat(req):
    st = os.stat(_expand(req.get("path")))
    return {
        "size": st.st_size,
        "mtime": st.st_mtime,
        "dir": statmod.S_ISDIR(st.st_mode),
    }


def op_list(req):
    entries = _entries(_expand(req.get("path")))
    offset = max(0, int(req.get("offset") or 0))
    limit = max(1, int(req.get("limit") or 1000))
    page = entries[offset:offset + limit]
    nxt = offset + limit
    return {
        "dirs": [[e[0], e[2]] for e in page if e[3]],
        "files": [[e[0], e[1], e[2]] for e in page if not e[3]],
        "total": len(entries),
        "next": nxt if nxt < len(entries) else None,
    }


def op_read(req):
    ranges = [(int(a), int(b)) for a, b in (req.get("ranges") or [])]
    total = sum(b - a + 1 for a, b in ranges if b >= a)
    if total > MAX_PAYLOAD:
        raise ValueError(
            "requested %d bytes in one call; the limit is %d" % (total, MAX_PAYLOAD)
        )
    fd = os.open(_expand(req.get("path")), os.O_RDONLY)
    try:
        blobs = [_pread(fd, a, b) for a, b in ranges]
    finally:
        os.close(fd)
    return {"lens": [len(b) for b in blobs]}, blobs


def op_find(req):
    root = _expand(req.get("path"))
    needle = (req.get("q") or "").lower()
    limit = max(1, int(req.get("limit") or 500))
    max_entries = max(1, int(req.get("max_entries") or 50000))
    deadline = time.time() + float(req.get("deadline") or 15.0)
    out, scanned, complete = [], 0, True
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            scanned += 1
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if needle not in rel.lower():
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append([rel, st.st_size, st.st_mtime])
            if len(out) >= limit:
                return {"files": out, "scanned": scanned, "complete": False}
        if scanned >= max_entries or time.time() > deadline:
            complete = False
            break
    return {"files": out, "scanned": scanned, "complete": complete}


OPS = {
    "hello": op_hello,
    "stat": op_stat,
    "list": op_list,
    "read": op_read,
    "find": op_find,
}


def serve(stdin, stdout):
    while True:
        line = stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        blobs = []
        try:
            req = json.loads(line.decode("utf-8"))
            if req.get("op") == "quit":
                return
            handler = OPS.get(req.get("op"))
            if handler is None:
                raise ValueError("unknown op %r" % (req.get("op"),))
            res = handler(req)
            if isinstance(res, tuple):
                res, blobs = res
            res = dict(res)
            res["ok"] = True
        except Exception as exc:  # noqa: BLE001 - answer, never die
            res, blobs = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, []
        stdout.write(json.dumps(res).encode("utf-8") + b"\n")
        for blob in blobs:
            stdout.write(blob)
        stdout.flush()


def main():
    try:  # die with the pipe rather than raising through a write
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        pass
    serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
