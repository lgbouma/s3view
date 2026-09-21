"""The HTTP layer over a transport that cannot presign.

A real Server on a real socket against a real directory, so the fork that
matters is covered end to end: ``/api/url`` has no presigned URL to give, so
it must hand back this server's own ranged proxy, and that proxy must answer a
``<video>`` element's range requests the way S3 would have.

file:// is the transport under test because it needs no host, but the fork is
on the URI scheme rather than the backend, so ssh:// takes the identical path.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from s3view import config
from s3view.server import Server
from tests import synth

MOVIE = bytes(range(256)) * 512  # 128 KB of position-dependent bytes
TEXT = b"pipeline log line\n" * 32


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(MOVIE)
    (tmp_path / "notes.log").write_bytes(TEXT)
    (tmp_path / "image.fits").write_bytes(synth.fits_bytes(height=64, width=48))
    (tmp_path / "night2").mkdir()
    return tmp_path


@pytest.fixture
def live(tree):
    cfg = dict(config.DEFAULTS)
    cfg["start"] = config.normalize_uri(str(tree))
    srv = Server(("127.0.0.1", 0), cfg, verbose=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.base = "http://127.0.0.1:%d" % srv.server_address[1]
    srv.root, srv.prefix = config.parse_uri(cfg["start"])
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def api(srv, route, **params):
    params["t"] = srv.token
    url = srv.base + "/api/" + route + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    resp = urllib.request.urlopen(req)
    with resp:
        return resp, resp.read()


# --- listing ----------------------------------------------------------
def test_a_local_directory_lists_like_a_prefix(live):
    page = api(live, "list", bucket=live.root, prefix=live.prefix)
    assert [f["name"] for f in page["folders"]] == ["night2"]
    kinds = {f["name"]: f["kind"] for f in page["files"]}
    assert kinds == {"image.fits": "fits", "movie.mp4": "video", "notes.log": "text"}


def test_start_location_survives_the_round_trip(live, tree):
    """What the CLI normalised has to be what the UI can parse back."""
    cfg = api(live, "config")
    assert cfg["start"] == "file://" + str(tree)
    assert config.format_uri(*config.parse_uri(cfg["start"])) == cfg["start"] + "/"


def test_text_preview_reads_one_range(live):
    res = api(live, "text", bucket=live.root, key=live.prefix + "notes.log")
    assert res["text"].startswith("pipeline log line")
    assert res["read"] == len(TEXT) and not res["truncated"]


# --- the fork: no presigning, so proxy ---------------------------------
def test_api_url_hands_back_the_local_proxy(live):
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4")
    assert res["proxied"] is True
    assert res["url"].startswith(live.base + "/api/object?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(res["url"]).query)
    # It is as sensitive as a presigned URL and gated the same way.
    assert query["t"] == [live.token]
    assert query["key"] == [live.prefix + "movie.mp4"]


def test_the_proxy_url_is_refused_without_its_token(live):
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(res["url"].replace(live.token, "not-the-token"))
    assert exc.value.code == 403


def test_the_proxy_serves_the_whole_object_with_no_range(live):
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4")
    resp, body = get(res["url"])
    assert resp.status == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Type"] == "video/mp4"
    assert body == MOVIE


@pytest.mark.parametrize("header,lo,hi", [
    ("bytes=0-65535", 0, 65535),                      # a video opening
    ("bytes=65536-131071", 65536, len(MOVIE) - 1),    # a seek to the middle
    ("bytes=131000-", 131000, len(MOVIE) - 1),        # a seek to the end
    ("bytes=-1024", len(MOVIE) - 1024, len(MOVIE) - 1),
])
def test_the_proxy_answers_range_requests_like_s3(live, header, lo, hi):
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4")
    resp, body = get(res["url"], {"Range": header})
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes %d-%d/%d" % (lo, hi, len(MOVIE))
    assert int(resp.headers["Content-Length"]) == hi - lo + 1
    assert body == MOVIE[lo:hi + 1]


def test_seeking_costs_only_the_range_asked_for(live):
    """The whole reason to preserve ranges rather than serve the file."""
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4")
    _resp, body = get(res["url"], {"Range": "bytes=100000-100099"})
    assert len(body) == 100
    assert body == MOVIE[100000:100100]


def test_download_asks_the_browser_to_save(live):
    res = api(live, "url", bucket=live.root, key=live.prefix + "movie.mp4", download=1)
    resp, body = get(res["url"])
    assert resp.headers["Content-Disposition"] == 'attachment; filename="movie.mp4"'
    assert body == MOVIE


def test_a_missing_object_is_a_404_not_a_traceback(live):
    res = api(live, "url", bucket=live.root, key=live.prefix + "gone.mp4")
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(res["url"])
    assert exc.value.code == 404


# --- previews over the same transport ---------------------------------
def test_fits_metadata_and_thumbnail(live):
    key = live.prefix + "image.fits"
    meta = api(live, "sci/meta", bucket=live.root, key=key)
    assert meta["kind"] == "fits"
    assert [u["sel"] for u in meta["units"]] == ["0", "1", "2"]
    assert "OBJECT" in meta["text"]

    url = live.base + "/api/thumb?" + urllib.parse.urlencode(
        {"bucket": live.root, "key": key, "size": 128, "t": live.token})
    resp, png = get(url)
    assert resp.headers["Content-Type"] == "image/png"
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_recursive_search(live, tree):
    (tree / "night2" / "deep.log").write_bytes(b"x")
    res = api(live, "search", bucket=live.root, prefix=live.prefix, q="deep")
    assert [f["name"] for f in res["files"]] == ["night2/deep.log"]


def test_refresh_clears_the_listing_cache(live, tree):
    api(live, "list", bucket=live.root, prefix=live.prefix)
    (tree / "late.log").write_bytes(b"arrived after the first listing")
    assert api(live, "refresh")["ok"] is True
    names = [f["name"] for f in api(live, "list", bucket=live.root,
                                    prefix=live.prefix)["files"]]
    assert "late.log" in names


def test_bucket_list_is_empty_rather_than_an_error(live):
    """No credentials, no botocore, no problem: this session is not on S3."""
    assert isinstance(api(live, "buckets")["buckets"], list)


# --- prefixes name folders --------------------------------------------
def test_a_prefix_without_a_trailing_slash_still_yields_usable_keys(live):
    """A pasted path has no trailing slash, and keys are built by concatenation.

    Without normalising, every key came back as "…/moviesfile.mp4": the folder
    listing looked perfectly correct and every single preview 404'd.
    """
    blunt = live.prefix.rstrip("/")
    page = api(live, "list", bucket=live.root, prefix=blunt)
    keys = {f["name"]: f["key"] for f in page["files"]}
    assert keys["movie.mp4"] == blunt + "/movie.mp4"
    # and the key actually resolves, which is the part that was broken
    res = api(live, "url", bucket=live.root, key=keys["movie.mp4"])
    resp, body = get(res["url"], {"Range": "bytes=0-63"})
    assert resp.status == 206 and body == MOVIE[:64]


def test_folder_prefixes_from_a_blunt_listing_are_navigable(live):
    blunt = live.prefix.rstrip("/")
    page = api(live, "list", bucket=live.root, prefix=blunt)
    assert page["folders"][0]["prefix"] == blunt + "/night2/"
    assert api(live, "list", bucket=live.root,
               prefix=page["folders"][0]["prefix"])["files"] == []


def test_search_with_a_blunt_prefix_yields_usable_keys(live, tree):
    (tree / "night2" / "deep.log").write_bytes(b"x")
    res = api(live, "search", bucket=live.root, prefix=live.prefix.rstrip("/"), q="deep")
    assert res["files"][0]["key"] == live.prefix + "night2/deep.log"
    assert api(live, "text", bucket=live.root, key=res["files"][0]["key"])["text"] == "x"
