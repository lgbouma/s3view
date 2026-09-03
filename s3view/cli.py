"""Command line entry point."""

import argparse
import socket
import sys
import threading
import webbrowser

from s3view import __version__, config
from s3view.server import Server


def _port_free(port):
    """Can the server bind this port? Mirrors the server's own socket options.

    SO_REUSEADDR matters: the server sets allow_reuse_address, so a port left in
    TIME_WAIT by a previous run is usable. A probe without it reports false
    positives and would send us wandering off to a random port.
    """
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="s3view",
        description="Fast, lightweight S3 browser: streams video, previews FITS, "
                    "never downloads what it does not need.",
    )
    p.add_argument("uri", nargs="?", help="s3://bucket/prefix/ to open (default: configured start)")
    p.add_argument("-p", "--port", type=int, default=0, help="port (default: an unused one)")
    p.add_argument("--profile", help="AWS profile")
    p.add_argument("--region", help="AWS region")
    p.add_argument("--endpoint-url", help="custom S3 endpoint (MinIO, R2, Ceph, ...)")
    p.add_argument("--page-size", type=int, help="objects fetched per listing page")
    p.add_argument("-n", "--no-open", action="store_true", help="do not open a browser")
    p.add_argument("-v", "--verbose", action="store_true", help="log requests")
    p.add_argument("--set-start", action="store_true", help="save URI as the default start location")
    p.add_argument("--version", action="version", version="s3view " + __version__)
    args = p.parse_args(argv)

    cfg = config.load()
    for key, val in (
        ("profile", args.profile),
        ("region", args.region),
        ("endpoint_url", args.endpoint_url),
        ("page_size", args.page_size),
    ):
        if val:
            cfg[key] = val
    if args.uri:
        cfg["start"] = args.uri if args.uri.startswith("s3://") else "s3://" + args.uri
        if args.set_start:
            saved = config.load()
            saved["start"] = cfg["start"]
            config.save(saved)
            print("default start location saved: %s" % cfg["start"])

    if args.port:
        if not _port_free(args.port):
            print("port %d is already in use" % args.port, file=sys.stderr)
            return 1
        port = args.port
    else:
        port = _free_port()
    try:
        server = Server(("127.0.0.1", port), cfg, verbose=args.verbose)
    except OSError as exc:
        print("could not bind 127.0.0.1:%d: %s" % (port, exc), file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%d/?t=%s" % (port, server.token)
    caps = server.capabilities
    # flush explicitly: stdout is block-buffered when redirected to a file or
    # pipe, which would hide the URL (and its token) until the server exits.
    where = cfg.get("start") or "(no default set - pick a bucket in the browser)"
    print("s3view %s  ->  %s" % (__version__, where))
    print("   %s" % url)
    onoff = lambda k: "on" if caps.get(k) else "off"  # noqa: E731
    print("   thumbnails:%s  fits:%s  asdf:%s   (ctrl-c to quit)"
          % (onoff("images"), onoff("fits"), onoff("asdf")), flush=True)

    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.shutdown()
        server.server_close()
    return 0
