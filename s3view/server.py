"""Local HTTP server: a small JSON API plus the static UI.

Design notes
------------
* Media never proxies through Python. ``/api/url`` hands the browser a
  presigned S3 URL and the ``<video>`` element does its own range requests
  straight to S3, so seeking in a 700 MB movie costs a few hundred KB.
* Bound to 127.0.0.1 and gated behind a per-run token, because any web page
  you have open could otherwise reach a plain localhost server and read your
  buckets.
"""

import gzip
import json
import mimetypes
import os
import posixpath
import re
import secrets
import subprocess
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from s3view import __version__, asdfview, config, fitsview, thumbs
from s3view.s3client import S3

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

TEXT_EXT = {
    "txt", "log", "json", "yaml", "yml", "cfg", "conf", "ini", "param", "md", "csv",
    "tsv", "py", "sh", "c", "h", "cpp", "js", "html", "css", "xml", "toml", "dat",
    "reg", "list", "err", "out", "sql", "r", "pro", "tex", "gitignore", "in",
}
VIDEO_EXT = {"mp4", "m4v", "mov", "webm", "ogv", "mkv", "avi"}
AUDIO_EXT = {"mp3", "m4a", "wav", "aac", "flac", "ogg"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"}
FITS_EXT = {"fits", "fit", "fts", "fz"}
ASDF_EXT = {"asdf"}
# Both are scientific array containers previewed the same way: metadata from a
# small ranged read, then only the rows the preview needs.
SCI_EXT = FITS_EXT | ASDF_EXT

TEXT_PREVIEW_BYTES = 256 * 1024


def kind_of(key):
    """Coarse type used by the UI to pick a preview renderer."""
    ext = thumbs.ext_of(key)
    if ext in FITS_EXT:
        return "fits"
    if ext in ASDF_EXT:
        return "asdf"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    if ext in {"tif", "tiff"}:
        return "image"
    if ext == "pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    if ext in {"npy", "npz", "h5", "hdf5", "parquet", "pkl"}:
        return "binary"
    return "other"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "s3view/" + __version__

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _authorized(self, query):
        token = self.headers.get("X-S3View-Token") or (query.get("t", [None])[0])
        if not secrets.compare_digest(str(token or ""), self.server.token):
            return False
        # Block cross-origin reads from any page that happens to be open.
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            return False
        return True

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200, cache=None):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        extra = {"Cache-Control": cache or "no-store"}
        if len(raw) > 4096 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            raw = gzip.compress(raw, 5)
            extra["Content-Encoding"] = "gzip"
        self._send(code, raw, "application/json", extra)

    def _error(self, code, msg):
        self._json({"error": msg}, code=code)

    # -- routing ---------------------------------------------------------
    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_POST(self):
        self.do_GET()

    def do_GET(self, head_only=False):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._serve_index(head_only)
            if path.startswith("/static/"):
                return self._serve_static(path, head_only)
            if path == "/favicon.ico":
                return self._send(204)
            if path.startswith("/api/"):
                if not self._authorized(query):
                    return self._error(403, "bad or missing token")
                return self._api(path[5:], query, head_only)
            return self._error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - report, never take the server down
            if self.server.verbose:
                traceback.print_exc()
            try:
                self._error(500, "%s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass

    def _serve_index(self, head_only=False):
        with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
            html = fh.read()
        html = html.replace(b"__TOKEN__", self.server.token.encode())
        self._send(200, html, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"}, head_only)

    def _serve_static(self, path, head_only=False):
        rel = posixpath.normpath(path[len("/static/"):]).lstrip("./")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.abspath(full).startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype, {"Cache-Control": "no-store"}, head_only)

    # -- API -------------------------------------------------------------
    def _api(self, route, q, head_only=False):
        s3 = self.server.s3
        cfg = self.server.config
        one = lambda k, d=None: q.get(k, [d])[0]  # noqa: E731

        if route == "config":
            return self._json(
                {
                    "version": __version__,
                    "start": cfg.get("start"),
                    "bookmarks": cfg.get("bookmarks", []),
                    "page_size": cfg.get("page_size", 1000),
                    "external_player": cfg.get("external_player"),
                    "capabilities": self.server.capabilities,
                    "cache": thumbs.cache_stats(),
                }
            )

        if route == "buckets":
            return self._json({"buckets": s3.list_buckets()}, cache="max-age=60")

        if route == "list":
            bucket = one("bucket")
            if not bucket:
                return self._error(400, "bucket required")
            prefix = one("prefix", "") or ""
            token = one("token")
            limit = int(one("limit", cfg.get("page_size", 1000)))
            page = s3.list_page(bucket, prefix, token, limit)
            for f in page["files"]:
                f["kind"] = kind_of(f["key"])
                f["thumb"] = thumbs.can_thumb(f["key"])
            return self._json(page)

        if route == "search":
            bucket = one("bucket")
            prefix = one("prefix", "") or ""
            query = one("q", "") or ""
            if not bucket or not query:
                return self._error(400, "bucket and q required")
            res = s3.search(bucket, prefix, query, limit=int(one("limit", 500)))
            for f in res["files"]:
                f["kind"] = kind_of(f["key"])
                f["thumb"] = thumbs.can_thumb(f["key"])
            return self._json(res)

        if route == "head":
            bucket, key = one("bucket"), one("key")
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            info = s3.head(bucket, key)
            info["kind"] = kind_of(key)
            return self._json(info)

        if route == "url":
            bucket, key = one("bucket"), one("key")
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            disp = None
            if one("download"):
                name = key.rsplit("/", 1)[-1].replace('"', "")
                disp = 'attachment; filename="%s"' % name
            url = s3.presign(bucket, key, expires=int(cfg.get("presign_expires", 3600)),
                             disposition=disp)
            return self._json({"url": url, "expires_in": cfg.get("presign_expires", 3600)})

        if route == "thumb":
            bucket, key = one("bucket"), one("key")
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            size = max(16, min(1024, int(one("size", 256))))
            etag = one("etag", "") or ""
            filesize = one("size_bytes")
            kw = {}
            kind = kind_of(key)
            if kind in ("fits", "asdf"):
                sel = one("sel")
                kw = {
                    "stretch": one("stretch", "zscale"),
                    "cmap": one("cmap", "gray"),
                    "plane": int(one("plane", 0)),
                }
                # FITS selects an HDU by index; ASDF selects an array by path.
                if kind == "fits":
                    kw["index"] = int(sel) if sel not in (None, "", "auto") else None
                else:
                    kw["sel"] = sel if sel not in (None, "") else None
            try:
                data, ctype = thumbs.thumb(
                    s3, bucket, key, size=size, etag=etag,
                    filesize=int(filesize) if filesize else None, **kw
                )
            except Exception as exc:  # noqa: BLE001
                return self._error(415, "%s: %s" % (type(exc).__name__, exc))
            # Immutable: the cache key already includes the object's ETag.
            return self._send(200, data, ctype,
                              {"Cache-Control": "private, max-age=86400"}, head_only)

        if route == "sci/meta":
            bucket, key = one("bucket"), one("key")
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            kind = kind_of(key)
            try:
                if kind == "fits":
                    return self._json(_fits_meta(s3, bucket, key))
                if kind == "asdf":
                    return self._json(_asdf_meta(s3, bucket, key))
                return self._error(415, "no scientific metadata for this type")
            except Exception as exc:  # noqa: BLE001
                return self._error(415, "%s: %s" % (type(exc).__name__, exc))

        if route == "text":
            bucket, key = one("bucket"), one("key")
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            nbytes = min(int(one("bytes", TEXT_PREVIEW_BYTES)), 4 * 1024 * 1024)
            info = s3.head(bucket, key)
            blob = s3.get_range(bucket, key, 0, min(nbytes, info["size"]) - 1) if info["size"] else b""
            return self._json(
                {
                    "text": blob.decode("utf-8", errors="replace"),
                    "size": info["size"],
                    "truncated": info["size"] > len(blob),
                    "read": len(blob),
                }
            )

        if route == "object":  # ranged proxy; fallback path for odd media types
            return self._proxy(q, head_only)

        if route == "open":  # hand a presigned URL to a native player
            bucket, key = one("bucket"), one("key")
            app = one("app") or cfg.get("external_player") or "VLC"
            if not bucket or not key:
                return self._error(400, "bucket and key required")
            url = s3.presign(bucket, key, expires=int(cfg.get("presign_expires", 3600)))
            try:
                subprocess.Popen(["open", "-a", app, url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:  # noqa: BLE001
                return self._error(500, "could not launch %s: %s" % (app, exc))
            return self._json({"ok": True, "app": app})

        if route == "bookmarks":
            body = {}
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            marks = cfg.setdefault("bookmarks", [])
            action = body.get("action") or one("action")
            if action == "add":
                uri = body.get("uri")
                if uri and not any(m["uri"] == uri for m in marks):
                    marks.append({"name": body.get("name") or uri, "uri": uri})
                    config.save(cfg)
            elif action == "remove":
                uri = body.get("uri")
                cfg["bookmarks"] = [m for m in marks if m["uri"] != uri]
                config.save(cfg)
            elif action == "start":
                cfg["start"] = body.get("uri") or cfg.get("start")
                config.save(cfg)
            return self._json({"bookmarks": cfg.get("bookmarks", []), "start": cfg.get("start")})

        if route == "cache/clear":
            return self._json({"removed": thumbs.clear_cache()})

        if route == "refresh":
            s3.list_cache.clear()
            return self._json({"ok": True})

        return self._error(404, "no such endpoint: " + route)

    def _proxy(self, q, head_only=False):
        """Range-preserving passthrough for media the browser can't presign-load."""
        s3 = self.server.s3
        bucket = q.get("bucket", [None])[0]
        key = q.get("key", [None])[0]
        if not bucket or not key:
            return self._error(400, "bucket and key required")
        rng = self.headers.get("Range")
        try:
            resp = s3.get_stream(bucket, key, rng)
        except Exception as exc:  # noqa: BLE001
            return self._error(404, str(exc))
        body = resp["Body"]
        length = resp["ContentLength"]
        ctype = resp.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
        status = 206 if rng and resp.get("ContentRange") else 200
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if resp.get("ContentRange"):
            self.send_header("Content-Range", resp["ContentRange"])
        self.end_headers()
        if head_only:
            body.close()
            return
        try:
            while True:
                chunk = body.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser seeked away; entirely normal for video
        finally:
            body.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg, verbose=False):
        super().__init__(addr, Handler)
        self.config = cfg
        self.verbose = verbose
        self.token = secrets.token_urlsafe(24)
        self.s3 = S3(
            profile=cfg.get("profile"),
            region=cfg.get("region"),
            endpoint_url=cfg.get("endpoint_url"),
            page_size=cfg.get("page_size", 1000),
        )
        self.capabilities = _capabilities()
        self._lock = threading.Lock()


def _capabilities():
    """Which preview features are actually available in this interpreter."""
    caps = {}
    for name, mod in (("images", "PIL"), ("astropy", "astropy"),
                      ("numpy", "numpy"), ("yaml", "yaml")):
        try:
            __import__(mod)
            caps[name] = True
        except ImportError:
            caps[name] = False
    # Array previews need numpy; the container libraries are what differ.
    caps["fits"] = caps["astropy"] and caps["numpy"]
    caps["asdf"] = caps["yaml"] and caps["numpy"]
    return caps


def _fits_meta(s3, bucket, key):
    """HDU list plus the full header text, from one small ranged read."""
    rf = fitsview.RemoteFITS(s3, bucket, key)
    units = []
    for h in rf.hdus():
        hdr = h["header"]
        naxis = int(hdr.get("NAXIS", 0))
        shape = [hdr.get("NAXIS%d" % i) for i in range(1, naxis + 1)]
        name = hdr.get("EXTNAME") or (
            "PRIMARY" if h["index"] == 0 else hdr.get("XTENSION", "") or "IMAGE"
        )
        units.append(
            {
                "sel": str(h["index"]),
                "name": name,
                "shape": shape,
                "dtype": "BITPIX %s" % hdr.get("BITPIX"),
                "itemsize": abs(int(hdr.get("BITPIX", 0) or 0)) // 8,
                "bytes": h["data_nbytes"],
                "previewable": naxis >= 2 and bool(h["data_nbytes"]),
                "label": "%d %s %s" % (h["index"], name, "\u00d7".join(str(x) for x in shape)),
            }
        )
    return {"kind": "fits", "selector": "HDU", "doc": "Header",
            "text": rf.header_text(), "units": units}


def _itemsize(datatype):
    """Bytes per element for an ASDF datatype name."""
    code = asdfview.DTYPES.get(datatype)
    return int(code[1:]) if code else 0


def _asdf_meta(s3, bucket, key):
    """Every ndarray in the tree plus the raw YAML tree."""
    ra = asdfview.RemoteASDF(s3, bucket, key)
    arrays = ra.arrays()
    units = []
    for a in arrays:
        shape = a["shape"]
        units.append(
            {
                "sel": a["path"],
                "name": a["path"],
                "shape": shape,
                "dtype": a["datatype"],
                "itemsize": _itemsize(a["datatype"]),
                "bytes": a["elements"] * _itemsize(a["datatype"]),
                "previewable": len(shape) >= 2,
                "label": "%s %s %s" % (
                    a["path"], "\u00d7".join(str(x) for x in shape), a["datatype"]),
            }
        )
    return {"kind": "asdf", "selector": "array", "doc": "Tree",
            "text": ra.tree_text(), "units": units}
