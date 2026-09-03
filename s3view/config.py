"""User configuration: bookmarks, defaults, cache locations."""

import json
import os
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


def parse_uri(uri):
    """'s3://bucket/some/prefix/' -> ('bucket', 'some/prefix/')."""
    if uri.startswith("s3://"):
        uri = uri[5:]
    uri = uri.lstrip("/")
    bucket, _, prefix = uri.partition("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix
