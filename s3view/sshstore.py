"""SSH transport: files on another machine, behind the same (bucket, key) surface.

A connection is an OpenSSH subprocess running ``s3view.remote_helper``, so
``~/.ssh/config`` decides everything about how it is made -- hostname aliases,
keys, agent forwarding, jump hosts, 2FA -- exactly as it would for a bare
``ssh``. Nothing is installed on the far side: the helper's source travels in
the command line.

What SSH cannot do is hand the browser a URL, so media here is served through
this program's own ranged proxy rather than straight from the store. The
property that actually matters survives: a preview still reads only the bytes
it needs. Because the helper answers a whole *list* of ranges in one round
trip, a strided FITS read of 500 rows costs one request carrying exactly those
rows -- so remote previews stay a fraction of the file rather than becoming
500 round trips and losing to a plain download.

Concurrency is several connections rather than several requests per
connection. That keeps the protocol trivially ordered, and ControlMaster
multiplexing means the pool costs one authentication and one TCP connection no
matter how many members it has.
"""

import base64
import contextlib
import json
import mimetypes
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

from s3view import config, remote_helper
from s3view.store import FIRST_CHUNK, MAX_CHUNK, TTLCache, etag_of, parse_byte_range

# One request carries at most this much, so a huge strided read is split
# rather than buffered whole on both ends.
MAX_BATCH = 32 * 1024 * 1024


class SSHError(OSError):
    """The connection is gone. Whoever catches this must not reuse it."""


class RemoteError(OSError):
    """The far side refused a request. The connection is still fine."""


# -- launching -------------------------------------------------------------

def _helper_source():
    path = getattr(remote_helper, "__file__", None)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    import inspect

    return inspect.getsource(remote_helper)


def _wrapper():
    """The helper as a self-contained ``python -c`` argument.

    Compressed and base64'd so it survives two levels of shell quoting without
    any escaping at all -- the payload is then pure ``[A-Za-z0-9+/=]``, which
    no shell has an opinion about -- and so the command line stays a couple of
    kilobytes rather than ten.
    """
    blob = base64.b64encode(zlib.compress(_helper_source().encode("utf-8"), 9))
    return ('import base64,zlib;exec(zlib.decompress(base64.b64decode("%s")))'
            % blob.decode("ascii"))


def local_argv(python=None):
    """Run the helper on *this* machine: the transport with the ssh taken out.

    The seam the tests drive, and a usable escape hatch for any other way of
    getting a pipe to a remote interpreter (``docker exec``, a batch system).
    """
    return [python or sys.executable, "-c", _wrapper()]


def _control_path():
    """A ControlMaster socket, so the pool costs one authentication.

    Unix socket paths cap out around 104 bytes and ``%C`` expands to 64 hex
    characters, so decline rather than hand ssh a path it will truncate --
    losing multiplexing is survivable, a mysteriously broken connection is not.
    """
    path = os.path.join(config.CACHE_DIR, "ssh-%C")
    if len(path) - len("%C") + 64 > 100:
        return None
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
    except OSError:
        return None
    return path


def split_port(host):
    """``user@host:2222`` -> ``("user@host", ["-p", "2222"])``.

    A port usually belongs in ~/.ssh/config, but a URI is entitled to carry
    one and ssh will not accept it glued to the hostname.
    """
    head, sep, tail = host.rpartition(":")
    if sep and head and tail.isdigit():
        return head, ["-p", tail]
    return host, []


def ssh_argv(host, ssh_bin="ssh", options=(), python=None):
    host, port = split_port(host)
    argv = [
        ssh_bin,
        # No pty: a pty would translate newlines and corrupt every byte of
        # file data on the way back.
        "-T",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    control = _control_path()
    if control:
        argv += ["-o", "ControlMaster=auto",
                 "-o", "ControlPath=" + control,
                 "-o", "ControlPersist=120"]
    argv += port + [str(o) for o in options]
    # ssh joins the command words and hands the string to the remote shell, so
    # both halves are quoted here for that shell, not for the local one. The
    # `sh -c` wrapper exists only to find an interpreter: python3 where there
    # is one, python where the environment has been set up the old way.
    launch = ('P=%s; command -v "$P" >/dev/null 2>&1 || P=python; exec "$P" -c "$0"'
              % shlex.quote(python or "python3"))
    return argv + [host, "sh", "-c", shlex.quote(launch), shlex.quote(_wrapper())]


# -- connections -----------------------------------------------------------

class _Conn:
    """One ssh subprocess speaking the helper protocol, one request at a time.

    stderr is drained continuously into a small ring. That is not tidiness:
    "Permission denied (publickey)" and "Host key verification failed" only
    ever arrive there, and without them a failure to connect reaches the user
    as an unexplained blank page.
    """

    def __init__(self, argv, label="ssh"):
        self.label = label
        self._err = []
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True,
        )
        threading.Thread(target=self._drain, daemon=True).start()
        hello = self.request({"op": "hello"})
        self.remote = hello

    def _drain(self):
        try:
            for line in iter(self.proc.stderr.readline, b""):
                self._err.append(line.decode("utf-8", "replace").rstrip())
                del self._err[:-20]
        except Exception:
            pass

    def _why(self, what):
        detail = "\n".join(self._err[-6:]).strip()
        code = self.proc.poll()
        if code is not None and not detail:
            detail = "ssh exited with status %d" % code
        return "%s: %s%s" % (self.label, what, "\n" + detail if detail else "")

    def request(self, req):
        try:
            self.proc.stdin.write(json.dumps(req).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            raise SSHError(self._why("connection closed while sending a request"))
        line = self.proc.stdout.readline()
        if not line:
            raise SSHError(self._why("connection closed"))
        try:
            res = json.loads(line.decode("utf-8"))
        except ValueError:
            raise SSHError(self._why("unparseable reply %r" % line[:200]))
        if not res.get("ok"):
            raise RemoteError("%s: %s" % (self.label, res.get("error") or "remote error"))
        lens = res.get("lens")
        if lens:
            res["blobs"] = [self._read_exactly(n) for n in lens]
        return res

    def _read_exactly(self, n):
        chunks, got = [], 0
        while got < n:
            chunk = self.proc.stdout.read(n - got)
            if not chunk:
                raise SSHError(self._why("connection closed mid-payload"))
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def close(self):
        try:
            self.proc.stdin.write(b'{"op":"quit"}\n')
            self.proc.stdin.flush()
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


class _Pool:
    """Lazily grown, LIFO. Empty until something is actually read.

    Lazy matters when authentication is interactive: the first connection
    prompts once, ControlMaster makes the rest free, and a session that only
    browses listings never opens a second one.
    """

    def __init__(self, factory, maxsize):
        self._factory = factory
        self._max = max(1, int(maxsize))
        self._idle = queue.LifoQueue()
        self._live = 0
        self._closed = False
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def lease(self):
        conn = self._acquire()
        try:
            yield conn
        except RemoteError:
            self._release(conn)  # a refused request leaves the stream intact
            raise
        except BaseException:
            self._discard(conn)  # anything else and we cannot trust the stream
            raise
        else:
            self._release(conn)

    def _acquire(self):
        while True:
            try:
                return self._idle.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                if self._closed:
                    raise SSHError("connection pool is closed")
                grow = self._live < self._max
                if grow:
                    self._live += 1
            if not grow:
                return self._idle.get()  # all busy; wait for one back
            try:
                return self._factory()
            except BaseException:
                with self._lock:
                    self._live -= 1
                raise

    def _release(self, conn):
        with self._lock:
            if self._closed:
                self._live -= 1
                conn.close()
                return
        self._idle.put(conn)

    def _discard(self, conn):
        with self._lock:
            self._live -= 1
        conn.close()

    def close(self):
        with self._lock:
            self._closed = True
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return
            except Exception:
                pass


# -- the store -------------------------------------------------------------

class SSHStore:
    scheme = "ssh"

    def __init__(self, host, ssh_bin="ssh", options=(), python=None,
                 connections=4, page_size=1000, argv=None):
        if not host and argv is None:
            raise ValueError("ssh:// needs a host, as in ssh://user@host/path/")
        self.host = host
        self.page_size = page_size
        self.argv = argv or ssh_argv(host, ssh_bin=ssh_bin, options=options, python=python)
        self._pool = _Pool(lambda: _Conn(self.argv, label="ssh://" + host), connections)
        self._batchers = ThreadPoolExecutor(
            max_workers=max(1, connections), thread_name_prefix="s3view-ssh"
        )
        self.list_cache = TTLCache(maxsize=512, ttl=90.0)
        self._stat_cache = TTLCache(maxsize=1024, ttl=90.0)

    # -- protocol --------------------------------------------------------
    def _request(self, req):
        connected = []

        def attempt():
            with self._pool.lease() as conn:
                connected.append(True)
                return conn.request(req)

        try:
            return attempt()
        except SSHError:
            if not connected:
                # We never got a connection at all -- wrong host, refused key.
                # Retrying would only ask for the password a second time.
                raise
            # A dropped connection is the ordinary cost of a laptop that slept
            # or a link that blinked. The dead one has already been discarded,
            # so one retry on a fresh connection hides it.
            return attempt()

    def _stat(self, key):
        hit = self._stat_cache.get(key)
        if hit is None:
            res = self._request({"op": "stat", "path": config.path_of(key)})
            hit = {"size": res["size"], "mtime": res["mtime"], "dir": res["dir"]}
            self._stat_cache.put(key, hit)
        return hit

    # -- listing ---------------------------------------------------------
    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        limit = limit or self.page_size
        ck = (prefix, token, limit)
        cached = self.list_cache.get(ck)
        if cached is not None:
            return cached
        t0 = time.time()
        res = self._request({
            "op": "list", "path": config.path_of(prefix),
            "offset": int(token or 0), "limit": limit,
        })
        nxt = res.get("next")
        page = {
            "bucket": bucket,
            "prefix": prefix,
            "folders": [{"name": n, "prefix": prefix + n + "/"} for n, _m in res["dirs"]],
            "files": [
                {"name": n, "key": prefix + n, "size": sz, "mtime": mt,
                 "etag": etag_of(sz, mt), "storage": "STANDARD"}
                for n, sz, mt in res["files"]
            ],
            "truncated": nxt is not None,
            "next_token": str(nxt) if nxt is not None else None,
            "ms": round((time.time() - t0) * 1000),
        }
        self.list_cache.put(ck, page)
        return page

    def search(self, bucket, prefix, query, max_keys=50000, limit=500, deadline=15.0):
        res = self._request({
            "op": "find", "path": config.path_of(prefix), "q": query,
            "limit": limit, "max_entries": max_keys, "deadline": deadline,
        })
        return {
            "files": [
                {"name": rel, "key": prefix + rel, "size": sz, "mtime": mt,
                 "etag": etag_of(sz, mt)}
                for rel, sz, mt in res["files"]
            ],
            "scanned": res["scanned"],
            "complete": res["complete"],
        }

    # -- objects ---------------------------------------------------------
    def head(self, bucket, key):
        st = self._stat(key)
        return {
            "key": key,
            "size": st["size"],
            "mtime": st["mtime"],
            "etag": etag_of(st["size"], st["mtime"]),
            "content_type": mimetypes.guess_type(key)[0] or "",
            "storage": "STANDARD",
            "metadata": {},
        }

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        raise NotImplementedError(
            "ssh:// has no presigned URLs; the server proxies these instead"
        )

    def get_range(self, bucket, key, start, end):
        return self.get_ranges(bucket, key, [(start, end)])[0]

    def get_ranges(self, bucket, key, ranges, workers=48):
        path = config.path_of(key)
        batches = _batch([(int(a), int(b)) for a, b in ranges], MAX_BATCH)

        def run(batch):
            res = self._request({"op": "read", "path": path,
                                 "ranges": [list(r) for r in batch]})
            return res.get("blobs") or []

        if len(batches) <= 1:
            return run(batches[0]) if batches else []
        out = []
        for blobs in self._batchers.map(run, batches):
            out.extend(blobs)
        return out

    def read_span(self, key, start, end):
        """Consecutive blobs covering an inclusive span, in growing chunks.

        Small first so a video's first frames arrive after one short round
        trip, growing so watching to the end costs a request per 8 MB rather
        than one per browser buffer.
        """
        path = config.path_of(key)
        pos, step = start, FIRST_CHUNK
        while pos <= end:
            n = min(step, end - pos + 1)
            res = self._request({"op": "read", "path": path,
                                 "ranges": [[pos, pos + n - 1]]})
            blob = (res.get("blobs") or [b""])[0]
            if not blob:
                return
            yield blob
            pos += len(blob)
            step = min(step * 2, MAX_CHUNK)

    def get_object(self, bucket, key, max_bytes=None):
        size = self._stat(key)["size"]
        want = min(size, max_bytes) if max_bytes else size
        ctype = mimetypes.guess_type(key)[0] or ""
        if want <= 0:
            return b"", ctype
        return b"".join(self.read_span(key, 0, want - 1)), ctype

    def get_stream(self, bucket, key, byte_range=None):
        size = self._stat(key)["size"]
        start, end = parse_byte_range(byte_range, size)
        return {
            "Body": _StreamBody(self, key, start, end),
            "ContentLength": max(0, end - start + 1),
            "ContentType": mimetypes.guess_type(key)[0] or "application/octet-stream",
            "ContentRange": ("bytes %d-%d/%d" % (start, end, size)) if byte_range else None,
        }

    def list_buckets(self):
        return []

    def close(self):
        self._pool.close()
        self._batchers.shutdown(wait=False)


def _batch(ranges, cap):
    """Group ranges so no single request carries more than *cap* bytes.

    A range bigger than the cap goes alone rather than being split, because
    callers count on getting back one blob per range they asked for.
    """
    out, current, total = [], [], 0
    for lo, hi in ranges:
        n = max(0, hi - lo + 1)
        if current and total + n > cap:
            out.append(current)
            current, total = [], 0
        current.append((lo, hi))
        total += n
    if current:
        out.append(current)
    return out


class _StreamBody:
    """read()/close() over ranged reads, in the shape the HTTP proxy wants.

    ``read(n)`` returns *up to* n bytes and an empty string at the end, which
    is all the proxy's loop asks for -- and means the next chunk is only
    fetched when the browser has taken the last one, so abandoning a stream
    costs nothing more than the chunk in flight.
    """

    def __init__(self, store, key, start, end):
        self._chunks = store.read_span(key, start, end)
        self._buf = b""
        self._done = False

    def read(self, n=-1):
        if not self._buf and not self._done:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                self._done = True
        if n is None or n < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):
        self._done = True
        self._buf = b""
        self._chunks.close()
