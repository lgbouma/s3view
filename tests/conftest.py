"""Keep tests out of the real config and cache directories.

These must be set before s3view.config is imported, because it resolves its
paths at import time; pytest loads conftest first, so this is the right place.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="s3view-tests-")
os.environ["S3VIEW_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["S3VIEW_CACHE_DIR"] = os.path.join(_TMP, "cache")

import pytest  # noqa: E402

from tests import synth  # noqa: E402

BUCKET = "test-bucket"


@pytest.fixture
def fits_blob():
    return synth.fits_bytes()


@pytest.fixture
def s3_with_fits(fits_blob):
    s3 = synth.FakeS3()
    s3.put(BUCKET, "dir/image.fits", fits_blob)
    return s3


@pytest.fixture
def s3_with_asdf():
    blob, data, err = synth.asdf_bytes()
    s3 = synth.FakeS3()
    s3.put(BUCKET, "dir/frame.asdf", blob)
    return s3, data, err
