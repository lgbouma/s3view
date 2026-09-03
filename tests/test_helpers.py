"""Pure helpers: URI parsing, type detection, caching."""

import time

import pytest

from s3view import config, thumbs
from s3view.s3client import TTLCache
from s3view.server import _itemsize, kind_of


@pytest.mark.parametrize("uri,expected", [
    ("s3://bucket/", ("bucket", "")),
    ("s3://bucket", ("bucket", "")),
    ("bucket/a/b", ("bucket", "a/b/")),
    ("s3://bucket/a/b/", ("bucket", "a/b/")),
    ("s3://bucket/deep/nested/prefix", ("bucket", "deep/nested/prefix/")),
])
def test_parse_uri(uri, expected):
    assert config.parse_uri(uri) == expected


@pytest.mark.parametrize("key,kind", [
    ("a/b.fits", "fits"), ("a/b.FITS", "fits"), ("a/b.fits.gz", "fits"),
    ("a/b.asdf", "asdf"),
    ("a/b.mp4", "video"), ("a/b.mkv", "video"),
    ("a/b.png", "image"), ("a/b.tif", "image"),
    ("a/b.txt", "text"), ("a/b.param", "text"), ("a/b.json", "text"),
    ("a/b.pdf", "pdf"),
    ("a/b.npy", "binary"), ("a/b.h5", "binary"),
    ("a/b.unknownext", "other"), ("a/noextension", "other"),
])
def test_kind_of(key, kind):
    assert kind_of(key) == kind


def test_asdf_is_not_lumped_in_with_binary():
    """Regression: .asdf used to fall through to the generic binary bucket."""
    assert kind_of("x.asdf") == "asdf"
    assert thumbs.can_thumb("x.asdf") is True


@pytest.mark.parametrize("key,ext", [
    ("a/b.FITS", "fits"), ("a/b.fits.gz", "fits"), ("a/b.fits.fz", "fits"),
    ("a/b.PNG", "png"), ("noext", ""),
])
def test_ext_of(key, ext):
    assert thumbs.ext_of(key) == ext


def test_can_thumb_rejects_media():
    assert thumbs.can_thumb("a/b.mp4") is False
    assert thumbs.can_thumb("a/b.txt") is False


@pytest.mark.parametrize("dtype,size", [
    ("float32", 4), ("float64", 8), ("uint32", 4), ("int8", 1),
    ("complex128", 16), ("nonsense", 0),
])
def test_itemsize(dtype, size):
    """complex128 in particular broke an earlier string-slicing implementation."""
    assert _itemsize(dtype) == size


def test_ttl_cache_roundtrip_and_expiry():
    c = TTLCache(maxsize=8, ttl=10.0)
    assert c.get("missing") is None
    c.put("k", 42)
    assert c.get("k") == 42
    c.clear()
    assert c.get("k") is None

    expired = TTLCache(maxsize=8, ttl=-1.0)
    expired.put("k", 1)
    assert expired.get("k") is None


def test_ttl_cache_evicts_when_full():
    c = TTLCache(maxsize=8, ttl=60.0)
    for i in range(20):
        c.put("k%d" % i, i)
    assert len(c._d) <= 8
