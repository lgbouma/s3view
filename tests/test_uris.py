"""Locations: one (root, key) namespace over three transports."""

import os

import pytest

from s3view import config
from s3view.store import parse_byte_range


@pytest.mark.parametrize("typed,uri", [
    # scp syntax is what people actually type, so it has to work
    ("luke@wh3:/ar0/data", "ssh://luke@wh3/ar0/data"),
    ("luke@wh3:~/movies", "ssh://luke@wh3/~/movies"),
    ("ssh://luke@wh3/ar0/data/", "ssh://luke@wh3/ar0/data/"),
    ("s3://bucket/prefix/", "s3://bucket/prefix/"),
    ("bucket/prefix/", "s3://bucket/prefix/"),   # a bare name is still a bucket
    ("", ""),
])
def test_normalize_uri(typed, uri):
    assert config.normalize_uri(typed) == uri


def test_normalize_uri_makes_local_paths_absolute():
    assert config.normalize_uri("/tmp/x") == "file:///tmp/x"
    assert config.normalize_uri("~") == "file://" + os.path.expanduser("~")


@pytest.mark.parametrize("uri,root,prefix", [
    ("s3://bucket/a/b", "bucket", "a/b/"),
    ("ssh://luke@wh3/ar0/ROMAN/movies", "ssh://luke@wh3", "ar0/ROMAN/movies/"),
    ("ssh://luke@wh3", "ssh://luke@wh3", ""),
    ("ssh://luke@wh3/~/movies", "ssh://luke@wh3", "~/movies/"),
    ("file:///Users/x/y", "file://", "Users/x/y/"),
    ("luke@wh3:/ar0/data", "ssh://luke@wh3", "ar0/data/"),
])
def test_parse_uri_splits_root_from_key(uri, root, prefix):
    assert config.parse_uri(uri) == (root, prefix)


@pytest.mark.parametrize("uri", [
    "s3://bucket/a/b/", "ssh://luke@wh3/ar0/data/", "ssh://luke@wh3/~/movies/",
    "file:///Users/x/y/", "s3://bucket/",
])
def test_format_uri_inverts_parse_uri(uri):
    assert config.format_uri(*config.parse_uri(uri)) == uri


@pytest.mark.parametrize("root,scheme", [
    ("bucket", "s3"), ("s3://bucket", "s3"),
    ("ssh://luke@wh3", "ssh"), ("file://", "file"), ("", "s3"),
])
def test_scheme_of(root, scheme):
    assert config.scheme_of(root) == scheme


def test_path_of_absolutizes_keys_but_leaves_home_alone():
    """Keys are root-relative; the slash the filesystem needs is added here.

    A leading ``~`` survives untouched because it is expanded by whichever
    machine owns the home directory, which for ssh:// is not this one.
    """
    assert config.path_of("ar0/data/x.fits") == "/ar0/data/x.fits"
    assert config.path_of("") == "/"
    assert config.path_of("~/movies/a.mp4") == "~/movies/a.mp4"


# --- range headers ----------------------------------------------------
@pytest.mark.parametrize("header,expected", [
    ("bytes=0-99", (0, 99)),
    ("bytes=100-", (100, 999)),          # what a <video> sends on open
    ("bytes=-50", (950, 999)),           # suffix range: the final N bytes
    ("bytes=0-5000", (0, 999)),          # clamped to the object
    ("bytes=0-99,200-299", (0, 99)),     # first range only
    ("", (0, 999)),
    (None, (0, 999)),
    ("items=0-99", (0, 999)),            # not a byte range at all
    ("bytes=abc-def", (0, 999)),
])
def test_parse_byte_range(header, expected):
    assert parse_byte_range(header, 1000) == expected


def test_parse_byte_range_of_an_empty_object_is_empty():
    start, end = parse_byte_range("bytes=0-10", 0)
    assert end - start + 1 <= 0


# --- the two implementations of parse_uri -----------------------------
#
# The browser splits URIs too, and its answer has to match Python's exactly.
# It did not, once: the JS half omitted the trailing slash that Python
# guarantees, so every key in a listing came back as "dir/subfile.mp4" --
# folder names still looked right and every preview 404'd.

PARITY_CASES = [
    "s3://bucket/a/b",
    "s3://bucket/a/b/",
    "s3://bucket",
    "ssh://luke@wh3/ar0/ROMAN/simu/LMC_20260906/movies",   # no trailing slash
    "ssh://luke@wh3/ar0/data/",
    "ssh://luke@wh3",
    "file:///Users/x/y",
    "file:///Users/x/y/",
]


def _js_splitUri(uris):
    """Run app.js's own splitUri under node, or skip if node is absent."""
    import json
    import pathlib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed; cannot cross-check the browser half")
    app = pathlib.Path(__file__).parent.parent / "s3view" / "static" / "app.js"
    src = app.read_text()
    # Lift the two declarations out of app.js rather than restating them, so
    # this test tracks the shipped code instead of a copy of it.
    start = src.index("const SCHEME_RE")
    end = src.index("\n}", src.index("function splitUri")) + 2
    script = src[start:end] + "\nconsole.log(JSON.stringify(%s.map(splitUri)));" % json.dumps(uris)
    out = subprocess.run([node, "-e", script], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()
    return [tuple(pair) for pair in json.loads(out.stdout)]


def test_the_browser_splits_uris_the_same_way_python_does():
    assert _js_splitUri(PARITY_CASES) == [config.parse_uri(u) for u in PARITY_CASES]


@pytest.mark.parametrize("uri", PARITY_CASES)
def test_a_non_empty_prefix_always_ends_in_a_slash(uri):
    """Because listings build every key as prefix + name."""
    _root, prefix = config.parse_uri(uri)
    assert prefix == "" or prefix.endswith("/")
