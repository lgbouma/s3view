"""Command line entry point."""

import argparse
import socket
import sys
import threading
import webbrowser

from s3view import __version__, config
from s3view._deps import require_botocore


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
    p.add_argument("uri", nargs="?",
                   help="location to open (default: configured start). "
                        "s3://bucket/prefix/, ssh://user@host/path/, "
                        "user@host:/path/, or a local directory")
    p.add_argument("-p", "--port", type=int, default=0, help="port (default: an unused one)")
    p.add_argument("--profile", help="AWS profile")
    p.add_argument("--region", help="AWS region")
    p.add_argument("--endpoint-url", help="custom S3 endpoint (MinIO, R2, Ceph, ...)")
    p.add_argument("--page-size", type=int, help="objects fetched per listing page")
    p.add_argument("--ssh-option", action="append", metavar="OPT", dest="ssh_options",
                   help="extra argument passed to ssh, repeatable; glue the value on "
                        "so it stays one word, e.g. --ssh-option=-oProxyJump=bastion")
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
        ("ssh_options", args.ssh_options),
    ):
        if val:
            cfg[key] = val
    if args.uri:
        cfg["start"] = config.normalize_uri(args.uri)
        if args.set_start:
            saved = config.load()
            saved["start"] = cfg["start"]
            config.save(saved)
            print("default start location saved: %s" % cfg["start"])

    # After parsing, so that --help and --version answer even in an environment
    # that cannot run the server -- and only when S3 is actually where we are
    # going, since an ssh:// or file:// session needs neither botocore nor
    # credentials. Server is imported here for the same reason.
    start = cfg.get("start")
    if not start or config.scheme_of(config.parse_uri(start)[0]) == "s3":
        require_botocore()
    from s3view.server import Server

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
