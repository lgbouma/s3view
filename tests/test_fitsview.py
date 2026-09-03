"""FITS: byte-range arithmetic, decimation, rendering."""

import numpy as np
import pytest

from s3view import fitsview
from tests.conftest import BUCKET

KEY = "dir/image.fits"


# --- range coalescing -------------------------------------------------
def test_coalesce_merges_only_adjacent_ranges():
    dense = [(0, 99), (100, 199), (200, 299)]
    assert _coalesce_len(dense, gap=100) == 1


def test_coalesce_leaves_sparse_ranges_alone():
    """Regression for the bug that made large previews ~30x slower.

    Rows of a 4088-wide float32 image are 16352 bytes and, at 512 sampled rows,
    sit ~130 KB apart. With a fixed 128 KB threshold every range merged into a
    single read of the whole 67 MB HDU. The threshold must scale with row size.
    """
    row = 4088 * 4
    stride = row * 8  # 512 rows sampled out of 4088
    sparse = [(i * stride, i * stride + row - 1) for i in range(512)]

    assert _coalesce_len(sparse, gap=min(fitsview.COALESCE_GAP, row)) == 512
    # and the pathological setting demonstrates the failure it guards against
    assert _coalesce_len(sparse, gap=128 * 1024) == 1


def _coalesce_len(ranges, gap):
    return len(fitsview._coalesce([tuple(r) for r in ranges], gap))


def test_coalesced_ranges_cover_the_originals():
    row = 1000
    ranges = [(i * row * 3, i * row * 3 + row - 1) for i in range(10)]
    merged = fitsview._coalesce(ranges, gap=row * 3)
    for start, end in ranges:
        assert any(m0 <= start and end <= m1 for m0, m1 in merged)


# --- column binning ---------------------------------------------------
def test_bin_columns_averages():
    arr = np.arange(12, dtype="f4").reshape(2, 6)
    out = fitsview._bin_columns(arr, 3)
    assert out.shape == (2, 3)
    np.testing.assert_allclose(out[0], [0.5, 2.5, 4.5])


def test_bin_columns_is_a_noop_when_not_shrinking():
    arr = np.arange(12, dtype="f4").reshape(2, 6)
    assert fitsview._bin_columns(arr, 9).shape == (2, 6)


def test_bin_columns_handles_uneven_division():
    arr = np.ones((2, 7), dtype="f4")
    out = fitsview._bin_columns(arr, 3)
    assert out.shape == (2, 3)
    np.testing.assert_allclose(out, 1.0)


def test_bin_columns_survives_nan():
    arr = np.array([[1.0, np.nan, 3.0, 4.0]], dtype="f4")
    assert np.isfinite(fitsview._bin_columns(arr, 2)).all()


# --- rendering --------------------------------------------------------
@pytest.mark.parametrize("stretch", ["zscale", "asinh", "log", "99.5", "minmax"])
def test_render_png_produces_a_png(stretch):
    arr = np.linspace(0, 1000, 64 * 64).reshape(64, 64).astype("f4")
    png, (lo, hi) = fitsview.render_png(arr, stretch=stretch)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert hi > lo


def test_render_png_handles_a_constant_image():
    png, _ = fitsview.render_png(np.zeros((16, 16), dtype="f4"))
    assert png.startswith(b"\x89PNG")


def test_render_png_handles_all_nan():
    png, _ = fitsview.render_png(np.full((16, 16), np.nan, dtype="f4"))
    assert png.startswith(b"\x89PNG")


def test_colormap_produces_colour():
    arr = np.linspace(0, 1, 32 * 32).reshape(32, 32).astype("f4")
    grey, _ = fitsview.render_png(arr, cmap="gray")
    colour, _ = fitsview.render_png(arr, cmap="viridis")
    assert grey != colour


# --- end to end against the fake store --------------------------------
def test_hdu_walk_finds_every_hdu(s3_with_fits):
    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY)
    hdus = rf.hdus()
    assert len(hdus) == 3
    assert [h["header"]["NAXIS1"] for h in hdus] == [48, 48, 48]
    assert hdus[0]["header"]["OBJECT"] == "SYNTH"


def test_data_offsets_are_block_aligned(s3_with_fits):
    for h in fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY).hdus():
        assert h["data_offset"] % 2880 == 0


def test_preview_matches_the_source_pixels(s3_with_fits, fits_blob):
    from astropy.io import fits
    import io as _io

    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY)
    arr, _hdr = rf.read_preview_array(size=64)
    truth = fits.open(_io.BytesIO(fits_blob))[0].data
    assert arr.shape == (64, 48)
    # full resolution requested, so the preview is the image itself
    np.testing.assert_allclose(arr, truth, rtol=1e-5)


def test_preview_selects_a_named_hdu(s3_with_fits):
    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY)
    a, _ = rf.read_preview_array(size=32, index=0)
    b, _ = rf.read_preview_array(size=32, index=1)
    assert not np.allclose(a, b)


def test_small_files_take_the_single_read_path(s3_with_fits):
    """Below SMALL_FILE one GET beats many ranged reads, so we do that."""
    s3_with_fits.reads.clear()
    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY)
    rf.read_preview_array(size=16)
    # One header probe per HDU plus a single contiguous data read -- crucially,
    # not one read per sampled row.
    assert len(s3_with_fits.reads) <= len(rf.hdus()) + 2
    biggest = max(end - start + 1 for _k, start, end in s3_with_fits.reads)
    assert biggest >= 64 * 48 * 4  # the whole image came back in one read


def test_large_files_read_far_less_than_the_whole_object(s3_with_fits, monkeypatch):
    """The thesis of the program, asserted.

    SMALL_FILE is lowered rather than synthesising a >16 MB file, so the strided
    path is exercised cheaply. Sampling 32 of 512 rows must cost about a
    sixteenth of the data, not all of it.
    """
    from tests import synth

    # Big enough that the fixed-size header probe is not itself a large
    # fraction of the object, which would make the ratio below meaningless.
    blob = synth.fits_bytes(height=2048, width=512, n_extra=0)
    s3_with_fits.put(BUCKET, "big.fits", blob)
    monkeypatch.setattr(fitsview, "SMALL_FILE", 1024)
    s3_with_fits.reads.clear()

    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, "big.fits")
    arr, _ = rf.read_preview_array(size=32)

    assert arr.shape == (32, 32)
    assert s3_with_fits.bytes_read < len(blob) / 8
    # one read per sampled row: the ranges stayed sparse, as intended
    assert len(s3_with_fits.reads) >= 32


def test_strided_preview_agrees_with_the_contiguous_one(s3_with_fits, monkeypatch):
    """Both code paths must produce identical pixels for the same request."""
    from tests import synth

    blob = synth.fits_bytes(height=128, width=64, n_extra=0)
    s3_with_fits.put(BUCKET, "cmp.fits", blob)

    contiguous, _ = fitsview.RemoteFITS(
        s3_with_fits, BUCKET, "cmp.fits").read_preview_array(size=32)
    monkeypatch.setattr(fitsview, "SMALL_FILE", 1024)
    strided, _ = fitsview.RemoteFITS(
        s3_with_fits, BUCKET, "cmp.fits").read_preview_array(size=32)

    np.testing.assert_allclose(contiguous, strided, rtol=1e-6)


def test_missing_hdu_raises(s3_with_fits):
    rf = fitsview.RemoteFITS(s3_with_fits, BUCKET, KEY)
    with pytest.raises(fitsview.FitsError):
        rf.image_hdu(99)
