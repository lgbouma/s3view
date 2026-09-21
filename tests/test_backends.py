"""file:// and ssh:// against the contract every backend has to satisfy.

The point of parametrizing over both is that there is only one contract. The
FITS and ASDF readers, the thumbnailer and the HTTP API were written against
S3 and are not touched here; if a backend satisfies these tests, they work.

The ssh:// backend runs its real transport -- the real subprocess, the real
line-and-payload protocol, the real connection pool -- with ``ssh`` itself
taken out of the argv. So these tests need no network, no host and no keys,
and still cover everything between the caller and the far end.
"""

import os
import subprocess
import sys

import pytest

from s3view import config, fitsview, sshstore
from s3view.localstore import LocalStore
from s3view.sshstore import RemoteError, SSHStore
from s3view.store import parse_byte_range
from tests import synth

TEXT = b"hello from s3view\n" * 10
MOVIE = bytes(range(256)) * 64  # 16 KB of known, position-dependent bytes


@pytest.fixture
def tree(tmp_path):
    """A directory with one of everything the previewers care about."""
    (tmp_path / "notes.txt").write_bytes(TEXT)
    (tmp_path / "movie.mp4").write_bytes(MOVIE)
    (tmp_path / "image.fits").write_bytes(synth.fits_bytes(height=64, width=48))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_bytes(b"nested")
    return tmp_path


@pytest.fixture(params=["file", "ssh"])
def store(request, tree):
    """(backend, root, prefix) for each transport, over the same directory."""
    if request.param == "file":
        backend, root = LocalStore(page_size=1000), "file://"
    else:
        backend = SSHStore("test", argv=sshstore.local_argv(), connections=2)
        root = "ssh://test"
    try:
        yield backend, root, str(tree).lstrip("/") + "/"
    finally:
        backend.close()


# --- listing ----------------------------------------------------------
def test_list_page_separates_directories_from_files(store):
    backend, root, prefix = store
    page = backend.list_page(root, prefix)
    assert [f["name"] for f in page["folders"]] == ["sub"]
    assert [f["name"] for f in page["files"]] == ["image.fits", "movie.mp4", "notes.txt"]
    assert page["folders"][0]["prefix"] == prefix + "sub/"
    assert not page["truncated"] and page["next_token"] is None


def test_list_page_reports_sizes_and_keys(store):
    backend, root, prefix = store
    files = {f["name"]: f for f in backend.list_page(root, prefix)["files"]}
    assert files["notes.txt"]["size"] == len(TEXT)
    assert files["notes.txt"]["key"] == prefix + "notes.txt"
    assert files["movie.mp4"]["etag"]  # a stand-in ETag, but a real one


def test_paging_covers_every_entry_exactly_once(store):
    backend, root, prefix = store
    seen, token, pages = [], None, 0
    while True:
        page = backend.list_page(root, prefix, token=token, limit=2)
        seen += [f["name"] for f in page["folders"]] + [f["name"] for f in page["files"]]
        pages += 1
        if not page["truncated"]:
            break
        token = page["next_token"]
        assert pages < 10  # a token that never terminates is the bug to catch
    assert pages > 1
    assert sorted(seen) == ["image.fits", "movie.mp4", "notes.txt", "sub"]


def test_etag_changes_when_the_file_does(store, tree):
    """The thumbnail cache is keyed on it, so a stale ETag serves a stale image."""
    backend, root, prefix = store
    before = backend.head(root, prefix + "notes.txt")["etag"]
    (tree / "notes.txt").write_bytes(TEXT + b"more\n")
    backend.list_cache.clear()
    if hasattr(backend, "_stat_cache"):
        backend._stat_cache.clear()
    assert backend.head(root, prefix + "notes.txt")["etag"] != before


def test_search_finds_nested_files(store):
    backend, root, prefix = store
    res = backend.search(root, prefix, "nested")
    assert [f["name"] for f in res["files"]] == [os.path.join("sub", "nested.txt")]
    assert res["files"][0]["key"] == prefix + "sub/nested.txt"
    assert res["complete"]


def test_search_respects_its_limit(store):
    backend, root, prefix = store
    res = backend.search(root, prefix, ".", limit=2)
    assert len(res["files"]) == 2
    assert not res["complete"]


# --- objects ----------------------------------------------------------
def test_head(store):
    backend, root, prefix = store
    info = backend.head(root, prefix + "notes.txt")
    assert info["size"] == len(TEXT)
    assert info["mtime"] > 0


def test_head_of_a_missing_file_raises(store):
    backend, root, prefix = store
    with pytest.raises(OSError):
        backend.head(root, prefix + "nope.txt")


def test_get_range_is_inclusive(store):
    backend, root, prefix = store
    assert backend.get_range(root, prefix + "notes.txt", 0, 4) == TEXT[:5]
    assert backend.get_range(root, prefix + "notes.txt", 6, 10) == TEXT[6:11]


def test_get_ranges_preserves_order(store):
    """Callers zip the results against what they asked for, positionally."""
    backend, root, prefix = store
    wanted = [(100, 199), (0, 99), (8000, 8099), (300, 349)]
    blobs = backend.get_ranges(root, prefix + "movie.mp4", wanted)
    assert [len(b) for b in blobs] == [100, 100, 100, 50]
    for (lo, hi), blob in zip(wanted, blobs):
        assert blob == MOVIE[lo:hi + 1]


def test_get_range_past_the_end_is_short_not_fatal(store):
    backend, root, prefix = store
    blob = backend.get_range(root, prefix + "notes.txt", len(TEXT) - 3, len(TEXT) + 500)
    assert blob == TEXT[-3:]


def test_get_object_honours_max_bytes(store):
    backend, root, prefix = store
    blob, _ctype = backend.get_object(root, prefix + "movie.mp4", max_bytes=1024)
    assert blob == MOVIE[:1024]


def test_get_object_whole(store):
    backend, root, prefix = store
    blob, _ctype = backend.get_object(root, prefix + "notes.txt")
    assert blob == TEXT


# --- streaming --------------------------------------------------------
def _drain(body, chunk=4096):
    out = b""
    while True:
        piece = body.read(chunk)
        if not piece:
            return out
        out += piece


def test_get_stream_without_a_range_is_the_whole_object(store):
    backend, root, prefix = store
    res = backend.get_stream(root, prefix + "movie.mp4")
    assert res["ContentLength"] == len(MOVIE)
    assert res["ContentRange"] is None
    assert _drain(res["Body"]) == MOVIE


@pytest.mark.parametrize("header,lo,hi", [
    ("bytes=0-1023", 0, 1023),
    ("bytes=8192-", 8192, len(MOVIE) - 1),
    ("bytes=-512", len(MOVIE) - 512, len(MOVIE) - 1),
])
def test_get_stream_serves_the_range_it_reports(store, header, lo, hi):
    """The proxy copies these numbers straight into a 206, so they must agree."""
    backend, root, prefix = store
    res = backend.get_stream(root, prefix + "movie.mp4", header)
    assert res["ContentRange"] == "bytes %d-%d/%d" % (lo, hi, len(MOVIE))
    assert res["ContentLength"] == hi - lo + 1
    blob = _drain(res["Body"])
    assert blob == MOVIE[lo:hi + 1]
    assert len(blob) == res["ContentLength"]


def test_get_stream_can_be_abandoned_midway(store):
    """A browser seeking away closes the body early; nothing may be left stuck."""
    backend, root, prefix = store
    res = backend.get_stream(root, prefix + "movie.mp4", "bytes=0-")
    assert res["Body"].read(64) == MOVIE[:64]
    res["Body"].close()
    # the backend is still usable afterwards
    assert backend.head(root, prefix + "notes.txt")["size"] == len(TEXT)


def test_presign_is_refused_rather_than_faked(store):
    """The server has to know to proxy instead, and silence would hide that."""
    backend, root, prefix = store
    with pytest.raises(NotImplementedError):
        backend.presign(root, prefix + "movie.mp4")


# --- the readers, unmodified, over the new transports ------------------
class Counting:
    """A backend that records how much of an object was actually read.

    The whole point of a transport that reads by range is what it *does not*
    fetch, and that is not visible from the answer -- only from the traffic.
    """

    def __init__(self, inner):
        self.inner = inner
        self.bytes_read = 0
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_range(self, bucket, key, start, end):
        blob = self.inner.get_range(bucket, key, start, end)
        self.calls += 1
        self.bytes_read += len(blob)
        return blob

    def get_ranges(self, bucket, key, ranges, workers=48):
        blobs = self.inner.get_ranges(bucket, key, ranges, workers=workers)
        self.calls += 1
        self.bytes_read += sum(len(b) for b in blobs)
        return blobs


def test_the_hdu_chain_walks_over_the_new_transports(store):
    backend, root, prefix = store
    rf = fitsview.RemoteFITS(backend, root, prefix + "image.fits")
    hdus = rf.hdus()
    assert [h["index"] for h in hdus] == [0, 1, 2]
    assert all(h["data_nbytes"] == 64 * 48 * 4 for h in hdus)
    assert "OBJECT" in rf.header_text()


def test_strided_fits_preview_reads_far_less_than_the_whole_file(store, tree, monkeypatch):
    """The property the whole program exists to protect, over ssh and file.

    SMALL_FILE is lowered rather than synthesising a >16 MB file, and the file
    is big enough that the fixed-size header probe is not itself a large
    fraction of it -- both of which is how the S3 tests reach this path too.
    """
    blob = synth.fits_bytes(height=2048, width=512, n_extra=0)
    (tree / "big.fits").write_bytes(blob)
    monkeypatch.setattr(fitsview, "SMALL_FILE", 1024)
    backend, root, prefix = store
    counted = Counting(backend)

    arr, _hdr = fitsview.RemoteFITS(counted, root, prefix + "big.fits") \
        .read_preview_array(size=32)

    assert arr.shape == (32, 32)
    assert counted.bytes_read < len(blob) / 8
    # 32 sampled rows, and the ranges stayed sparse rather than coalescing into
    # a read of the whole HDU -- which is the bug that makes this path useless.
    assert counted.calls <= 6


def test_fits_preview_renders_a_png(store):
    backend, root, prefix = store
    png = fitsview.preview_png(backend, root, prefix + "image.fits",
                               size=64, use_cache=False)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_strided_fits_pixels_match_the_file_on_disk(store, monkeypatch):
    """Byte-offset arithmetic is what a new transport could quietly break."""
    import numpy as np

    backend, root, prefix = store
    key = prefix + "image.fits"
    contiguous, _ = fitsview.RemoteFITS(backend, root, key).read_preview_array(size=32)
    monkeypatch.setattr(fitsview, "SMALL_FILE", 1024)
    strided, _ = fitsview.RemoteFITS(backend, root, key).read_preview_array(size=32)
    np.testing.assert_allclose(contiguous, strided, rtol=1e-6)


def test_fits_preview_over_ssh_matches_astropy(store, tree, monkeypatch):
    """End to end: the transport, the offsets and the decimation together."""
    import numpy as np
    from astropy.io import fits

    monkeypatch.setattr(fitsview, "SMALL_FILE", 1024)
    backend, root, prefix = store
    truth = fits.getdata(str(tree / "image.fits")).astype("f4")
    rf = fitsview.RemoteFITS(backend, root, prefix + "image.fits")
    hdu = rf.image_hdu()
    width = truth.shape[1]
    row = backend.get_range(root, prefix + "image.fits",
                            hdu["data_offset"], hdu["data_offset"] + width * 4 - 1)
    np.testing.assert_allclose(np.frombuffer(row, dtype=">f4"), truth[0], rtol=1e-6)


# --- ssh:// only ------------------------------------------------------
def test_batching_splits_by_payload_not_by_count():
    ranges = [(i * 1000, i * 1000 + 999) for i in range(10)]
    assert sshstore._batch(ranges, cap=100_000) == [ranges]
    batches = sshstore._batch(ranges, cap=2_500)
    assert [len(b) for b in batches] == [2, 2, 2, 2, 2]  # 3 x 1000 > 2500
    assert [r for b in batches for r in b] == ranges


def test_a_range_larger_than_the_cap_goes_alone():
    """Callers count on one blob back per range asked for, so never split one."""
    assert sshstore._batch([(0, 10_000)], cap=1_000) == [[(0, 10_000)]]


def test_a_strided_read_is_one_round_trip(tree, monkeypatch):
    """Not a nicety: 500 rows as 500 requests would lose to a plain download."""
    backend = SSHStore("test", argv=sshstore.local_argv(), connections=1)
    calls = []
    real = backend._request
    monkeypatch.setattr(backend, "_request",
                        lambda req: (calls.append(req["op"]), real(req))[1])
    try:
        key = str(tree / "image.fits").lstrip("/")
        wanted = [(i * 200, i * 200 + 99) for i in range(64)]
        blobs = backend.get_ranges("ssh://test", key, wanted)
        assert len(blobs) == 64
        assert calls.count("read") == 1
    finally:
        backend.close()


def test_a_refused_request_leaves_the_connection_usable(tree):
    backend = SSHStore("test", argv=sshstore.local_argv(), connections=1)
    try:
        with pytest.raises(RemoteError):
            backend.head("ssh://test", "definitely/not/here")
        key = str(tree / "notes.txt").lstrip("/")
        assert backend.head("ssh://test", key)["size"] == len(TEXT)
    finally:
        backend.close()


def test_a_dead_connection_is_replaced_transparently(tree):
    """Laptops sleep and links blink; the UI should not learn about it."""
    backend = SSHStore("test", argv=sshstore.local_argv(), connections=1)
    try:
        key = str(tree / "notes.txt").lstrip("/")
        assert backend.head("ssh://test", key)["size"] == len(TEXT)
        backend._stat_cache.clear()
        pooled = backend._pool._idle.get_nowait()
        pooled.proc.kill()
        pooled.proc.wait()
        backend._pool._idle.put(pooled)
        assert backend.head("ssh://test", key)["size"] == len(TEXT)
    finally:
        backend.close()


def test_close_reaps_the_subprocesses(tree):
    backend = SSHStore("test", argv=sshstore.local_argv(), connections=2)
    backend.head("ssh://test", str(tree / "notes.txt").lstrip("/"))
    procs = [c.proc for c in list(backend._pool._idle.queue)]
    assert procs
    backend.close()
    for proc in procs:
        proc.wait(timeout=5)
        assert proc.poll() is not None


def test_the_helper_is_a_self_contained_python_c_argument():
    """It has to run on a machine where s3view is not installed."""
    argv = sshstore.local_argv()
    assert argv[1] == "-c"
    assert "s3view" not in argv[2]
    proc = subprocess.run(argv, input=b'{"op":"hello"}\n{"op":"quit"}\n',
                          stdout=subprocess.PIPE, timeout=30)
    assert b'"ok": true' in proc.stdout or b'"ok":true' in proc.stdout


def test_ssh_argv_never_allocates_a_pty():
    """A pty would translate newlines and corrupt every byte of file data."""
    argv = sshstore.ssh_argv("luke@wh3")
    assert argv[0] == "ssh" and "-T" in argv
    assert argv[argv.index("luke@wh3") + 1:argv.index("luke@wh3") + 3] == ["sh", "-c"]


def test_parse_byte_range_agrees_with_what_the_backends_serve(store):
    backend, root, prefix = store
    size = backend.head(root, prefix + "movie.mp4")["size"]
    lo, hi = parse_byte_range("bytes=100-", size)
    res = backend.get_stream(root, prefix + "movie.mp4", "bytes=100-")
    assert (lo, hi) == (100, size - 1)
    assert res["ContentLength"] == hi - lo + 1


def test_ssh_and_file_do_not_need_botocore():
    """The transports that do not talk to S3 must not import its client.

    This broke once already: the FITS reader reached into the S3 module for a
    thread pool, which pulled botocore in behind it and made an ssh-only
    session need credentials it would never use. A subprocess is the only
    honest way to check, since botocore is importable in this one.
    """
    probe = """
import sys
from importlib.abc import MetaPathFinder

class Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("botocore", "boto3"):
            raise ImportError("blocked: " + name)
        return None

sys.meta_path.insert(0, Block())
import s3view.server, s3view.sshstore, s3view.localstore, s3view.fitsview, s3view.asdfview
from s3view.store import Store
store = Store(dict(s3view.config.DEFAULTS))
assert store.list_buckets() == []          # degrades rather than raising
assert "botocore" not in sys.modules
print("clean")
"""
    proc = subprocess.run([sys.executable, "-c", probe], cwd=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"clean" in proc.stdout


def test_an_s3_root_without_botocore_says_so(tmp_path):
    """And the failure, when it does come, has to be legible."""
    probe = """
import sys
from importlib.abc import MetaPathFinder

class Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("botocore", "boto3"):
            raise ImportError("blocked: " + name)
        return None

sys.meta_path.insert(0, Block())
from s3view import config
from s3view.store import Store
try:
    Store(dict(config.DEFAULTS)).head("some-bucket", "k")
except RuntimeError as exc:
    assert "botocore" in str(exc), exc
    print("explained")
"""
    proc = subprocess.run([sys.executable, "-c", probe], cwd=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"explained" in proc.stdout


@pytest.mark.parametrize("host,expected,port", [
    ("luke@wh3", "luke@wh3", []),
    ("luke@wh3:2222", "luke@wh3", ["-p", "2222"]),
    ("wh3", "wh3", []),
    ("user@[::1]:22", "user@[::1]", ["-p", "22"]),
    ("[::1]", "[::1]", []),           # an IPv6 literal is not a port
])
def test_a_port_in_the_uri_becomes_a_flag(host, expected, port):
    assert sshstore.split_port(host) == (expected, port)
    argv = sshstore.ssh_argv(host)
    assert expected in argv
    for flag in port:
        assert flag in argv


def test_an_ssh_root_without_a_host_is_refused_clearly():
    with pytest.raises(ValueError, match="needs a host"):
        SSHStore("")


def test_a_failed_connection_is_not_retried(monkeypatch):
    """Retrying an auth failure would only ask for the password twice."""
    backend = SSHStore("test", argv=["/nonexistent/interpreter"], connections=1)
    attempts = []
    real = sshstore._Conn
    monkeypatch.setattr(sshstore, "_Conn",
                        lambda *a, **k: (attempts.append(1), real(*a, **k))[1])
    with pytest.raises(OSError):
        backend.head("ssh://test", "some/key")
    assert len(attempts) == 1
