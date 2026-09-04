#!/usr/bin/env python3
"""Record docs/demo.gif -- the real UI, driven by a real browser.

Nothing here is a mockup: the animation is Chromium talking to the actual
s3view server, running the actual JavaScript, rendering FITS and ASDF through
the same strided byte-range machinery the tests exercise. Only the *data* is
fabricated, so the recording can be reproduced by anyone with no credentials,
no network, and no proprietary imagery.

Two consequences of that, worth knowing before you trust a number on screen:

* Timings are honest but flattering -- the "s3" is in memory, so a read that
  takes 2 s over the wire takes milliseconds here. The recording throttles the
  fake store to a plausible bandwidth so the readouts stay believable.
* Files under raw/ are listed with plausible sizes but hold no bytes. They
  exist to show the listing handling a real 900-object prefix, and the demo
  never opens one.

    python -m pip install playwright && python -m playwright install chromium
    python tools/record_demo.py            # -> docs/demo.gif
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Must precede any s3view import: config resolves its paths once, at import
# time, and a demo has no business touching the user's bookmarks or thumb cache.
_TMP = tempfile.mkdtemp(prefix="s3view-demo-")
os.environ["S3VIEW_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["S3VIEW_CACHE_DIR"] = os.path.join(_TMP, "cache")

import numpy as np  # noqa: E402

from s3view import config  # noqa: E402
from s3view.server import Server  # noqa: E402
from tests import synth  # noqa: E402

VIEW = {"width": 1280, "height": 720}
BUCKET = "survey-pipeline-data"
NIGHT = "2026-03-12/"
OUT_GIF = os.path.join(ROOT, "docs", "demo.gif")

# Bytes per second the fake store pretends to be limited to. An in-memory "S3"
# answers instantly, which would pair every honest byte count in the footer
# with an impossible time; this holds the readouts to something a network could
# have done. 12 MB/s is a good in-region link -- slower than the truth, faster
# than the home link the README's own measurements were taken over, and chosen
# so the demo does not spend seconds of its runtime on a spinner.
BANDWIDTH = 12e6


# --------------------------------------------------------------- synthetic sky
def word_points(h, w, word, sigma, fill=0.3, seed=0):
    """Positions along the strokes of `word`, laid across the frame.

    The demo's sky has an asterism in it. Rendering the glyphs to a mask and
    sampling the lit pixels puts stars *on* the letterforms, which survives the
    decimation a preview does far better than drawing strokes would.

    How many stars that takes depends on both the size of the lettering and the
    size of a star, so the count is set by what fraction of the strokes the
    stamps cover: too sparse and the word is a guess, too dense and the glyphs
    fill in and stop looking like stars.
    """
    from PIL import Image, ImageDraw, ImageFont
    from matplotlib import font_manager

    path = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans",
                                                             weight="bold"))
    probe = ImageFont.truetype(path, 100)
    box = probe.getbbox(word)
    size = int(100 * (0.62 * w) / max(1, box[2] - box[0]))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((w // 2, h // 2), word, fill=255, anchor="mm",
                              font=ImageFont.truetype(path, size))
    ys, xs = np.nonzero(np.asarray(mask) > 128)
    if not len(ys):
        return np.empty(0, int), np.empty(0, int)
    n = int(np.clip(fill * len(ys) / (2 * np.pi * sigma * sigma), 200, 6000))
    pick = np.random.default_rng(seed).choice(len(ys), size=min(n, len(ys)), replace=False)
    # Row 0 of a FITS array is the *bottom* of the displayed image, and both the
    # app and the QA figures honour that, so the mask has to be flipped to come
    # out the right way up on screen.
    return (h - 1 - ys[pick]), xs[pick]


def starfield(h, w, seed=7, density=7e-5, word="s3view"):
    """A plausible detector frame: noise, a smooth background, stars, galaxies.

    Built by stamping Gaussians rather than convolving, so a 4088x4088 frame
    costs a fraction of a second and one array's worth of memory.
    """
    rng = np.random.default_rng(seed)
    img = rng.standard_normal((h, w), dtype=np.float32) * 4.0 + 120.0
    gy = np.linspace(-1, 1, h, dtype=np.float32)[:, None]
    gx = np.linspace(-1, 1, w, dtype=np.float32)[None, :]
    img += 22.0 * (1.0 - 0.55 * gy * gy - 0.35 * gx * gx)

    def stamp(y, x, flux, sigma, r):
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        x0, x1 = max(0, x - r), min(w, x + r + 1)
        dy = np.arange(y0 - y, y1 - y, dtype=np.float32)[:, None]
        dx = np.arange(x0 - x, x1 - x, dtype=np.float32)[None, :]
        img[y0:y1, x0:x1] += flux * np.exp(-(dy * dy + dx * dx) / (2 * sigma * sigma))

    n = int(h * w * density)
    ys = rng.integers(0, h, n)
    xs = rng.integers(0, w, n)
    flux = 10 ** rng.uniform(1.5, 4.0, n)
    sig = rng.uniform(0.9, 1.4, n)
    for y, x, f, s in zip(ys, xs, flux, sig):
        stamp(int(y), int(x), float(f), float(s), 7)

    for _ in range(max(3, n // 90)):                      # a few resolved sources
        y, x = int(rng.integers(0, h)), int(rng.integers(0, w))
        stamp(y, x, float(10 ** rng.uniform(1.6, 2.6)), float(rng.uniform(6, 22)), 60)

    if word:
        # Bright enough to stand out of the field, and wide enough at detector
        # scale to survive a preview that samples every eighth row.
        sigma = float(np.clip(h / 950.0, 0.7, 5.0))
        r = max(2, int(3 * sigma))
        wy, wx = word_points(h, w, word, sigma, seed=seed)
        for y, x in zip(wy, wx):
            stamp(int(y), int(x), float(10 ** rng.uniform(3.4, 4.3)),
                  sigma * float(rng.uniform(0.85, 1.25)), r)
    return img


# ------------------------------------------------------------------ fake store
class DemoS3(synth.FakeS3):
    """FakeS3 plus the three things a *convincing* listing needs.

    Per-key sizes and dates (FakeS3 hardcodes one of each), keys that report a
    size without storing bytes, and a bandwidth cap so the readouts in the
    footer describe something a network could plausibly have done.
    """

    def __init__(self):
        super().__init__()
        self.info = {}                                   # (bucket, key) -> (size, mtime)
        self.media_base = ""

    def put(self, bucket, key, blob, mtime=0.0):
        super().put(bucket, key, blob)
        self.info[(bucket, key)] = (len(blob), mtime)

    def put_listed(self, bucket, key, size, mtime=0.0):
        """A key that exists for the listing only -- no bytes behind it."""
        super().put(bucket, key, b"")
        self.info[(bucket, key)] = (size, mtime)

    def _stat(self, bucket, key, fallback_size):
        return self.info.get((bucket, key), (fallback_size, 1.7e9))

    def _throttle(self, nbytes):
        time.sleep(nbytes / BANDWIDTH)

    # -- reads -----------------------------------------------------------
    def get_range(self, bucket, key, start, end):
        out = super().get_range(bucket, key, start, end)
        self._throttle(len(out))
        return out

    def get_ranges(self, bucket, key, ranges, workers=8):
        """Parallel ranges still share one pipe, so the cap is on the total."""
        out = [synth.FakeS3.get_range(self, bucket, key, a, b) for a, b in ranges]
        self._throttle(sum(len(c) for c in out))
        return out

    def get_object(self, bucket, key, max_bytes=None):
        blob, ctype = super().get_object(bucket, key, max_bytes)
        self._throttle(len(blob))
        return blob, ctype

    # -- metadata --------------------------------------------------------
    def head(self, bucket, key):
        h = super().head(bucket, key)
        size, mtime = self._stat(bucket, key, h["size"])
        h["size"], h["mtime"] = size, mtime
        return h

    def list_page(self, bucket, prefix="", token=None, limit=None, delimiter="/"):
        page = super().list_page(bucket, prefix, token, limit, delimiter)
        for f in page["files"]:
            f["size"], f["mtime"] = self._stat(bucket, f["key"], f["size"])
        return page

    def list_buckets(self):
        names = sorted(set(self.objects) | {"survey-raw", "tess-lightcurves", "lgb-scratch"})
        return [{"name": b, "created": 1.7e9} for b in names]

    def presign(self, bucket, key, expires=3600, disposition=None, content_type=None):
        """Point at the local stand-in for S3, so <img>/<video> really stream."""
        return "%s/%s/%s" % (self.media_base, bucket, key)


def media_server(s3):
    """The bytes side of `presign`: plain HTTP with Range, on its own port."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def handle_error(self, *a):
            pass                                      # the browser drops ranges it no longer needs

        def do_GET(self):
            bucket, _, key = self.path.lstrip("/").partition("/")
            blob = s3.objects.get(bucket, {}).get(key)
            if blob is None:
                self.send_error(404)
                return
            start, end, code = 0, len(blob) - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                lo, _, hi = rng[6:].partition("-")
                start = int(lo or 0)
                end = min(int(hi) if hi else end, end)
                code = 206
            chunk = blob[start:end + 1]
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(blob)))
            self.end_headers()
            self.wfile.write(chunk)

    class Quiet(ThreadingHTTPServer):
        def handle_error(self, *a):
            pass

    srv = Quiet(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ------------------------------------------------------------------- the data
def qa_png(img, title):
    import matplotlib
    matplotlib.use("Agg")
    import io
    import matplotlib.pyplot as plt
    from astropy.visualization import ZScaleInterval

    lo, hi = ZScaleInterval().get_limits(img)
    fig, ax = plt.subplots(figsize=(4, 4), dpi=110)
    ax.imshow(img, vmin=lo, vmax=hi, cmap="magma", origin="lower")
    ax.set_title(title, fontsize=9)
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def build_store(cache=None):
    """Build the synthetic bucket, optionally memoized -- the 191 MB frame
    takes long enough that re-shooting the demo should not rebuild it."""
    import pickle
    if cache and os.path.exists(cache):
        print("loading cached store %s ..." % cache, flush=True)
        s3 = DemoS3()
        with open(cache, "rb") as fh:
            s3.objects, s3.info = pickle.load(fh)
        return s3

    print("building synthetic data ...", flush=True)
    s3 = DemoS3()
    cal = NIGHT + "cal/"

    # A real multi-HDU frame at detector size: SCI + ERR + DQ, ~191 MB.
    synth.star_field = starfield          # the demo wants prettier sky than the tests
    big = synth.fits_bytes(4088, 4088, n_extra=2, seed=7)
    s3.put(BUCKET, cal + "f184_wfi01_cal.fits", big, 1772_400_000.0)
    del big

    s3.put(BUCKET, cal + "f158_wfi01_cal.fits",
           synth.fits_bytes(2048, 2048, n_extra=1, seed=13), 1772_401_400.0)
    asdf_blob, _d, _e = synth.asdf_bytes(2048, 2048, seed=21)
    s3.put(BUCKET, cal + "r0003201_wfi01_cal.asdf", asdf_blob, 1772_402_900.0)

    small = starfield(320, 320, seed=5)
    s3.put(BUCKET, cal + "qa_f184_wfi01.png", qa_png(small, "f184 / wfi01"), 1772_403_100.0)
    s3.put(BUCKET, cal + "qa_f158_wfi01.png",
           qa_png(starfield(320, 320, seed=6), "f158 / wfi01"), 1772_403_150.0)
    s3.put(BUCKET, cal + "cal_notes.txt",
           b"wfi01, 2026-03-12\n" + b"  flat: v14  dark: v9  gain: 1.62 e-/DN\n" * 12,
           1772_403_400.0)

    s3.put(BUCKET, NIGHT + "manifest.json",
           b'{\n  "night": "2026-03-12",\n  "detectors": 4,\n  "exposures": 900\n}\n',
           1772_404_000.0)
    s3.put(BUCKET, NIGHT + "pipeline.log",
           b"INFO  calibrating wfi01 ... ok\n" * 200, 1772_404_100.0)

    # Listing-only: a prefix big enough to show the virtualized list working.
    rng = np.random.default_rng(3)
    for i in range(900):
        s3.put_listed(BUCKET, NIGHT + "raw/r0003201001001001%03d_uncal.asdf" % i,
                      int(rng.normal(184e6, 4e6)), 1772_300_000.0 + 47 * i)
    for i in range(40):
        s3.put_listed(BUCKET, NIGHT + "catalogs/wfi01_f184_%02d.cat" % i,
                      int(rng.normal(2.1e6, 3e5)), 1772_405_000.0 + 90 * i)
    for i in range(24):
        s3.put_listed(BUCKET, NIGHT + "qa/qa_wfi%02d.pdf" % i,
                      int(rng.normal(1.4e6, 2e5)), 1772_406_000.0 + 60 * i)
    if cache:
        with open(cache, "wb") as fh:
            pickle.dump((s3.objects, s3.info), fh, protocol=4)
    return s3


# ---------------------------------------------------------------- the browser
CURSOR = r"""
(() => {
  const add = () => {
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = 'position:fixed;left:-99px;top:-99px;width:16px;height:16px;' +
      'margin:-8px 0 0 -8px;border-radius:50%;background:#141b26;' +
      'box-shadow:0 0 0 2px rgba(255,255,255,.95),0 2px 8px rgba(0,0,0,.45);' +
      'z-index:99999;pointer-events:none;transition:left .40s cubic-bezier(.33,0,.2,1),' +
      'top .40s cubic-bezier(.33,0,.2,1)';
    document.documentElement.appendChild(c);
    const ring = document.createElement('div');
    ring.style.cssText = 'position:fixed;left:-99px;top:-99px;width:16px;height:16px;' +
      'margin:-8px 0 0 -8px;border-radius:50%;border:2px solid rgba(20,27,38,.85);' +
      'opacity:0;z-index:99998;pointer-events:none';
    document.documentElement.appendChild(ring);
    window.__cur = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; };
    window.__tap = () => {
      const x = parseFloat(c.style.left), y = parseFloat(c.style.top);
      ring.style.transition = 'none';
      ring.style.left = x + 'px'; ring.style.top = y + 'px';
      ring.style.transform = 'scale(1)'; ring.style.opacity = '.95';
      requestAnimationFrame(() => {
        ring.style.transition = 'transform .45s ease-out, opacity .45s ease-out';
        ring.style.transform = 'scale(3.2)'; ring.style.opacity = '0';
      });
    };
  };
  if (document.documentElement) add(); else addEventListener('DOMContentLoaded', add);
})();
"""


class Driver:
    """Cursor choreography. Playwright's video has no pointer, so we draw one."""

    def __init__(self, page):
        self.page = page

    def pause(self, ms):
        self.page.wait_for_timeout(ms)

    def _center(self, loc):
        box = loc.bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def to(self, loc, settle=380):
        x, y = self._center(loc)
        self.page.evaluate("([x,y]) => window.__cur(x,y)", [x, y])
        self.page.mouse.move(x, y)                        # real hover styles
        self.pause(settle)
        return x, y

    def click(self, loc, settle=380, after=320):
        x, y = self.to(loc, settle)
        self.page.evaluate("window.__tap()")
        self.page.mouse.click(x, y)
        self.pause(after)

    def dblclick(self, loc, settle=420, after=380):
        x, y = self.to(loc, settle)
        self.page.evaluate("window.__tap()")
        self.page.mouse.dblclick(x, y)
        self.pause(after)

    def choose(self, loc, value, after=1500):
        """Native <select> popups never appear in the video, so set the value."""
        self.to(loc, 340)
        self.page.evaluate("window.__tap()")
        loc.select_option(value)
        self.pause(after)


def script(page, drv):
    """The shot list: browse, preview a 191 MB frame, ASDF, gallery, big prefix.

    Roughly half a minute -- long enough for each readout to be read, short
    enough that the gif stays a few megabytes.
    """
    # Match the name cell exactly: has_text is a substring test, and "cal"
    # would otherwise also hit "catalogs" and "cal_notes.txt".
    row = lambda n: page.locator('#sizer .row:has(.nm span:text-is("%s"))' % n).first  # noqa: E731
    foot_sel = lambda i: page.locator("#ovfoot select").nth(i)                         # noqa: E731

    def sharp(px):
        """Wait out the progressive pass, so the demo dwells on the real render."""
        page.wait_for_function(
            "px => { const i = document.querySelector('#ovbody img.fits');"
            "return i && i.complete && i.naturalWidth >= px; }", arg=px, timeout=60000)

    page.wait_for_selector("#sizer .row")
    drv.pause(900)

    # 1. a night of pipeline products; into the calibrated frames
    drv.dblclick(row("cal"))
    page.wait_for_selector("#sizer .row:has-text('f184')")
    drv.pause(700)

    # 2. 191 MB opens without downloading 191 MB
    drv.dblclick(row("f184_wfi01_cal.fits"))
    sharp(512)
    drv.pause(1800)                                    # long enough to read the footer

    # 3. any HDU in the file is one hop away (sel is the HDU index)
    drv.choose(foot_sel(0), "2", after=150)
    sharp(512)
    drv.pause(1200)

    # 4. colormap, then full resolution -- each one a fresh strided read
    drv.choose(foot_sel(2), "viridis", after=150)
    sharp(512)
    drv.pause(900)
    drv.choose(foot_sel(3), "1024", after=150)
    sharp(1024)
    drv.pause(1800)

    # 5. the header, in full
    drv.click(page.locator("#ovfoot button", has_text="Header"), after=1500)
    drv.click(page.locator("#ovclose"), after=600)

    # 6. same interface over ASDF: pick a named array out of the tree
    drv.dblclick(row("r0003201_wfi01_cal.asdf"))
    sharp(512)
    drv.pause(1100)
    drv.choose(foot_sel(0), "roman.err", after=200)
    sharp(512)
    drv.pause(1300)
    drv.click(page.locator("#ovclose"), after=500)

    # 7. gallery view, and the toggle that keeps array thumbnails opt-in
    drv.click(page.locator("#viewGrid"), after=1100)
    drv.click(page.locator("#fitsToggle"), after=2400)
    drv.click(page.locator("#viewList"), after=600)

    # 8. 900 objects in one prefix, scrolled
    drv.click(page.locator("#up"), after=600)
    drv.dblclick(row("raw"))
    # Wait for a row that only the new prefix has: rows from the old listing
    # are still on screen, and scrolling before the re-render lands is undone
    # by it.
    page.wait_for_selector('#sizer .row:has-text("_uncal.asdf")')
    drv.pause(600)
    for _ in range(5):
        page.mouse.wheel(0, 850)
        drv.pause(300)
    drv.pause(900)


def record(url, webm_dir):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb",
                                          "--font-render-hinting=none"])
        ctx = browser.new_context(viewport=VIEW, record_video_dir=webm_dir,
                                  record_video_size=VIEW, device_scale_factor=1)
        ctx.add_init_script(CURSOR)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        started = time.time()
        page.wait_for_selector("#sizer .row", timeout=30000)
        lead = time.time() - started                       # frames to trim off the front
        script(page, Driver(page))
        path = page.video.path()
        ctx.close()
        browser.close()
        return path, lead


def to_gif(webm, lead, out, fps=10, width=960):
    """Two passes: a palette built from the whole clip, then applied with
    rectangle diffing -- a UI recording is mostly static, and diffing is what
    keeps the file in single-digit megabytes."""
    pal = os.path.join(_TMP, "palette.png")
    trim = ["-ss", "%.2f" % max(0.0, lead - 0.35)]
    scale = "fps=%d,scale=%d:-1:flags=lanczos" % (fps, width)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", webm,
                    "-vf", scale + ",palettegen=stats_mode=diff", pal], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", webm, "-i", pal,
                    "-lavfi", scale + " [x];[x][1:v]paletteuse=dither=bayer:"
                    "bayer_scale=4:diff_mode=rectangle", "-loop", "0", out], check=True)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default=OUT_GIF, help="output gif")
    ap.add_argument("--cache", help="cache the synthetic store here between runs")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--width", type=int, default=960, help="gif width in px")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required to encode the gif")
    s3 = build_store(args.cache)
    media, base = media_server(s3)
    s3.media_base = base

    cfg = dict(config.DEFAULTS)
    cfg["start"] = "s3://%s/%s" % (BUCKET, NIGHT)
    cfg["bookmarks"] = [
        {"name": "2026-03-12", "uri": "s3://%s/%s" % (BUCKET, NIGHT)},
        {"name": "cal", "uri": "s3://%s/%scal/" % (BUCKET, NIGHT)},
        {"name": "tess-lightcurves", "uri": "s3://tess-lightcurves/"},
    ]
    srv = Server(("127.0.0.1", 0), cfg, verbose=False)
    srv.s3 = s3
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], srv.token)
    print("serving %s" % url, flush=True)

    webm_dir = os.path.join(_TMP, "video")
    try:
        print("recording ...", flush=True)
        webm, lead = record(url, webm_dir)
        print("encoding %s ..." % args.out, flush=True)
        to_gif(webm, lead, args.out, fps=args.fps, width=args.width)
    finally:
        srv.shutdown()
        srv.server_close()
        media.shutdown()
    print("%s  (%.1f MB)" % (args.out, os.path.getsize(args.out) / 1e6))


if __name__ == "__main__":
    main()
