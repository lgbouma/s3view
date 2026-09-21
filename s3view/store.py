"""Backend routing: one (bucket, key) namespace over several transports.

Everything above this module -- the HTTP API, the FITS and ASDF readers, the
thumbnailer -- speaks in ``(bucket, key)`` pairs and never learns which
transport answered. That is what makes a second transport additive: a backend
is the nine methods below, and nothing upstairs changes.

    bare name          an S3 bucket          s3client.S3
    ssh://user@host    a tree over SSH       sshstore.SSHStore
    file://            this machine          localstore.LocalStore

The S3 backend is imported lazily, so an ssh-only session never needs botocore
and an ssh-only machine never needs credentials.

Only S3 can hand the browser a URL it can fetch by itself; see ``scheme_of``
callers in the server for where that fork lives.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from s3view import config

# Shared pool for background work: listing prefetch, and anything else that
# should not block a request. Transport-agnostic, and here rather than in the
# S3 client so that reaching a thread pool does not require botocore.
POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="s3view")

# Strided reads get their own pool so a big FITS preview cannot starve listing
# prefetch (and vice versa).
RANGE_POOL = ThreadPoolExecutor(max_workers=48, thread_name_prefix="s3view-range")

# How much of an object one backend call may carry, and the shape media reads
# grow through. Kept here because every non-S3 transport wants the same
# answers: start small so the first video frames arrive quickly, then grow so
# a long read costs round trips proportional to its size, not its buffer.
FIRST_CHUNK = 512 * 1024
MAX_CHUNK = 8 * 1024 * 1024


class TTLCache:
    """Small thread-safe TTL + LRU cache."""

    def __init__(self, maxsize=512, ttl=90.0):
        self.maxsize = maxsize
        self.ttl = ttl
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires < time.time():
                self._d.pop(key, None)
                return None
            return value

    def put(self, key, value):
        with self._lock:
            if len(self._d) >= self.maxsize:
                # Drop the soonest-to-expire entries; cheap and good enough here.
                for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.maxsize // 4]:
                    self._d.pop(k, None)
            self._d[key] = (time.time() + self.ttl, value)

    def clear(self):
        with self._lock:
            self._d.clear()


def parse_byte_range(header, size):
    """``'bytes=100-199'`` -> ``(100, 199)``, clamped to an object of *size*.

    Falls back to the whole object when there is no usable header, and honours
    only the first range of a multi-range request -- which is all any media
    element actually sends.
    """
    last = size - 1
    if size <= 0:
        return 0, -1
    if not header or not header.strip().lower().startswith("bytes="):
        return 0, last
    spec = header.split("=", 1)[1].split(",")[0].strip()
    lo, _, hi = spec.partition("-")
    try:
        if not lo:  # suffix range: the final N bytes
            n = int(hi)
            return (max(0, size - n), last) if n > 0 else (0, last)
        start = int(lo)
        end = int(hi) if hi else last
    except ValueError:
        return 0, last
    start = max(0, min(start, last))
    end = max(start, min(end, last))
    return start, end


def etag_of(size, mtime):
    """A stand-in ETag for transports that have none.

    It only has to change when the file does, because that is all it is used
    for: keying the thumbnail cache.
    """
    return "%x-%x" % (int(size), int(float(mtime) * 1e6))


class Store:
    """Dispatches each call to the backend that owns the given root."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.page_size = cfg.get("page_size", 1000)
        self._backends = {}
        self._lock = threading.Lock()
        self.list_cache = _Caches(self)

    # -- routing ---------------------------------------------------------
    def backend(self, bucket):
        scheme = config.scheme_of(bucket)
        # One S3 backend serves every bucket it can reach; SSH is per host.
        ident = scheme if scheme in ("s3", "file") else bucket
        with self._lock:
            b = self._backends.get(ident)
            if b is None:
                b = self._build(scheme, bucket)
                self._backends[ident] = b
            return b

    def _build(self, scheme, bucket):
        cfg = self.cfg
        if scheme == "s3":
            try:
                from s3view.s3client import S3
            except ImportError as exc:
                raise RuntimeError(
                    "reading S3 needs botocore, which this interpreter does not "
                    "have (%s)" % exc
                )
            return S3(
                profile=cfg.get("profile"),
                region=cfg.get("region"),
                endpoint_url=cfg.get("endpoint_url"),
                page_size=self.page_size,
            )
        if scheme == "file":
            from s3view.localstore import LocalStore

            return LocalStore(page_size=self.page_size)
        if scheme == "ssh":
            from s3view.sshstore import SSHStore

            return SSHStore(
                bucket.split("://", 1)[1],
                options=cfg.get("ssh_options") or (),
                python=cfg.get("ssh_python"),
                connections=int(cfg.get("ssh_connections") or 4),
                page_size=self.page_size,
            )
        raise ValueError(
            "%s:// is not a location s3view can read; expected one of %s"
            % (scheme, ", ".join(s + "://" for s in config.SCHEMES))
        )

    # -- the surface every backend implements ----------------------------
    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        return self.backend(bucket).list_page(bucket, prefix, token, limit, delimiter)

    def search(self, bucket, prefix, query, max_keys=50000, limit=500, deadline=15.0):
        return self.backend(bucket).search(
            bucket, prefix, query, max_keys=max_keys, limit=limit, deadline=deadline
        )

    def head(self, bucket, key):
        return self.backend(bucket).head(bucket, key)

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        return self.backend(bucket).presign(
            bucket, key, expires=expires, disposition=disposition,
            content_type=content_type,
        )

    def get_range(self, bucket, key, start, end):
        return self.backend(bucket).get_range(bucket, key, start, end)

    def get_ranges(self, bucket, key, ranges, workers=48):
        return self.backend(bucket).get_ranges(bucket, key, ranges, workers=workers)

    def get_object(self, bucket, key, max_bytes=None):
        return self.backend(bucket).get_object(bucket, key, max_bytes=max_bytes)

    def get_stream(self, bucket, key, byte_range=None):
        return self.backend(bucket).get_stream(bucket, key, byte_range)

    def list_buckets(self):
        """S3 buckets, or nothing at all.

        A session that only ever browses ssh:// has no credentials and no
        botocore to fail with, and the sidebar is not the place to learn that.
        """
        try:
            return self.backend("").list_buckets()
        except Exception:
            return []

    def close(self):
        with self._lock:
            live, self._backends = list(self._backends.values()), {}
        for b in live:
            try:
                b.close()
            except Exception:
                pass


class _Caches:
    """``store.list_cache.clear()``, fanned out over every live backend."""

    def __init__(self, store):
        self._store = store

    def clear(self):
        for b in list(self._store._backends.values()):
            cache = getattr(b, "list_cache", None)
            if cache is not None:
                cache.clear()
