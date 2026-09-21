# s3view

[![PyPI](https://img.shields.io/pypi/v/s3view)](https://pypi.org/project/s3view/)
[![tests](https://github.com/lgbouma/s3view/actions/workflows/ci.yml/badge.svg)](https://github.com/lgbouma/s3view/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A fast, lightweight browser for astronomy data, wherever it sits: an S3
bucket, a directory on a machine you can `ssh` into, or a local path. It
behaves like a file manager — click through prefixes, preview things, hit
space bar — but it **streams instead of downloading**.

Compatible with: FITS, ASDF, movies, standard image formats, PDFs, standard
text formats.

Run from command line:
```
s3view                                    # open your default location
s3view s3://bucket/prefix/                # an S3 prefix
s3view you@host:/data/movies/             # a directory on another machine
s3view /local/pipeline/products/          # a directory on this one
s3view --set-start s3://bucket/prefix/    # remember that as the default
```

It starts a local server, opens your browser and prints a URL. `Ctrl-C` quits.
On first run, with nothing configured, it lists your buckets and lets you pick.

![s3view browsing a night of pipeline products: a 191 MB FITS frame previewed from ~8 MB of ranged reads, an ASDF array, gallery thumbnails, and a 900-object prefix](https://raw.githubusercontent.com/lgbouma/s3view/main/docs/demo.gif)

## Installation

```bash
uv tool install 's3view[all]'
```

Python 3.10+. `pipx install` and `pip install` take the same argument, and
`uvx 's3view[all]' s3://bucket/prefix/` runs it without installing anything.
`[all]` adds the preview stack (astropy, numpy, Pillow, matplotlib, PyYAML);
without it `botocore` is the only dependency and previews degrade gracefully.
An `ssh://` or local session needs no botocore and no credentials at all.

<details>
<summary>Running from a checkout instead</summary>

To develop on it, or to use a scientific stack you have already built:

```bash
git clone git@github.com:lgbouma/s3view.git /my/preferred/dir/s3view
echo 'export PATH="/my/preferred/dir/s3view/bin:$PATH"' >> ~/.bashrc   # or ~/.zshrc
```

`bin/s3view` runs under whichever `python3` is first on your `PATH`, so *that*
interpreter needs `botocore` — a per-project virtualenv or Apple's
`/usr/bin/python3` often lacks it even when your usual environment has it.
Set `S3VIEW_PYTHON=/path/to/python` to name a different one. Note that a
checkout on `PATH` shadows an installed s3view; `command -v s3view` shows which
one you are running.

</details>

### Optional dependencies

Everything below degrades gracefully, and the launch banner says which of them
are live.

| package | enables |
|---|---|
| `pillow` | image thumbnails, and previews of formats browsers cannot decode |
| `numpy` | any array preview at all |
| `astropy` | FITS previews (zscale in particular) |
| `pyyaml` | ASDF previews |
| `matplotlib` | colormaps beyond grayscale |

### Credentials and endpoints

Standard botocore resolution: environment variables, `~/.aws/credentials`,
`AWS_PROFILE`, SSO, instance roles. Override per run with `--profile` and
`--region`. Non-AWS S3-compatible stores work via `--endpoint-url` (MinIO,
Ceph, Cloudflare R2, Wasabi).

**Buckets you can read but do not own.** S3's `ListBuckets` returns only the
buckets owned by the calling account. A bucket shared with you cross-account
through a bucket policy is fully readable yet never appears in that list — a
common arrangement for shared project data. s3view therefore adds any bucket it
knows you can reach (bookmarked, or the one you are in) to the sidebar and marks
it with a `·`. To reach one for the first time, either pass it on the command
line or press `⌘L` and type the `s3://` path, then bookmark it with ☆. That address bar takes any of the three location forms, so it is also how you jump to an `ssh://` path mid-session.

## What it previews

| type | how |
|---|---|
| **FITS** (`.fits`, `.fit`, `.fts`, `.fz`) | strided ranged reads; HDU picker, stretch, colormap, resolution, full header text |
| **ASDF** (`.asdf`) | same, driven by the YAML tree and block index; pick any named array (`roman.data`, `roman.err`, `roman.dq`, …) and read the tree |
| mp4 / mov / webm / m4v | streamed from S3 by range request, with a running "~X MB transferred" readout |
| png / jpg / gif / webp / tif | presigned direct load; server-side thumbnail for formats the browser cannot decode |
| txt / json / yaml / cfg / param / log / py / csv | first 256 KB via one ranged read |
| pdf | presigned, in an iframe |
| anything else | metadata plus a download link |

Array previews share one interface: choose the HDU or array, a stretch
(`zscale`, `asinh`, `log`, `99.5%`, `minmax`), a colormap, and a resolution from
256 to 1024 px. The footer always reports what it actually read — e.g.
`2.1 s · read ~8.4 MB of 191 MB (4.2%)` — so the cost is never hidden from you.

Containers the browser cannot decode (mkv, avi) offer **Open in player**, which
hands the presigned URL to IINA or VLC — still streaming, never downloading.

## Remote paths over SSH

```bash
s3view you@host:/data/survey/20260906/movies/
s3view ssh://you@host/~/reductions/      # ~ expands on the remote host
s3view --ssh-option=-oProxyJump=bastion you@host:/data/
```

`ssh://user@host/path/` and `user@host:/path/` are synonyms. Nothing is
installed on the far side: s3view sends a small helper script down the
connection and talks to it over stdin/stdout, so the remote needs only a
`python3` on its `PATH` (`ssh_python` in the config names a specific one).

Connections are ordinary `ssh` subprocesses, so `~/.ssh/config` governs them
exactly as it would a bare `ssh` — aliases, keys, jump hosts, 2FA. Set up
key-based auth first; s3view enables `ControlMaster`, so a session then costs
one authentication. `--ssh-option` passes anything else through, glued into one
word as above.

**What differs from S3.** There is nothing to presign over SSH, so media is
served through s3view's own ranged proxy on `127.0.0.1` rather than fetched by
the browser directly. Reads stay as sparse as ever — the helper answers a whole
list of byte ranges in one round trip — so a FITS preview or a scrub to the end
of a movie is cheap at any file size:

| against a remote host over a ~4 MB/s link | result |
|---|---|
| list a directory (17 files, 3.2 GB) | 119 ms |
| 64 KB from the start / middle / end of a 731 MB mp4 | 68 / 25 / 29 ms |
| sequential throughput | 3.7 MB/s, 91% of `ssh … dd \| wc -c` |

Your SSH link is the ceiling, though: continuous playback needs the movie's
bitrate to fit in it, and an 88 Mbit/s movie will buffer on a 4 MB/s link no
matter what serves it.

Read-only, like the rest of s3view: it never writes, moves or deletes anything
on the remote host.

## Local paths

An absolute path or `~` opens the local filesystem, which is also the way to
browse an NFS or `sshfs` mount without using the SSH transport at all:

```bash
s3view /data/pipeline/night1/
```

## Configuration

`~/.config/s3view/config.json`, written on first run. Set `start` to open
somewhere by default:

```json
{
  "start": "s3://your-bucket/your/prefix/",
  "bookmarks": [
    {"name": "your-prefix", "uri": "s3://your-bucket/your/prefix/"},
    {"name": "movies", "uri": "ssh://you@host/data/survey/movies/"}
  ],
  "profile": null,
  "region": null,
  "endpoint_url": null,
  "page_size": 1000,
  "presign_expires": 3600,
  "external_player": "IINA",
  "ssh_options": [],
  "ssh_python": null,
  "ssh_connections": 4
}
```

`start` and every bookmark accept any of the three location forms; leave `start`
`null` for the bucket picker. `s3view --set-start <location>` writes it for you,
and ☆ manages bookmarks, so an `ssh://` path you visit once is one click away
afterwards. No bucket or host names are baked into the source.

## Security

The server binds `127.0.0.1` only, and every API call requires a token generated
fresh at startup and carried in the URL it opens. Without this, any web page you
happened to have open could quietly read your buckets through localhost.
Requests arriving with a foreign `Origin` header are rejected.

Presigned URLs default to one-hour expiry and are minted only for objects you
actually open. **Copy URL** puts one on your clipboard deliberately — treat it
as a password for that object until it expires. For `ssh://` and `file://`
there is nothing to presign, so the same buttons hand back a
`127.0.0.1/api/object` URL carrying this run's token: exactly as sensitive
while s3view is running, and dead the moment it exits.

## Development

```bash
pip install -e ".[test]"
pytest
```

The suite needs **no AWS credentials and no network**. It builds synthetic FITS
and ASDF files in memory and serves them through a fake S3 that records every
ranged read, so the tests can assert on *how much* was fetched — the property
the whole program exists to protect. Both the contiguous and strided read paths
are exercised and checked against each other for identical pixels.

`ssh://` is tested with the real transport — the real subprocess, the real
line-and-payload protocol, the real connection pool — and only `ssh` itself
taken out of the argv, so no host, network or keys are needed to cover
everything between the caller and the far end. Both new backends run against
one shared contract test, because there is only one contract: satisfy it and
the FITS reader, the thumbnailer and the HTTP API work unmodified.

CI runs on Python 3.10–3.13 on Linux plus macOS, and a separate job installs
*only* botocore to prove the optional dependencies really do degrade gracefully
rather than crashing.

### Releasing

Version lives in one place, `s3view/__init__.py`; `pyproject.toml` reads it
from there. To cut a release, bump it, commit, then:

```bash
git tag v0.1.1 && git push origin v0.1.1
```

The `release` workflow builds the sdist and wheel, refuses the tag if it
disagrees with `s3view.__version__`, installs the wheel into a clean
environment and starts the CLI from it, then uploads to PyPI through [trusted
publishing](https://docs.pypi.org/trusted-publishers/) — there is no API token
anywhere in the repository or its secrets. Running the workflow by hand
(`workflow_dispatch`) does everything except the upload, which is the way to
rehearse a release: PyPI never allows a version number to be reused, even after
the file is deleted.

To re-record the README animation (needs `playwright` and `ffmpeg`, neither of
them a runtime dependency):

```bash
pip install playwright && playwright install chromium
python tools/record_demo.py            # -> docs/demo.gif
```

## Why it is fast

**On S3, video and audio never pass through this program.** The page is handed a
short-lived presigned S3 URL and the browser's own media stack range-requests it
directly, so seeking is cheap no matter how large the file. Measured from inside
the browser against a 731 MB mp4, on a ~2.4 MB/s link:

| request | result |
|---|---|
| first 64 KB | `206`, 717 ms |
| 64 KB from the **middle** | `206`, 321 ms |
| 64 KB from the **end** | `206`, 251 ms |

Over `ssh://` the range requests terminate in s3view rather than at AWS, and
cost one round trip each — 25 to 68 ms anywhere in that same 731 MB file.

**Array images are read by byte range, not downloaded.** Both FITS and ASDF are
self-describing: a small ranged read of the metadata is enough to compute the
exact byte offset of every row of every array. s3view then fetches only the rows
the preview needs, in parallel, and bins the columns it already has in memory.
For a 200 MB, 4088×4088 float32 detector frame:

| preview | bytes read | time |
|---|---|---|
| 256 px | 4.2 MB (2.2%) | ~1.3 s |
| 512 px | 8.4 MB (4.2%) | ~2.1 s |
| 1024 px | 16.7 MB (8.7%) | ~3.8 s |
| downloading it instead | 200 MB | ~90 s |

Ranged reads bypass botocore's request machinery: the object is presigned once
and every range is pulled over a pooled HTTPS connection, because signing 512
separate requests costs more CPU than the transfer costs bandwidth.

**Listings are paginated and virtualized.** A prefix of 1000 objects renders
about 60 DOM nodes; the next page is prefetched in the background while you read
the current one. A prefix holding 187 GB across 1000 files opens as fast as an
empty one.

**Thumbnails are lazy and rate-limited.** Only tiles actually on screen are
requested, three at a time, cached on disk under `~/.cache/s3view`. Array
thumbnails in gallery view cost megabytes each, so they sit behind a toggle.

## Known limitations

- Array previews sample rows rather than averaging them vertically, so a
  decimated view of a crowded field aliases. Columns *are* averaged. Raise the
  resolution to sample more rows.
- ASDF blocks compressed with `lz4` or `blosc` cannot be read by range and are
  not supported; uncompressed, `zlib` and `bzip2` blocks are.
- `GetBucketLocation` is frequently denied on cross-account buckets, so the
  region is taken from the `x-amz-bucket-region` header on HeadBucket instead.
  If both are denied, s3view falls back to your configured default region.
- Chrome will not load video in a hidden or background tab; if a movie sits on a
  spinner, bring the window to the front.
- Over `ssh://`, media crosses your SSH link instead of AWS's network, so that
  link is the ceiling for continuous playback. Seeking and previewing stay
  cheap regardless of file size; a movie whose bitrate exceeds your bandwidth
  will buffer.
- The `ssh://` transport needs `python3` on the remote `PATH`. There is no
  `dd`-only fallback, because one process per byte range would cost more than
  the transfer it saves.
- Read-only, on every transport. There is no upload, rename, or delete.
