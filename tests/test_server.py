"""HTTP layer, driven against the in-memory store."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from s3view import config
from s3view.server import Server
from tests import synth
from tests.conftest import BUCKET


@pytest.fixture
def live_server():
    """A real Server on a real socket, wired to FakeS3."""
    fits_blob = synth.fits_bytes()
    asdf_blob, _d, _e = synth.asdf_bytes()

    s3 = synth.FakeS3()
    s3.put(BUCKET, "dir/image.fits", fits_blob)
    s3.put(BUCKET, "dir/frame.asdf", asdf_blob)
    s3.put(BUCKET, "dir/notes.txt", b"hello from s3view\n" * 10)
    s3.put(BUCKET, "dir/movie.mp4", b"\x00" * 4096)
    s3.put(BUCKET, "dir/sub/nested.txt", b"nested")

    cfg = dict(config.DEFAULTS)
    cfg["start"] = "s3://%s/" % BUCKET
    srv = Server(("127.0.0.1", 0), cfg, verbose=False)
    srv.s3 = s3  # replace the botocore-backed client
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = "http://127.0.0.1:%d" % srv.server_address[1]
    srv.fake = s3
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def fetch(srv, path, token=True, headers=None, raw=False):
    url = srv.base + path
    hdrs = dict(headers or {})
    if token:
        hdrs["X-S3View-Token"] = srv.token
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        return (resp, body) if raw else json.loads(body)


def status_of(srv, path, **kw):
    try:
        fetch(srv, path, raw=True, **kw)
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code


# --- access control ---------------------------------------------------
def test_api_requires_a_token(live_server):
    assert status_of(live_server, "/api/config", token=False) == 403


def test_api_rejects_a_wrong_token(live_server):
    assert status_of(
        live_server, "/api/config", token=False,
        headers={"X-S3View-Token": "not-the-token"}) == 403


def test_api_rejects_a_foreign_origin(live_server):
    """A page on any other site must not be able to read the user's buckets."""
    assert status_of(
        live_server, "/api/config", headers={"Origin": "https://evil.example"}) == 403


def test_api_allows_a_localhost_origin(live_server):
    assert status_of(
        live_server, "/api/config", headers={"Origin": "http://127.0.0.1:1234"}) == 200


def test_token_accepted_as_a_query_parameter(live_server):
    """Media elements cannot set headers, so the token is allowed in the URL."""
    assert status_of(live_server, "/api/config?t=" + live_server.token, token=False) == 200


# --- static -----------------------------------------------------------
def test_index_is_served_with_the_token_substituted(live_server):
    _resp, body = fetch(live_server, "/", token=False, raw=True)
    assert b"__TOKEN__" not in body
    assert live_server.token.encode() in body


def test_static_traversal_is_blocked(live_server):
    assert status_of(live_server, "/static/../../../../etc/passwd", token=False) == 404


def test_unknown_endpoint_404(live_server):
    assert status_of(live_server, "/api/nonesuch") == 404


def test_missing_arguments_400(live_server):
    assert status_of(live_server, "/api/list") == 400


# --- listing ----------------------------------------------------------
def test_config_reports_capabilities(live_server):
    body = fetch(live_server, "/api/config")
    assert body["start"] == "s3://%s/" % BUCKET
    for flag in ("images", "fits", "asdf", "numpy"):
        assert flag in body["capabilities"]


def test_listing_annotates_kind_and_thumbability(live_server):
    body = fetch(live_server, "/api/list?bucket=%s&prefix=dir/" % BUCKET)
    kinds = {f["name"]: f["kind"] for f in body["files"]}
    assert kinds["image.fits"] == "fits"
    assert kinds["frame.asdf"] == "asdf"
    assert kinds["movie.mp4"] == "video"
    assert kinds["notes.txt"] == "text"
    thumbable = {f["name"]: f["thumb"] for f in body["files"]}
    assert thumbable["image.fits"] and thumbable["frame.asdf"]
    assert not thumbable["movie.mp4"]
    assert [f["name"] for f in body["folders"]] == ["sub"]


def test_buckets_listed(live_server):
    assert BUCKET in [b["name"] for b in fetch(live_server, "/api/buckets")["buckets"]]


def test_search_is_recursive(live_server):
    body = fetch(live_server, "/api/search?bucket=%s&prefix=&q=nested" % BUCKET)
    assert [f["key"] for f in body["files"]] == ["dir/sub/nested.txt"]


# --- object metadata and previews -------------------------------------
def test_head_reports_kind(live_server):
    body = fetch(live_server, "/api/head?bucket=%s&key=dir/movie.mp4" % BUCKET)
    assert body["kind"] == "video"
    assert body["size"] == 4096


def test_url_is_presigned(live_server):
    body = fetch(live_server, "/api/url?bucket=%s&key=dir/movie.mp4" % BUCKET)
    assert "X-Amz-Signature" in body["url"]


def test_sci_meta_for_fits(live_server):
    body = fetch(live_server, "/api/sci/meta?bucket=%s&key=dir/image.fits" % BUCKET)
    assert body["kind"] == "fits"
    assert body["selector"] == "HDU"
    assert body["doc"] == "Header"
    assert len(body["units"]) == 3
    assert body["units"][0]["itemsize"] == 4
    assert "SIMPLE" in body["text"]


def test_sci_meta_for_asdf(live_server):
    body = fetch(live_server, "/api/sci/meta?bucket=%s&key=dir/frame.asdf" % BUCKET)
    assert body["kind"] == "asdf"
    assert body["selector"] == "array"
    assert body["doc"] == "Tree"
    assert [u["sel"] for u in body["units"]] == ["roman.data", "roman.err"]
    assert "instrument: SYNTH" in body["text"]


def test_sci_meta_refuses_other_types(live_server):
    assert status_of(
        live_server, "/api/sci/meta?bucket=%s&key=dir/notes.txt" % BUCKET) == 415


@pytest.mark.parametrize("key", ["dir/image.fits", "dir/frame.asdf"])
def test_thumb_renders_a_png(live_server, key):
    _resp, body = fetch(
        live_server, "/api/thumb?bucket=%s&key=%s&size=32&etag=x" % (BUCKET, key),
        raw=True)
    assert body.startswith(b"\x89PNG\r\n\x1a\n")


def test_thumb_honours_the_array_selector(live_server):
    base = "/api/thumb?bucket=%s&key=dir/frame.asdf&size=32&etag=x" % BUCKET
    _r1, data = fetch(live_server, base + "&sel=roman.data", raw=True)
    _r2, err = fetch(live_server, base + "&sel=roman.err", raw=True)
    assert data != err


def test_thumb_rejects_unpreviewable_types(live_server):
    assert status_of(
        live_server, "/api/thumb?bucket=%s&key=dir/movie.mp4" % BUCKET) == 415


def test_text_preview_is_ranged(live_server):
    body = fetch(live_server, "/api/text?bucket=%s&key=dir/notes.txt" % BUCKET)
    assert body["text"].startswith("hello from s3view")
    assert body["truncated"] is False
    assert body["read"] == body["size"]


# --- range proxy ------------------------------------------------------
def test_object_proxy_serves_partial_content(live_server):
    req = urllib.request.Request(
        live_server.base + "/api/object?bucket=%s&key=dir/movie.mp4" % BUCKET,
        headers={"X-S3View-Token": live_server.token, "Range": "bytes=100-199"})
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 100-199/4096"
        assert resp.headers["Accept-Ranges"] == "bytes"
    assert len(body) == 100


def test_object_proxy_serves_the_whole_object_without_a_range(live_server):
    _resp, body = fetch(
        live_server, "/api/object?bucket=%s&key=dir/movie.mp4" % BUCKET, raw=True)
    assert len(body) == 4096
