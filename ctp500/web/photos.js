"use strict";

// The photo browser: every photo on the print server (camera shots and uploads), grouped by the day it was
// taken; a viewer to flip through them, download, preview the print, print or delete; uploads by button or drop.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { photos: [], index: -1, previewOn: false, previewSeq: 0, lithoOn: false };

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("ctp500." + key); return v === null ? fallback : v; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("ctp500." + key, value); } catch { /* private mode */ } },
};

async function api(path, { method = "GET", body, headers = {}, raw = false } = {}) {
  const h = { "X-Ctp500-Client": "web", ...headers };
  const token = store.get("token", "");
  if (token) h.Authorization = "Bearer " + token;
  let payload = body;
  if (body !== undefined && !(body instanceof Blob)) { h["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, { method, headers: h, body: payload }); }
  catch { throw new Error("Can't reach the print server"); }
  if (res.status === 401) {
    const t = prompt("This printer needs its access token:");
    if (t) { store.set("token", t.trim()); return api(path, { method, body, headers, raw }); }
    throw new Error("Access token needed");
  }
  if (raw) {
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    return res;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) throw new Error(data.error || res.statusText);
  return data;
}

function withToken(url) {
  const token = store.get("token", "");
  return token ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : url;
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

// ------------------------------------------------------------------ grid

function dayLabel(t) {
  const d = new Date(t * 1000), today = new Date();
  const start = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(today) - start(d)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  const opts = { weekday: "long", month: "long", day: "numeric" };
  if (d.getFullYear() !== today.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

function timeLabel(t) {
  return new Date(t * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit" });
}

async function load() {
  try {
    const { photos } = await api("/api/camera/photos");
    state.photos = photos;
  } catch (err) { toast(err.message, "error"); return; }
  render();
}

function render() {
  const gallery = $("#gallery");
  $$(".day", gallery).forEach(el => el.remove());
  $("#empty").hidden = state.photos.length > 0;
  $("#count").textContent = state.photos.length ? `· ${state.photos.length}` : "";
  let section = null, label = null;
  state.photos.forEach((p, i) => {
    const l = dayLabel(p.taken);
    if (l !== label) {
      label = l;
      section = document.createElement("section");
      section.className = "day";
      section.innerHTML = "<h2></h2><div class='grid'></div>";
      section.querySelector("h2").textContent = l;
      gallery.append(section);
    }
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "tile";
    tile.title = timeLabel(p.taken);
    tile.innerHTML = "<img loading='lazy' alt=''>";
    tile.querySelector("img").src = withToken(p.thumb);
    tile.onclick = () => open(i);
    section.querySelector(".grid").append(tile);
  });
}

// ------------------------------------------------------------------ viewer

function open(i) {
  state.index = i;
  $("#viewer").hidden = false;
  document.body.classList.add("viewing");
  show();
}

function close() {
  cancelPendingPrint();
  $("#viewer").hidden = true;
  document.body.classList.remove("viewing");
  state.index = -1;
}

function show() {
  const p = state.photos[state.index];
  if (!p) { close(); return; }
  $("#viewer-img").src = withToken(p.url);
  $("#viewer-count").textContent = `${state.index + 1} / ${state.photos.length}`;
  $("#viewer-when").textContent = timeLabel(p.taken);
  $("#viewer-download").href = withToken(p.download);
  $("#viewer-download").setAttribute("download", p.name);
  $("#prev").disabled = state.index <= 0;
  $("#next").disabled = state.index >= state.photos.length - 1;
  if (state.previewOn) preview(); else $("#viewer-paper").hidden = true;
  if (state.lithoOn) lithophane(); else $("#viewer-litho").hidden = true;
  // the neighbours load in the background, so flipping through is instant
  for (const n of [state.photos[state.index + 1], state.photos[state.index - 1]]) {
    if (n) new Image().src = withToken(n.url);
  }
}

// After a preview arrives, the next photo's is drawn on the server too (it keeps them), so the swipe to it doesn't
// wait ~3 s for an AI drawing. One at a time, only while previews are showing.
let prefetching = null;
function prefetchPreview() {
  const n = state.photos[state.index + 1];
  if (!state.previewOn || !n || prefetching === n.name) return;
  prefetching = n.name;
  api(`/api/camera/photo/${encodeURIComponent(n.name)}/preview`, { method: "POST", body: overrides(), raw: true })
    .then(r => r.blob()).catch(() => {}).finally(() => { if (prefetching === n.name) prefetching = null; });
}

function step(d) {
  const i = state.index + d;
  if (i >= 0 && i < state.photos.length) { cancelPendingPrint(); state.index = i; show(); }
}

// The print preview and the lithophane share the space beside the photo (on a phone, the photo's place): one at
// a time.
function setPreview(on) {
  state.previewOn = on;
  const btn = $("#viewer-preview-btn");
  btn.classList.toggle("on", on);
  btn.textContent = on ? "Hide preview" : "Preview print";
  if (on && state.lithoOn) setLitho(false);
  $("#viewer").classList.toggle("previewing", state.previewOn || state.lithoOn);
}

function setLitho(on) {
  state.lithoOn = on;
  $("#viewer-litho-btn").classList.toggle("on", on);
  $("#viewer-litho").hidden = !on;
  if (on && state.previewOn) { setPreview(false); $("#viewer-paper").hidden = true; }
  $("#viewer").classList.toggle("previewing", state.previewOn || state.lithoOn);
}

// A lithophane: a thin 3D-printed panel that shows the photo when light shines through it (thick = dark). The
// server makes the preview (lit from behind) and the STL for Bambu Studio. Made for quick prints: minutes from
// Bambu Studio's estimates for the A1 mini at 0.24mm Draft, 100% infill, 1 wall (2026-11-01, a 4:3 photo), plus
// ~6 min of the printer's own start (heating, bed leveling, purge line): 80 mm flat = 20 + 6.
// Relief plaques: the Standard profile (0.20 mm, start included); Draft saved only 5 of 55 min at 80 mm (2026-11-02).
const LITHO_MINUTES = { flat: { 60: 18, 80: 26, 110: 41 }, curved: { 60: 29, 80: 39, 110: 55 },
  relief: { 60: 36, 80: 55, 110: 90 } };
const LITHO_SETTINGS = "In Bambu Studio pick 0.24mm Draft, then Sparse infill 100% and Wall loops 1.";

function lithophane() {
  const p = state.photos[state.index];
  const size = Number($("#litho-size").value), shape = $("#litho-shape").value;
  const base = `/api/camera/litho/${encodeURIComponent(p.name)}`;
  const img = $("#litho-preview");
  $("#viewer-litho").hidden = false;
  $("#litho-meta").textContent = "Shaping the lithophane…";
  img.onload = () => {
    const long = Math.max(img.naturalWidth, img.naturalHeight), short = Math.min(img.naturalWidth, img.naturalHeight);
    const other = Math.round(size * short / long);
    const dims = img.naturalWidth >= img.naturalHeight ? `${size} × ${other} mm` : `${other} × ${size} mm`;
    const mins = LITHO_MINUTES[shape][size];
    const time = mins ? `about ${mins} min` : "";
    $("#litho-meta").textContent = shape === "curved"
      ? `${dims}, curved, ${time} standing up (add a brim). White PLA. ${LITHO_SETTINGS} Light it with an LED tea light, never a candle.`
      : shape === "relief"
        ? `${dims}, a carved plaque up to 6 mm thick, ${time}. Any color PLA; Bambu Studio's 0.20mm Standard profile as it is. Best lit from the side.`
        : `${dims}, flat, ${time}. White PLA. ${LITHO_SETTINGS} Hold it up to a window or a lamp.`;
  };
  img.onerror = () => {      // ask again for the reason (an <img> can't read the error)
    $("#litho-meta").textContent = "Couldn't make the lithophane for this photo";
    fetch(img.src).then(r => r.json()).then(j => { if (j.error) $("#litho-meta").textContent = j.error; })
      .catch(() => {});
  };
  img.src = withToken(`${base}.png?size=${size}&shape=${shape}`);
  const link = $("#litho-download");
  link.href = withToken(`${base}.stl?size=${size}&shape=${shape}`);
  link.title = "An STL to open in Bambu Studio";
  // With the PC on, its helper slices it in Bambu Studio with those settings: a file to open and print as is.
  api("/api/status").then(s => {
    const ready = ((s.helper || {}).kinds || []).includes("slice");
    $("#litho-ready").hidden = !ready;
    link.textContent = ready ? "3D model (STL)" : "Download 3D file";
  }).catch(() => { $("#litho-ready").hidden = true; });
}

async function downloadReady() {
  const p = state.photos[state.index];
  const size = $("#litho-size").value, shape = $("#litho-shape").value;
  const btn = $("#litho-ready");
  btn.disabled = true;
  btn.textContent = "Slicing on the PC…";
  try {
    const res = await api(`/api/camera/litho/${encodeURIComponent(p.name)}.3mf?size=${size}&shape=${shape}`, { raw: true });
    const name = (/filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "") || [])[1] || "lithophane.gcode.3mf";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast("Open it in Bambu Studio and press Print");
  } catch (err) { toast(err.message, "error"); }
  btn.disabled = false;
  btn.textContent = "Ready to print";
}

function overrides() {
  const out = {};
  if ($("#viewer-style").value) out.style = $("#viewer-style").value;
  if ($("#viewer-weight").value) out.weight = Number($("#viewer-weight").value);
  if ($("#viewer-zoom").value) out.zoom = $("#viewer-zoom").value === "1";
  if ($("#viewer-sharpen").value) out.sharpen = $("#viewer-sharpen").value === "1";
  return out;
}

async function preview() {
  const p = state.photos[state.index];
  const seq = ++state.previewSeq;
  $("#viewer-paper").hidden = false;
  $("#viewer-preview-meta").textContent = "Drawing the print…";
  try {
    const res = await api(`/api/camera/photo/${encodeURIComponent(p.name)}/preview`, { method: "POST", body: overrides(), raw: true });
    const blob = await res.blob();
    if (seq !== state.previewSeq) return;
    const img = $("#viewer-preview");
    if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
    $("#viewer-preview-meta").textContent = `What prints · ${res.headers.get("X-Print-Length-Mm")} mm of paper`;
    prefetchPreview();
  } catch (err) {
    if (seq === state.previewSeq) $("#viewer-preview-meta").textContent = err.message;
  }
}

// Print waits 3 seconds with a visible countdown before anything is sent: on a phone a tap meant for the style
// menu or Preview landed on Print and it printed unasked (2026-10-30). Tapping again, closing the viewer or
// moving to another photo cancels.
const COUNTDOWN = 3;
let pendingPrint = null;

function printCurrent() {
  if (pendingPrint) { cancelPendingPrint(); return; }
  const p = state.photos[state.index];
  const body = overrides();
  const btn = $("#viewer-print");
  let left = COUNTDOWN;
  const label = () => { btn.textContent = `Printing in ${left}… tap to cancel`; };
  btn.classList.add("counting");
  label();
  pendingPrint = setInterval(() => {
    left--;
    if (left > 0) { label(); return; }
    clearInterval(pendingPrint);
    pendingPrint = null;
    resetPrintButton();
    sendPrint(p, body);
  }, 1000);
}

function cancelPendingPrint(silent = false) {
  if (!pendingPrint) return;
  clearInterval(pendingPrint);
  pendingPrint = null;
  resetPrintButton();
  if (!silent) toast("Print cancelled");
}

function resetPrintButton() {
  const btn = $("#viewer-print");
  btn.textContent = "Print";
  btn.classList.remove("counting");
}

async function sendPrint(p, body) {
  try {
    const { job } = await api(`/api/camera/photo/${encodeURIComponent(p.name)}/print`, { method: "POST", body });
    toast("Printing…");
    watch(job.id);
  } catch (err) { toast(err.message, "error"); }
}

async function watch(id) {
  for (let i = 0; i < 120; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const job = await api("/api/jobs/" + id);
      if (job.status === "done") { toast("Printed"); return; }
      if (job.status === "failed") { toast(job.error || "The print failed", "error"); return; }
      if (job.status === "cancelled") return;
    } catch { /* keep trying */ }
  }
}

async function deleteCurrent() {
  const p = state.photos[state.index];
  if (!confirm("Delete this photo from the print server? (If it came from the camera, it's still on the camera's card.)")) return;
  try {
    await api(`/api/camera/photo/${encodeURIComponent(p.name)}/delete`, { method: "POST" });
    state.photos.splice(state.index, 1);
    render();
    if (!state.photos.length) close();
    else { state.index = Math.min(state.index, state.photos.length - 1); show(); }
  } catch (err) { toast(err.message, "error"); }
}

// ------------------------------------------------------------------ upload

async function upload(files, { quiet = false } = {}) {
  files = [...files].filter(f => f.type.startsWith("image/") || /\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name));
  if (!files.length) { toast("Those aren't pictures", "error"); return []; }
  const bar = $("#upload-progress");
  bar.hidden = false;
  const names = [];
  let done = 0;
  for (const f of files) {
    try {
      const reply = await api("/api/camera/upload", { method: "POST", body: f,
        headers: { "Content-Type": f.type || "application/octet-stream", "X-Filename": f.name || "photo.jpg" } });
      names.push(reply.name);
    } catch (err) { toast(`${f.name}: ${err.message}`, "error"); }
    done++;
    $("#upload-bar").style.width = Math.round(done / files.length * 100) + "%";
  }
  bar.hidden = true;
  $("#upload-bar").style.width = "0";
  if (names.length && !quiet) toast(`Uploaded ${names.length} photo${names.length === 1 ? "" : "s"}`);
  await load();
  return names;
}

// A photo from the phone's camera: upload it, then open it with the print preview showing, one tap from Print.
async function fromCamera(file) {
  if (!file) return;
  toast("Sending the photo…");
  const [name] = await upload([file], { quiet: true });
  const i = state.photos.findIndex(p => p.name === name);
  if (i < 0) return;
  setPreview(true);
  open(i);
}

// ------------------------------------------------------------------ wiring

function init() {
  $("#zip").href = withToken("/api/camera/photos.zip");
  $("#upload-btn").onclick = () => $("#upload-input").click();
  $("#upload-input").onchange = e => { upload(e.target.files); e.target.value = ""; };
  $("#camera-btn").onclick = $("#camera-fab").onclick = () => $("#camera-input").click();
  $("#camera-input").onchange = e => { fromCamera(e.target.files[0]); e.target.value = ""; };
  $("#viewer-close").onclick = close;
  $("#prev").onclick = () => step(-1);
  $("#next").onclick = () => step(1);
  $("#viewer-print").onclick = printCurrent;
  $("#viewer-delete").onclick = deleteCurrent;
  $("#viewer-preview-btn").onclick = () => {
    setPreview(!state.previewOn);
    if (state.previewOn) preview(); else $("#viewer-paper").hidden = true;
  };
  $("#viewer-litho-btn").onclick = () => {
    setLitho(!state.lithoOn);
    if (state.lithoOn) lithophane();
  };
  $("#litho-size").onchange = $("#litho-shape").onchange = () => { if (state.lithoOn) lithophane(); };
  $("#litho-ready").onclick = downloadReady;
  $("#viewer-style").onchange = () => { cancelPendingPrint(true); if (state.previewOn) preview(); };
  $("#viewer-weight").onchange = $("#viewer-style").onchange;
  $("#viewer-zoom").onchange = $("#viewer-style").onchange;
  $("#viewer-sharpen").onchange = $("#viewer-style").onchange;

  document.addEventListener("keydown", e => {
    if ($("#viewer").hidden) return;
    if (e.key === "Escape") close();
    if (e.key === "ArrowLeft") step(-1);
    if (e.key === "ArrowRight") step(1);
  });

  // swipe left/right in the viewer
  let x0 = null;
  $("#viewer").addEventListener("touchstart", e => { x0 = e.touches[0].clientX; }, { passive: true });
  $("#viewer").addEventListener("touchend", e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
    x0 = null;
  });

  // drag and drop anywhere
  const overlay = $("#drop-overlay");
  document.addEventListener("dragover", e => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); overlay.hidden = false; }
  });
  document.addEventListener("dragleave", e => { if (!e.relatedTarget) overlay.hidden = true; });
  document.addEventListener("drop", e => {
    overlay.hidden = true;
    if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); upload(e.dataTransfer.files); }
  });

  load();
}

init();
