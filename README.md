# s3view

[![tests](https://github.com/lgbouma/s3view/actions/workflows/ci.yml/badge.svg)](https://github.com/lgbouma/s3view/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A fast, lightweight S3 browser for scientific data. It behaves like a file
manager — click through prefixes, preview things, hit space bar — but it
**streams instead of downloading**.

That distinction is the whole point. Most GUI S3 clients pull an entire object
before they will show you anything, which makes them unusable for the sort of
data that piles up in object storage: 200 MB detector frames, multi-gigabyte
movies, prefixes with tens of thousands of files. s3view reads only the bytes it
needs, using HTTP range requests.

For astronomers specifically, it gives you **quicklooks of FITS and ASDF images
sitting in S3 without downloading them** — a 200 MB frame renders in a couple of
seconds off a few MB of scattered reads. It is the missing "just let me look at
it" step between `aws s3 ls` and pulling data down to disk.

```
s3view                          # open your default location
s3view s3://bucket/prefix/      # open somewhere specific
s3view --set-start s3://bucket/prefix/    # remember that as the default
```

It starts a local server, opens your browser and prints a URL. `Ctrl-C` quits.
On first run, with nothing configured, it lists your buckets and lets you pick.

## Installation

You almost certainly already have the only hard requirement: **Python 3.9+ and
`botocore`**, which ships with the AWS CLI. No boto3, no web framework, no build
step, no `npm install`.

**Recommended — clone and put it on your PATH.** This runs under whichever
`python3` you normally use, so it picks up the scientific stack you already have
installed:

```bash
git clone git@github.com:lgbouma/s3view.git ~/src/s3view
echo 'export PATH="$HOME/src/s3view/bin:$PATH"' >> ~/.bashrc   # or ~/.zshrc
exec $SHELL
```

**Or symlink it** into a directory already on your PATH:

```bash
ln -s ~/src/s3view/bin/s3view ~/.local/bin/s3view
```

**Or install it as a package** into your existing environment, which gives you
an `s3view` console script:

```bash
pip install -e ~/src/s3view
```

A note on isolated installers: `pipx install` and `uv tool install` put s3view in
its own virtualenv, where it *cannot see* the astropy and Pillow in your normal
environment, so FITS/ASDF and thumbnails silently switch off. If you want that
isolation, ask for the extras explicitly:

```bash
pipx install '~/src/s3view[all]'
```

### Optional dependencies

Everything below degrades gracefully — s3view starts and tells you at launch
which features are live (`thumbnails:on  fits:on`).

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
line or press `⌘L` and type the `s3://` path, then bookmark it with ☆.

## Why it is fast

**Video and audio never pass through this program.** The page is handed a
short-lived presigned S3 URL and the browser's own media stack range-requests it
directly, so seeking is cheap no matter how large the file. Measured from inside
the browser against a 731 MB mp4, on a ~2.4 MB/s link:

| request | result |
|---|---|
| first 64 KB | `206`, 717 ms |
| 64 KB from the **middle** | `206`, 321 ms |
| 64 KB from the **end** | `206`, 251 ms |

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

## The path bar

The breadcrumb doubles as an address bar. Click the empty strip to the right of
it (or press `⌘L`) and it becomes a selectable text field containing the full
`s3://bucket/prefix/` URI, already selected — drag-select and `⌘C`, or just
`⌘C` straight away. Paste a different `s3://…` path and press Enter to jump
there. The `⧉` button next to it copies the current path in one click, and
`⌘⇧C` does the same from the keyboard.

## Keyboard

| key | action |
|---|---|
| `↑` `↓` | move selection |
| `Enter` / `→` | open folder, or preview file |
| `←` / `⌘↑` | parent folder |
| `Space` | preview (and play/pause inside a video) |
| `←` `→` in preview | previous / next file |
| `Esc` | close preview |
| `/` | focus the filter box |
| `⌘L` | edit / select the current s3:// path |
| `⌘⇧C` | copy the current s3:// path |
| `⌘R` | reload the listing |

Double-click opens, like a file manager. The filter box narrows what is already
loaded; **search…** runs a recursive server-side scan of the current prefix
under a key and time budget.

## Configuration

`~/.config/s3view/config.json`, written on first run:

```json
{
  "start": null,
  "bookmarks": [],
  "profile": null,
  "region": null,
  "endpoint_url": null,
  "page_size": 1000,
  "presign_expires": 3600,
  "external_player": "IINA"
}
```

`start` is where s3view opens; leave it `null` to get the bucket picker. The ☆
button bookmarks the current prefix, and `--set-start` writes the default. No
bucket names are baked into the source.

## Security

The server binds `127.0.0.1` only, and every API call requires a token generated
fresh at startup and carried in the URL it opens. Without this, any web page you
happened to have open could quietly read your buckets through localhost.
Requests arriving with a foreign `Origin` header are rejected.

Presigned URLs default to one-hour expiry and are minted only for objects you
actually open. **Copy URL** puts one on your clipboard deliberately — treat it
as a password for that object until it expires.

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

CI runs on Python 3.10–3.13 on Linux plus macOS, and a separate job installs
*only* botocore to prove the optional dependencies really do degrade gracefully
rather than crashing.

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
- Read-only. There is no upload, rename, or delete.
