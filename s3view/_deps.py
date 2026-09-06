"""The one hard dependency, checked with an explanation instead of a traceback.

Optional dependencies degrade quietly and report themselves in the startup
banner. botocore cannot degrade -- there is no browsing without it -- so its
absence gets a message. The usual cause is a shadowed interpreter: s3view run
from a checkout picks up whichever python3 is first on PATH, which is often a
project virtualenv rather than the environment that has botocore.
"""

import sys

_MISSING = """\
s3view needs botocore, and the interpreter running it does not have it:

    {exe}

Any one of these fixes it:

  * Install s3view as a self-contained tool. This is the recommended route:
    it brings its own dependencies and cannot be shadowed.

        uv tool install 's3view[all]'

  * Add botocore to the interpreter above.

        {exe} -m pip install botocore

  * If you run s3view from a checkout, point it at an interpreter that
    already has botocore.

        export S3VIEW_PYTHON=/path/to/python
"""


def require_botocore():
    """Exit with an explanation if botocore is not importable."""
    try:
        import botocore  # noqa: F401
    except ImportError:
        sys.stderr.write(_MISSING.format(exe=sys.executable))
        raise SystemExit(1)
