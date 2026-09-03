/* s3view - browser UI.
 *
 * Two ideas carry most of the weight here:
 *   1. Nothing large is ever fetched through this page. Video, audio, images
 *      and PDFs are loaded from short-lived presigned S3 URLs, so the browser
 *      issues its own HTTP range requests and seeking a 700 MB movie costs a
 *      few hundred KB rather than 700 MB.
 *   2. Both views are virtualized against a fixed row height, so a prefix with
 *      100k keys renders the same handful of DOM nodes as one with 10.
 */
"use strict";

const TOKEN = window.S3VIEW_TOKEN;
const ROW_H = 26;
const TILE_W = 132, TILE_H = 138;
const OVERSCAN = 8;

const $ = (s) => document.querySelector(s);
const scroller = $("#scroll"), sizer = $("#sizer");

const S = {
  bucket: null, prefix: "",
  folders: [], files: [],
  nextToken: null, truncated: false, loading: false,
  view: "list", sort: { key: "name", dir: 1 },
  filter: "", sel: -1,
  buckets: [], marks: [], start: null,
  searching: false, lastMs: 0, pages: 0,
  fitsThumbs: false, caps: {},
  hist: [], histAt: -1,
};

/* ------------------------------------------------------------------ api */
async function api(path, params, opts) {
  const u = new URL("/api/" + path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
  }
  const r = await fetch(u, Object.assign({ headers: { "X-S3View-Token": TOKEN } }, opts || {}));
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

function thumbUrl(key, size, extra) {
  const u = new URL("/api/thumb", location.origin);
  u.searchParams.set("bucket", S.bucket);
  u.searchParams.set("key", key);
  u.searchParams.set("size", size);
  u.searchParams.set("t", TOKEN);
  for (const [k, v] of Object.entries(extra || {})) u.searchParams.set(k, v);
  return u.toString();
}

/* -------------------------------------------------------------- helpers */
function fmtSize(n) {
  if (n === undefined || n === null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)) + " " + u[i];
}
function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = Date.now() / 1000;
  const opts = now - ts < 15552000
    ? { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "short", day: "2-digit" };
  return d.toLocaleString(undefined, opts).replace(",", "");
}
const GLYPH = {
  dir: "▸", fits: "✦", video: "▶", audio: "♪", image: "▣",
  text: "≡", pdf: "▤", binary: "◆", other: "·",
};
function glyph(e) { return e.type === "dir" ? GLYPH.dir : (GLYPH[e.kind] || GLYPH.other); }
function baseName(k) { const p = k.replace(/\/$/, "").split("/"); return p[p.length - 1]; }

/* ----------------------------------------------------------- navigation */
function uri(bucket, prefix) { return "s3://" + bucket + "/" + (prefix || ""); }

async function navigate(bucket, prefix, push = true) {
  if (push) {
    S.hist = S.hist.slice(0, S.histAt + 1);
    S.hist.push([bucket, prefix]);
    S.histAt = S.hist.length - 1;
  }
  S.bucket = bucket;
  S.prefix = prefix || "";
  S.folders = []; S.files = [];
  S.nextToken = null; S.truncated = false; S.sel = -1; S.pages = 0;
  S.searching = false;
  TQ.q.length = 0;  // abandon thumbnails queued for the prefix we just left
  $("#filter").value = ""; S.filter = "";
  scroller.scrollTop = 0;
  document.title = "s3view — " + S.bucket + "/" + S.prefix;
  renderCrumbs(); renderSide(); render();
  await loadPage();
}

function goBack() { if (S.histAt > 0) { S.histAt--; const [b, p] = S.hist[S.histAt]; navigate(b, p, false); } }
function goFwd() { if (S.histAt < S.hist.length - 1) { S.histAt++; const [b, p] = S.hist[S.histAt]; navigate(b, p, false); } }
function goUp() {
  if (!S.bucket) return;
  if (!S.prefix) {
    if (!S.start) showBucketPicker();
    return;
  }
  const parts = S.prefix.replace(/\/$/, "").split("/");
  parts.pop();
  navigate(S.bucket, parts.length ? parts.join("/") + "/" : "");
}

async function loadPage() {
  if (S.loading || (S.pages > 0 && !S.truncated)) return;
  S.loading = true; setStatus();
  try {
    const page = await api("list", {
      bucket: S.bucket, prefix: S.prefix, token: S.nextToken,
    });
    S.folders = S.folders.concat(page.folders);
    S.files = S.files.concat(page.files);
    S.nextToken = page.next_token;
    S.truncated = page.truncated;
    S.lastMs = page.ms; S.pages++;
  } catch (err) {
    showError(err.message);
  } finally {
    S.loading = false;
    render();
  }
}

/* -------------------------------------------------------------- entries */
function entries() {
  let dirs = S.folders.map((f) => ({ type: "dir", name: f.name, prefix: f.prefix }));
  let files = S.files.map((f) => Object.assign({ type: "file" }, f));
  if (S.filter) {
    const q = S.filter.toLowerCase();
    dirs = dirs.filter((d) => d.name.toLowerCase().includes(q));
    files = files.filter((f) => f.name.toLowerCase().includes(q));
  }
  const { key, dir } = S.sort;
  const cmp = (a, b) => {
    let r;
    if (key === "size") r = (a.size || 0) - (b.size || 0);
    else if (key === "mtime") r = (a.mtime || 0) - (b.mtime || 0);
    else r = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
    return r * dir;
  };
  dirs.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }) * dir);
  files.sort(cmp);
  return S.searching ? files : dirs.concat(files);
}

/* ------------------------------------------------------------ rendering */
let cached = [];
function render() {
  cached = entries();
  sizer.innerHTML = "";
  if (!cached.length && !S.loading) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = S.filter ? "Nothing matches “" + S.filter + "” in the loaded pages."
                             : "This prefix is empty.";
    sizer.style.height = "auto";
    sizer.appendChild(d);
    setStatus();
    return;
  }
  $("#head").style.display = S.view === "list" ? "grid" : "none";
  if (S.view === "list") {
    sizer.style.height = cached.length * ROW_H + "px";
  } else {
    const cols = Math.max(1, Math.floor(scroller.clientWidth / TILE_W));
    sizer.style.height = Math.ceil(cached.length / cols) * TILE_H + "px";
  }
  paint();
  setStatus();
}

function paint() {
  if (!cached.length) return;
  const top = scroller.scrollTop, h = scroller.clientHeight;
  const frag = document.createDocumentFragment();
  if (S.view === "list") {
    const first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    const last = Math.min(cached.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
    for (let i = first; i < last; i++) frag.appendChild(rowEl(cached[i], i));
  } else {
    const cols = Math.max(1, Math.floor(scroller.clientWidth / TILE_W));
    const w = scroller.clientWidth / cols;
    const first = Math.max(0, (Math.floor(top / TILE_H) - 1) * cols);
    const last = Math.min(cached.length, (Math.ceil((top + h) / TILE_H) + 1) * cols);
    for (let i = first; i < last; i++) frag.appendChild(tileEl(cached[i], i, cols, w));
  }
  sizer.replaceChildren(frag);
  pumpThumbs();  // tiles are connected now, so queued thumbnails can start
  maybeLoadMore();
}

function rowEl(e, i) {
  const d = document.createElement("div");
  d.className = "row" + (i === S.sel ? " sel" : "");
  d.style.top = i * ROW_H + "px";
  d.dataset.i = i;
  const ic = document.createElement("i");
  ic.className = "ic" + (e.type === "dir" ? " dir" : "");
  ic.textContent = glyph(e);
  const nm = document.createElement("div");
  nm.className = "nm";
  const sp = document.createElement("span");
  sp.textContent = S.searching ? e.key : e.name;
  nm.append(ic, sp);
  const sz = document.createElement("div");
  sz.className = "num";
  sz.textContent = e.type === "dir" ? "—" : fmtSize(e.size);
  const dt = document.createElement("div");
  dt.className = "dt";
  dt.textContent = e.type === "dir" ? "" : fmtDate(e.mtime);
  d.append(nm, sz, dt);
  return d;
}

function tileEl(e, i, cols, w) {
  const d = document.createElement("div");
  d.className = "tile" + (i === S.sel ? " sel" : "");
  d.style.top = Math.floor(i / cols) * TILE_H + "px";
  d.style.left = (i % cols) * w + "px";
  d.style.width = w + "px";
  d.style.height = TILE_H + "px";
  d.dataset.i = i;
  const box = document.createElement("div");
  box.className = "box";
  const lbl = document.createElement("div");
  lbl.className = "lbl";
  lbl.textContent = e.name;
  lbl.title = e.name;
  d.append(box, lbl);

  // Array-container thumbnails cost megabytes each, so they stay behind a
  // toggle; ordinary raster images are cheap and load on sight.
  const heavy = e.kind === "fits" || e.kind === "asdf";
  const wantThumb = e.type === "file" && e.thumb && (!heavy || S.fitsThumbs);
  if (wantThumb) {
    const g = document.createElement("span");
    g.className = "spin";
    box.appendChild(g);
    queueThumb(box, e);
  } else {
    const g = document.createElement("span");
    g.className = "glyph";
    g.textContent = glyph(e);
    box.appendChild(g);
  }
  return d;
}

/* Bandwidth on a remote link is the scarce resource, so thumbnails are
 * fetched a couple at a time and only for tiles still on screen. */
const TQ = { q: [], active: 0, max: 3 };
function queueThumb(box, e) {
  // Only enqueue. Tiles are built inside a DocumentFragment, so the box is not
  // connected to the document yet; pumping here would see isConnected === false
  // for every entry and quietly drain the whole queue. paint() pumps once the
  // fragment is in the DOM.
  TQ.q.push([box, e]);
}
function pumpThumbs() {
  while (TQ.active < TQ.max && TQ.q.length) {
    const [box, e] = TQ.q.shift();
    if (!box.isConnected) continue;
    TQ.active++;
    const img = new Image();
    img.onload = () => { if (box.isConnected) box.replaceChildren(img); TQ.active--; pumpThumbs(); };
    img.onerror = () => {
      if (box.isConnected) {
        const g = document.createElement("span");
        g.className = "glyph"; g.textContent = glyph(e); g.title = "no preview";
        box.replaceChildren(g);
      }
      TQ.active--; pumpThumbs();
    };
    img.src = thumbUrl(e.key, 160, { etag: e.etag, size_bytes: e.size });
  }
}

function showBucketPicker() {
  document.title = "s3view";
  S.bucket = null; S.prefix = "";
  cached = [];
  $("#head").style.display = "none";
  $("#crumbs").replaceChildren(txt("choose a bucket"));
  sizer.style.height = "auto";
  const wrap = document.createElement("div");
  wrap.className = "empty";
  if (!S.buckets.length) {
    wrap.textContent =
      "No start location is configured, and no buckets could be listed. " +
      "Pass one on the command line, e.g.  s3view s3://your-bucket/prefix/";
    sizer.replaceChildren(wrap);
    return;
  }
  const head = document.createElement("div");
  head.textContent = "Pick a bucket to start in, or set a default with " +
    "s3view --set-start s3://bucket/prefix/";
  head.style.marginBottom = "12px";
  wrap.appendChild(head);
  const list = document.createElement("div");
  list.style.cssText =
    "display:flex;flex-wrap:wrap;gap:6px;justify-content:center;max-width:760px;margin:0 auto";
  S.buckets.forEach((b) => {
    const btn = document.createElement("button");
    btn.textContent = b.name;
    btn.onclick = () => navigate(b.name, "");
    list.appendChild(btn);
  });
  wrap.appendChild(list);
  sizer.replaceChildren(wrap);
  setStatus();
}


function renderCrumbs() {
  const c = $("#crumbs");
  c.replaceChildren();
  const mk = (label, bucket, prefix, last) => {
    const s = document.createElement("span");
    s.className = "crumb" + (last ? " last" : "");
    s.textContent = label;
    if (!last) s.onclick = () => navigate(bucket, prefix);
    return s;
  };
  c.appendChild(mk(S.bucket, S.bucket, "", !S.prefix));
  const parts = S.prefix ? S.prefix.replace(/\/$/, "").split("/") : [];
  parts.forEach((p, i) => {
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = "/";
    c.appendChild(sep);
    c.appendChild(mk(p, S.bucket, parts.slice(0, i + 1).join("/") + "/", i === parts.length - 1));
  });
  c.scrollLeft = 1e6;
  const here = uri(S.bucket, S.prefix);
  $("#star").classList.toggle("on", S.marks.some((m) => m.uri === here));
}

function renderSide() {
  const marks = $("#marks");
  marks.replaceChildren();
  const here = uri(S.bucket, S.prefix);
  S.marks.forEach((m) => {
    const d = document.createElement("div");
    d.className = "side-item" + (m.uri === here ? " active" : "");
    d.title = m.uri;
    const label = document.createElement("span");
    label.textContent = m.name;
    label.style.overflow = "hidden";
    label.style.textOverflow = "ellipsis";
    const x = document.createElement("span");
    x.className = "x"; x.textContent = "×"; x.title = "Remove bookmark";
    x.onclick = async (ev) => {
      ev.stopPropagation();
      const r = await api("bookmarks", {}, {
        method: "POST",
        headers: { "X-S3View-Token": TOKEN, "Content-Type": "application/json" },
        body: JSON.stringify({ action: "remove", uri: m.uri }),
      });
      S.marks = r.bookmarks; renderSide(); renderCrumbs();
    };
    d.append(label, x);
    d.onclick = () => { const [b, p] = splitUri(m.uri); navigate(b, p); };
    marks.appendChild(d);
  });

  // ListBuckets only ever returns buckets the calling account OWNS. A bucket
  // reached cross-account through a bucket policy is perfectly readable but
  // never appears there, so fold in the ones we know we can reach: anything
  // bookmarked, plus wherever we currently are.
  const known = new Set(S.buckets.map((b) => b.name));
  const extra = [];
  const addExtra = (name) => {
    if (name && !known.has(name)) { known.add(name); extra.push(name); }
  };
  S.marks.forEach((m) => addExtra(splitUri(m.uri)[0]));
  addExtra(S.bucket);

  const all = S.buckets
    .map((b) => ({ name: b.name, owned: true }))
    .concat(extra.map((n) => ({ name: n, owned: false })))
    .sort((a, b) => a.name.localeCompare(b.name));

  const bl = $("#buckets");
  bl.replaceChildren();
  const q = ($("#bucketFilter").value || "").toLowerCase();
  all.filter((b) => !q || b.name.includes(q)).forEach((b) => {
    const d = document.createElement("div");
    d.className = "side-item" + (b.name === S.bucket && !S.prefix ? " active" : "");
    const label = document.createElement("span");
    label.textContent = b.name;
    label.style.overflow = "hidden";
    label.style.textOverflow = "ellipsis";
    d.appendChild(label);
    if (!b.owned) {
      const mark = document.createElement("span");
      mark.className = "foreign";
      mark.textContent = "\u00b7";
      d.appendChild(mark);
      d.title = b.name + "  \u2014  not owned by this account; reachable via a " +
        "bucket policy, so it is absent from ListBuckets";
    } else {
      d.title = b.name;
    }
    d.onclick = () => navigate(b.name, "");
    bl.appendChild(d);
  });
}

function splitUri(u) {
  const rest = u.replace(/^s3:\/\//, "");
  const i = rest.indexOf("/");
  return i < 0 ? [rest, ""] : [rest.slice(0, i), rest.slice(i + 1)];
}

function setStatus() {
  if (!S.bucket) { $("#stat").textContent = ""; $("#perf").textContent = ""; return; }
  const nd = S.folders.length, nf = S.files.length;
  const bytes = S.files.reduce((a, f) => a + (f.size || 0), 0);
  const shown = cached.length;
  let msg = "";
  if (S.loading) msg = "loading…  ";
  msg += nd + " folder" + (nd === 1 ? "" : "s") + ", " + nf + " file" + (nf === 1 ? "" : "s");
  if (S.filter || S.searching) msg += "  ·  " + shown + " shown";
  msg += "  ·  " + fmtSize(bytes);
  if (S.truncated) msg += "  ·  more on scroll";
  $("#stat").textContent = msg;
  $("#perf").textContent = S.lastMs ? S.lastMs + " ms · " + S.pages + "p" : "";
}

function showError(m) {
  $("#stat").textContent = "error: " + m;
}

function maybeLoadMore() {
  if (!S.truncated || S.loading || S.searching) return;
  if (scroller.scrollTop + scroller.clientHeight > sizer.clientHeight - 600) loadPage();
}

/* ------------------------------------------------------- copyable path */
function currentUri() {
  return S.bucket ? "s3://" + S.bucket + "/" + (S.prefix || "") : "";
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    // Clipboard API needs a secure context and a user gesture; fall back to
    // the old selection trick so this still works when it is unavailable.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
    ta.remove();
    return ok;
  }
}

async function copyPath() {
  const uri = currentUri();
  if (!uri) return;
  const btn = $("#copypath");
  const ok = await copyText(uri);
  btn.textContent = ok ? "\u2713" : "\u2715";
  btn.classList.toggle("ok", ok);
  setTimeout(() => { btn.textContent = "\u29c9"; btn.classList.remove("ok"); }, 1200);
}

/* Turn the breadcrumb into a selectable, editable address bar: select it with
 * the mouse and copy with cmd-C, or paste a different s3:// path and hit Enter. */
function openPathEditor() {
  const uri = currentUri();
  if (!uri) return;
  const inp = $("#pathinput");
  $("#crumbs").hidden = true;
  inp.hidden = false;
  inp.value = uri;
  inp.focus();
  inp.select();
}

function closePathEditor(go) {
  const inp = $("#pathinput");
  if (inp.hidden) return;
  const value = inp.value.trim();
  inp.hidden = true;
  $("#crumbs").hidden = false;
  if (go && value && value !== currentUri()) {
    const [b, p] = splitUri(value);
    if (b) navigate(b, p);
  }
}

$("#copypath").onclick = copyPath;
$("#crumbs").addEventListener("click", (ev) => {
  // Clicking a crumb navigates; clicking the empty strip opens the editor.
  if (!ev.target.closest(".crumb")) openPathEditor();
});
$("#pathinput").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { closePathEditor(true); ev.preventDefault(); }
  else if (ev.key === "Escape") { closePathEditor(false); ev.preventDefault(); }
  ev.stopPropagation();
});
$("#pathinput").addEventListener("blur", () => closePathEditor(false));


/* -------------------------------------------------------------- preview */
let ovIndex = -1;
function fileList() { return cached.filter((e) => e.type === "file"); }

async function openPreview(entry) {
  const files = fileList();
  ovIndex = files.findIndex((f) => f.key === entry.key);
  $("#ov").classList.add("show");
  await renderPreview(entry);
}

function closePreview() {
  $("#ov").classList.remove("show");
  $("#ovbody").replaceChildren();  // stops any playing media
  $("#ovfoot").replaceChildren();
}

function stepPreview(d) {
  const files = fileList();
  if (!files.length) return;
  ovIndex = (ovIndex + d + files.length) % files.length;
  renderPreview(files[ovIndex]);
}

async function renderPreview(e) {
  const body = $("#ovbody"), foot = $("#ovfoot");
  $("#ovtitle").textContent = e.key;
  body.replaceChildren(spinner());
  foot.replaceChildren(txt(fmtSize(e.size) + " · " + fmtDate(e.mtime)));

  try {
    if (e.kind === "video" || e.kind === "audio") return await previewMedia(e, body, foot);
    if (e.kind === "image") return await previewImage(e, body, foot);
    if (e.kind === "fits" || e.kind === "asdf") return await previewSci(e, body, foot);
    if (e.kind === "text") return await previewText(e, body, foot);
    if (e.kind === "pdf") return await previewPdf(e, body, foot);
    return previewFallback(e, body, foot);
  } catch (err) {
    body.replaceChildren(txt("Could not preview: " + err.message));
  }
}

function spinner() { const d = document.createElement("div"); d.className = "spin"; return d; }
function txt(t, cls) {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  s.textContent = t;
  return s;
}

async function previewMedia(e, body, foot) {
  const { url } = await api("url", { bucket: S.bucket, key: e.key });
  const el = document.createElement(e.kind === "video" ? "video" : "audio");
  el.controls = true;
  el.autoplay = true;
  el.preload = "metadata";
  el.src = url;              // straight to S3: the browser range-requests it
  body.replaceChildren(el);

  const gauge = txt("");
  const hint = txt("");
  foot.replaceChildren(
    txt(fmtSize(e.size) + " · streaming from S3, not downloaded"), gauge, hint
  );
  const update = () => {
    if (!el.duration || !isFinite(el.duration) || !el.buffered.length) return;
    let secs = 0;
    for (let i = 0; i < el.buffered.length; i++) secs += el.buffered.end(i) - el.buffered.start(i);
    const approx = (secs / el.duration) * e.size;
    gauge.textContent = "  ·  ~" + fmtSize(approx) + " transferred (" +
      Math.round((approx / e.size) * 100) + "% of file)";
  };
  el.addEventListener("progress", update);
  el.addEventListener("timeupdate", update);
  el.addEventListener("error", () => {
    hint.className = "note";
    hint.textContent = "  ·  this browser can't decode this container — try Open in player";
  });

  const open = document.createElement("button");
  open.textContent = "Open in player";
  open.onclick = () => api("open", { bucket: S.bucket, key: e.key }).catch((x) => alert(x.message));
  foot.appendChild(open);
}

async function previewImage(e, body, foot) {
  const { url } = await api("url", { bucket: S.bucket, key: e.key });
  const img = new Image();
  img.onload = () => {
    body.replaceChildren(img);
    foot.replaceChildren(txt(
      img.naturalWidth + "×" + img.naturalHeight + " · " + fmtSize(e.size)));
  };
  img.onerror = () => {
    // Formats the browser can't decode (TIFF, ...) still render server-side.
    const alt = new Image();
    alt.src = thumbUrl(e.key, 1024, { etag: e.etag, size_bytes: e.size });
    alt.onload = () => body.replaceChildren(alt);
    alt.onerror = () => body.replaceChildren(txt("No preview available."));
  };
  img.src = url;
}

async function previewSci(e, body, foot) {
  // FITS and ASDF differ only in what a "unit" is -- an HDU or a named array --
  // so one previewer serves both. Metadata comes from a small ranged read; the
  // image itself is rendered server-side from strided ranged reads.
  const state = { size: 512, stretch: "zscale", cmap: "gray", sel: "auto", plane: 0 };
  let meta = null;
  try {
    meta = await api("sci/meta", { bucket: S.bucket, key: e.key });
  } catch (err) { /* still worth trying to render */ }

  const units = (meta && meta.units ? meta.units : []).filter((u) => u.previewable);
  const unitFor = (sel) => (sel === "auto" ? units[0] : units.find((u) => u.sel === sel));

  const img = new Image();
  img.className = "fits";
  const readout = txt("");
  let showingDoc = false;
  let seq = 0;

  const draw = () => {
    const mine = ++seq;
    let sharpArrived = false;
    body.replaceChildren(spinner());
    const t0 = performance.now();

    // Progressive: a cheap low-res pass paints first, then the requested
    // resolution replaces it. This is a second strided read, so it only pays
    // for itself at the large sizes.
    if (+state.size > 512) {
      const quick = new Image();
      quick.className = "fits";
      quick.onload = () => {
        if (mine === seq && !sharpArrived && !showingDoc) body.replaceChildren(quick);
      };
      quick.src = thumbUrl(e.key, 128, {
        etag: e.etag, size_bytes: e.size, stretch: state.stretch,
        cmap: state.cmap, sel: state.sel, plane: state.plane,
      });
    }

    img.onload = () => {
      if (mine !== seq) return;
      sharpArrived = true;
      body.replaceChildren(img);
      const u = unitFor(state.sel);
      let est = "";
      if (u && u.shape && u.shape.length >= 2 && u.itemsize) {
        const width = u.shape[u.shape.length - 1];
        const bytes = Math.min(state.size, u.shape[u.shape.length - 2]) * width * u.itemsize;
        est = "  \u00b7  read ~" + fmtSize(bytes) + " of " + fmtSize(e.size) +
          " (" + (100 * bytes / e.size).toFixed(1) + "%)";
      }
      readout.textContent = ((performance.now() - t0) / 1000).toFixed(1) + " s" + est;
    };
    img.onerror = () => {
      if (mine === seq) body.replaceChildren(txt("Could not render this file."));
    };
    img.src = thumbUrl(e.key, state.size, {
      etag: e.etag, size_bytes: e.size, stretch: state.stretch,
      cmap: state.cmap, sel: state.sel, plane: state.plane,
    });
  };

  const sel = (label, opts, key) => {
    const s = document.createElement("select");
    opts.forEach(([v, t]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      s.appendChild(o);
    });
    s.value = state[key];
    s.onchange = () => { state[key] = s.value; draw(); };
    const wrap = document.createElement("label");
    wrap.style.display = "inline-flex";
    wrap.style.gap = "4px";
    wrap.style.alignItems = "center";
    wrap.append(txt(label), s);
    return wrap;
  };

  const unitOpts = [["auto", "auto"]].concat(units.map((u) => [u.sel, u.label]));

  const docBtn = document.createElement("button");
  docBtn.textContent = (meta && meta.doc) || "Metadata";
  docBtn.onclick = () => {
    showingDoc = !showingDoc;
    docBtn.classList.toggle("on", showingDoc);
    if (showingDoc) {
      const pre = document.createElement("pre");
      pre.textContent = meta ? meta.text : "(metadata unavailable)";
      body.replaceChildren(pre);
    } else { draw(); }
  };

  foot.replaceChildren(
    txt(fmtSize(e.size)),
    sel((meta && meta.selector) || "unit", unitOpts, "sel"),
    sel("stretch", [["zscale", "zscale"], ["asinh", "asinh"], ["log", "log"],
                    ["99.5", "99.5%"], ["minmax", "minmax"]], "stretch"),
    sel("cmap", [["gray", "gray"], ["viridis", "viridis"], ["magma", "magma"],
                 ["inferno", "inferno"], ["cividis", "cividis"]], "cmap"),
    sel("res", [["256", "256"], ["512", "512"], ["768", "768"], ["1024", "1024"]], "size"),
    docBtn, readout,
  );
  draw();
}

async function previewText(e, body, foot) {
  const r = await api("text", { bucket: S.bucket, key: e.key });
  const pre = document.createElement("pre");
  pre.textContent = r.text;
  body.replaceChildren(pre);
  foot.replaceChildren(txt(
    "read " + fmtSize(r.read) + " of " + fmtSize(r.size) + (r.truncated ? " (truncated)" : "")));
}

async function previewPdf(e, body, foot) {
  const { url } = await api("url", { bucket: S.bucket, key: e.key });
  const f = document.createElement("iframe");
  f.src = url;
  f.style.cssText = "width:100%;height:calc(92vh - 150px);border:0;background:#fff";
  body.replaceChildren(f);
}

function previewFallback(e, body, foot) {
  const pre = document.createElement("pre");
  pre.textContent =
    "key   " + e.key + "\nsize  " + fmtSize(e.size) + " (" + e.size + " bytes)" +
    "\ndate  " + fmtDate(e.mtime) + "\netag  " + (e.etag || "") +
    "\n\nNo inline viewer for this type.";
  body.replaceChildren(pre);
}

/* ------------------------------------------------------------- controls */
function activate(e) {
  if (!e) return;
  if (e.type === "dir") navigate(S.bucket, e.prefix);
  else openPreview(e);
}

function selectAt(i, scroll) {
  const prev = S.sel;
  S.sel = Math.max(0, Math.min(cached.length - 1, i));
  // Update the highlight in place. Repainting here would swap out the very
  // node the user is clicking, and Chrome only fires dblclick when both
  // clicks land on the same element -- so a repaint here breaks opening
  // folders by double-click.
  const was = sizer.querySelector('[data-i="' + prev + '"]');
  if (was) was.classList.remove("sel");
  const now = sizer.querySelector('[data-i="' + S.sel + '"]');
  if (now) now.classList.add("sel");
  if (scroll) {
    const cols = S.view === "grid"
      ? Math.max(1, Math.floor(scroller.clientWidth / TILE_W)) : 1;
    const rowTop = S.view === "list"
      ? S.sel * ROW_H : Math.floor(S.sel / cols) * TILE_H;
    const rowH = S.view === "list" ? ROW_H : TILE_H;
    if (rowTop < scroller.scrollTop) scroller.scrollTop = rowTop;
    else if (rowTop + rowH > scroller.scrollTop + scroller.clientHeight) {
      scroller.scrollTop = rowTop + rowH - scroller.clientHeight;
    }
    paint();  // keyboard nav may have moved the selection outside the window
  }
}

scroller.addEventListener("scroll", () => { paint(); }, { passive: true });
window.addEventListener("resize", () => render());

sizer.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-i]");
  if (!el) return;
  selectAt(+el.dataset.i, false);
});
sizer.addEventListener("dblclick", (ev) => {
  const el = ev.target.closest("[data-i]");
  if (el) activate(cached[+el.dataset.i]);
});

$("#up").onclick = goUp;
$("#back").onclick = goBack;
$("#fwd").onclick = goFwd;
$("#reload").onclick = async () => {
  await api("refresh", {});
  navigate(S.bucket, S.prefix, false);
};
$("#viewList").onclick = () => setView("list");
$("#viewGrid").onclick = () => setView("grid");
function setView(v) {
  S.view = v;
  $("#viewList").classList.toggle("on", v === "list");
  $("#viewGrid").classList.toggle("on", v === "grid");
  let btn = $("#fitsToggle");
  if (v === "grid" && !btn) {
    btn = document.createElement("button");
    btn.id = "fitsToggle";
    btn.textContent = "Array thumbs";
    btn.title = "Render FITS/ASDF previews in the gallery (a few MB each)";
    btn.classList.toggle("on", S.fitsThumbs);
    btn.onclick = () => {
      S.fitsThumbs = !S.fitsThumbs;
      btn.classList.toggle("on", S.fitsThumbs);
      render();
    };
    $("#viewGrid").after(btn);
  } else if (v === "list" && btn) { btn.remove(); }
  render();
}

$("#filter").addEventListener("input", (ev) => {
  S.filter = ev.target.value.trim();
  S.sel = -1;
  render();
});

$("#deep").onclick = async () => {
  const q = $("#filter").value.trim() || prompt("Search keys under " + S.prefix + " containing:");
  if (!q) return;
  $("#filter").value = q;
  S.loading = true; setStatus();
  try {
    const r = await api("search", { bucket: S.bucket, prefix: S.prefix, q });
    S.searching = true;
    S.folders = [];
    S.files = r.files;
    S.filter = "";
    S.truncated = false;
    $("#stat").textContent = "search: " + r.files.length + " match" +
      (r.files.length === 1 ? "" : "es") + " after scanning " + r.scanned +
      " keys" + (r.complete ? "" : " (budget reached)");
  } catch (err) { showError(err.message); }
  finally { S.loading = false; render(); }
};

$("#star").onclick = async () => {
  const here = uri(S.bucket, S.prefix);
  const have = S.marks.some((m) => m.uri === here);
  const r = await api("bookmarks", {}, {
    method: "POST",
    headers: { "X-S3View-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({
      action: have ? "remove" : "add", uri: here,
      name: S.prefix ? baseName(S.prefix) : S.bucket,
    }),
  });
  S.marks = r.bookmarks;
  renderSide(); renderCrumbs();
};

$("#bucketFilter").addEventListener("input", renderSide);

document.querySelectorAll("#head span").forEach((h) => {
  h.onclick = () => {
    const k = h.dataset.sort;
    S.sort = { key: k, dir: S.sort.key === k ? -S.sort.dir : 1 };
    document.querySelectorAll("#head span").forEach((x) => {
      x.textContent = x.textContent.replace(/ [▲▼]$/, "");
    });
    h.textContent += S.sort.dir > 0 ? " ▲" : " ▼";
    render();
  };
});

$("#ovclose").onclick = closePreview;
$("#ovprev").onclick = () => stepPreview(-1);
$("#ovnext").onclick = () => stepPreview(1);
$("#ov").addEventListener("click", (ev) => { if (ev.target.id === "ov") closePreview(); });
$("#ovdl").onclick = async () => {
  const f = fileList()[ovIndex];
  if (!f) return;
  const { url } = await api("url", { bucket: S.bucket, key: f.key, download: 1 });
  location.href = url;
};
$("#ovcopy").onclick = async () => {
  const f = fileList()[ovIndex];
  if (!f) return;
  const { url } = await api("url", { bucket: S.bucket, key: f.key });
  await navigator.clipboard.writeText(url);
  $("#ovcopy").textContent = "Copied ✓";
  setTimeout(() => ($("#ovcopy").textContent = "Copy URL"), 1400);
};

document.addEventListener("keydown", (ev) => {
  const open = $("#ov").classList.contains("show");
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);

  if (open) {
    if (ev.key === "Escape") { closePreview(); ev.preventDefault(); }
    else if (ev.key === "ArrowRight") { stepPreview(1); ev.preventDefault(); }
    else if (ev.key === "ArrowLeft") { stepPreview(-1); ev.preventDefault(); }
    else if (ev.key === " ") {
      const v = $("#ovbody").querySelector("video, audio");
      if (v) { v.paused ? v.play() : v.pause(); ev.preventDefault(); }
      else { closePreview(); ev.preventDefault(); }
    }
    return;
  }
  if (typing) {
    if (ev.key === "Escape") { ev.target.blur(); }
    if (ev.key === "Enter" && ev.target.id === "filter" && cached.length) {
      selectAt(0, true); activate(cached[0]);
    }
    return;
  }

  if (ev.key === "ArrowDown") { selectAt(S.sel + 1, true); ev.preventDefault(); }
  else if (ev.key === "ArrowUp") { selectAt(S.sel - 1, true); ev.preventDefault(); }
  else if (ev.key === "Enter" || (ev.key === "ArrowRight" && S.view === "list")) {
    activate(cached[S.sel]); ev.preventDefault();
  } else if (ev.key === "ArrowLeft" && S.view === "list") { goUp(); ev.preventDefault(); }
  else if (ev.key === "ArrowUp" && ev.metaKey) { goUp(); ev.preventDefault(); }
  else if (ev.key === " ") {
    const e = cached[S.sel];
    if (e && e.type === "file") { openPreview(e); ev.preventDefault(); }
  } else if (ev.key === "/") { $("#filter").focus(); ev.preventDefault(); }
  else if (ev.key === "r" && ev.metaKey) { $("#reload").click(); ev.preventDefault(); }
  else if (ev.key.toLowerCase() === "c" && ev.metaKey && ev.shiftKey) {
    copyPath(); ev.preventDefault();
  } else if (ev.key.toLowerCase() === "l" && ev.metaKey) {
    openPathEditor(); ev.preventDefault();
  }
});

/* ----------------------------------------------------------------- boot */
(async function boot() {
  try {
    const cfg = await api("config", {});
    S.marks = cfg.bookmarks || [];
    S.start = cfg.start;
    S.caps = cfg.capabilities || {};
    renderSide();
    const bucketsReady = api("buckets", {})
      .then((r) => { S.buckets = r.buckets; renderSide(); return r.buckets; })
      .catch(() => []);
    if (cfg.start) {
      const [b, p] = splitUri(cfg.start);
      await navigate(b, p);
      await bucketsReady;
    } else {
      // First run with nothing configured: let them pick a bucket rather than
      // guessing one or failing with an empty listing.
      await bucketsReady;
      showBucketPicker();
    }
  } catch (err) {
    document.body.innerHTML =
      '<div class="empty">s3view failed to start: ' + err.message + "</div>";
  }
})();
