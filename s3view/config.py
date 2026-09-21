"""User configuration: bookmarks, defaults, cache locations, and URI parsing."""

import json
import os
import re
import threading

# No bucket is baked into the source. The start location and bookmarks live in
# the user's own config file, written on first run and editable from the UI.
DEFAULT_BOOKMARK = None

CONFIG_DIR = os.path.expanduser(os.environ.get("S3VIEW_CONFIG_DIR", "~/.config/s3view"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.expanduser(os.environ.get("S3VIEW_CACHE_DIR", "~/.cache/s3view"))
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")

DEFAULTS = {
    "start": DEFAULT_BOOKMARK,
    "bookmarks": [],
    "profile": None,
    "region": None,
    "endpoint_url": None,
    "page_size": 1000,
    "presign_expires": 3600,
    "external_player": "IINA",
    # ssh:// transport. Everything else about the connection -- hostnames,
    # keys, jump hosts -- comes from the user's own ~/.ssh/config.
    "ssh_options": [],
    "ssh_python": None,
    "ssh_connections": 4,
}

_lock = threading.Lock()


def _ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)


def load():
    """Load config, creating it with defaults on first run."""
    _ensure_dirs()
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        save(cfg)
    except (OSError, ValueError):
        pass  # corrupt config: fall back to defaults rather than refusing to start
    return cfg


def save(cfg):
    _ensure_dirs()
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return cfg


# -- locations -----------------------------------------------------------
#
# One (root, key) namespace covers every transport. The root is called
# ``bucket`` everywhere above this layer, because that is what it was first: a
# bare name is an S3 bucket, ``ssh://user@host`` is a directory tree on another
# machine, ``file://`` is this one. The key is always slash-separated and never
# begins with a slash, so the filesystem backends prepend one and S3 uses it
# verbatim.

SCHEMES = ("s3", "ssh", "file")

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*)://(.*)$", re.I)
# scp syntax -- "luke@wh3:/ar0/data" -- because that is what people type.
_SCP_RE = re.compile(r"^([^\s/@:]+@[^\s/@:]+):([/~].*)$")


def scheme_of(root):
    """Which transport owns this root. A bare bucket name means S3."""
    m = _SCHEME_RE.match(root or "")
    return m.group(1).lower() if m else "s3"


def normalize_uri(uri):
    """Accept what a person would type; return a canonical scheme:// URI.

    ``luke@wh3:/ar0/data`` and ``/local/dir`` are both perfectly clear about
    where they point, so they are not worth refusing over a missing prefix.
    A bare name stays an S3 bucket, which is what it has always meant here.
    """
    uri = (uri or "").strip()
    if not uri:
        return uri
    scp = _SCP_RE.match(uri)
    if scp:
        return "ssh://%s/%s" % (scp.group(1), scp.group(2).lstrip("/"))
    if _SCHEME_RE.match(uri):
        return uri
    if uri.startswith("/") or uri.startswith("~"):
        return "file://" + os.path.abspath(os.path.expanduser(uri))
    return "s3://" + uri


def parse_uri(uri):
    """URI -> (root, key prefix).

    The prefix never starts with a slash and, when non-empty, always ends with
    one -- the same shape S3 wants, so nothing above this cares which
    transport it came from.
    """
    uri = normalize_uri(uri)
    m = _SCHEME_RE.match(uri)
    scheme, rest = (m.group(1).lower(), m.group(2)) if m else ("s3", uri)
    if scheme == "file":
        root, prefix = "file://", rest
    else:
        head, _, prefix = rest.lstrip("/").partition("/") if scheme == "s3" \
            else rest.partition("/")
        root = head if scheme == "s3" else "%s://%s" % (scheme, head)
    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return root, prefix


def format_uri(root, prefix=""):
    """(root, prefix) -> URI. The inverse of parse_uri."""
    if not root:
        return ""
    base = root if _SCHEME_RE.match(root) else "s3://" + root
    return base + "/" + (prefix or "")


def path_of(key):
    """A key as an absolute path on whichever machine holds it.

    Keys are root-relative, so the leading slash the filesystem needs is added
    here rather than carried around in every listing. A leading ``~`` is left
    alone: it is expanded by the side that owns the home directory.
    """
    key = key or ""
    if key.startswith("~"):
        return key
    return "/" + key.lstrip("/")
