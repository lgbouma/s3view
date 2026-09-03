"""ASDF: tree parsing, block addressing, strided reads."""

import numpy as np
import pytest

from s3view import asdfview
from tests import synth
from tests.conftest import BUCKET

KEY = "dir/frame.asdf"


def test_tree_lists_named_arrays(s3_with_asdf):
    s3, _data, _err = s3_with_asdf
    arrays = asdfview.RemoteASDF(s3, BUCKET, KEY).arrays()
    assert [a["path"] for a in arrays] == ["roman.data", "roman.err"]
    assert arrays[0]["shape"] == [64, 48]
    assert arrays[0]["datatype"] == "float32"
    assert arrays[0]["byteorder"] == "little"


def test_tree_text_is_the_raw_yaml(s3_with_asdf):
    s3, _d, _e = s3_with_asdf
    text = asdfview.RemoteASDF(s3, BUCKET, KEY).tree_text()
    assert text.startswith("#ASDF 1.0.0")
    assert "instrument: SYNTH" in text


def test_block_offsets_come_from_the_trailing_index(s3_with_asdf):
    s3, _d, _e = s3_with_asdf
    ra = asdfview.RemoteASDF(s3, BUCKET, KEY)
    offsets = ra.block_offsets()
    assert len(offsets) == 2
    blob = s3.objects[BUCKET][KEY]
    for off in offsets:
        assert blob[off : off + 4] == b"\xd3BLK"


def test_block_walk_fallback_matches_the_index():
    """A file with no trailing index must still be addressable."""
    with_index, _d, _e = synth.asdf_bytes(with_index=True)
    without, _d2, _e2 = synth.asdf_bytes(with_index=False)
    assert b"#ASDF BLOCK INDEX" not in without

    s3 = synth.FakeS3()
    s3.put(BUCKET, "a.asdf", with_index)
    s3.put(BUCKET, "b.asdf", without)

    indexed = asdfview.RemoteASDF(s3, BUCKET, "a.asdf").block_offsets()
    walked = asdfview.RemoteASDF(s3, BUCKET, "b.asdf").block_offsets()
    assert indexed == walked


def test_block_header_is_parsed(s3_with_asdf):
    s3, data, _err = s3_with_asdf
    blk = asdfview.RemoteASDF(s3, BUCKET, KEY).block_header(0)
    assert blk["compression"] == ""
    assert blk["used"] == data.nbytes
    # magic(4) + header_size(2) + 48-byte header
    assert blk["data_offset"] == blk["start"] + 54


def test_preview_reproduces_the_source_array(s3_with_asdf):
    s3, data, _err = s3_with_asdf
    arr, meta = asdfview.RemoteASDF(s3, BUCKET, KEY).read_preview_array(size=64)
    assert meta["path"] == "roman.data"
    np.testing.assert_allclose(arr, data, rtol=1e-5)


def test_preview_can_select_a_named_array(s3_with_asdf):
    s3, _data, err = s3_with_asdf
    arr, meta = asdfview.RemoteASDF(s3, BUCKET, KEY).read_preview_array(
        size=64, sel="roman.err")
    assert meta["path"] == "roman.err"
    np.testing.assert_allclose(arr, err, rtol=1e-5)


def test_auto_selection_prefers_the_largest_array(s3_with_asdf):
    s3, _d, _e = s3_with_asdf
    assert asdfview.RemoteASDF(s3, BUCKET, KEY).image_array()["path"] == "roman.data"


def test_unknown_array_raises(s3_with_asdf):
    s3, _d, _e = s3_with_asdf
    with pytest.raises(asdfview.AsdfError):
        asdfview.RemoteASDF(s3, BUCKET, KEY).read_preview_array(sel="roman.nope")


def test_little_endian_is_honoured(s3_with_asdf):
    """ASDF arrays are little-endian; FITS is big-endian. Mixing them up
    produces plausible-looking garbage rather than an error, so assert it."""
    s3, data, _err = s3_with_asdf
    arr, _ = asdfview.RemoteASDF(s3, BUCKET, KEY).read_preview_array(size=64)
    assert abs(float(arr.mean()) - float(data.mean())) < 1.0


def test_preview_reads_only_the_sampled_rows(s3_with_asdf):
    """One ranged read per sampled row, and no more."""
    s3, _d, _e = s3_with_asdf
    s3.reads.clear()
    asdfview.RemoteASDF(s3, BUCKET, KEY).read_preview_array(size=8)
    row_reads = [r for r in s3.reads if r[2] - r[1] + 1 == 48 * 4]
    assert len(row_reads) == 8


def test_large_file_reads_a_small_fraction_of_the_object():
    """The ratio only means anything once the object dwarfs the fixed-size
    head and tail metadata probes, as a real 200 MB frame does."""
    blob, _data, _err = synth.asdf_bytes(height=2048, width=512)
    s3 = synth.FakeS3()
    s3.put(BUCKET, "big.asdf", blob)
    s3.reads.clear()

    arr, _meta = asdfview.RemoteASDF(s3, BUCKET, "big.asdf").read_preview_array(size=32)
    assert arr.shape == (32, 32)
    assert s3.bytes_read < len(blob) / 8


def test_not_an_asdf_file_raises():
    s3 = synth.FakeS3()
    s3.put(BUCKET, "x.asdf", b"this is not asdf at all" * 100)
    with pytest.raises(asdfview.AsdfError):
        asdfview.RemoteASDF(s3, BUCKET, "x.asdf").tree()
