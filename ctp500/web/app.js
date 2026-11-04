"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  tab: "note",
  picture: null,        // {data: base64, name, url}
  qrKind: "link",
  lastDoc: "",
  printing: null,       // job id being watched
  previewSeq: 0,
};

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("ctp500." + key); return v === null ? fallback : v; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("ctp500." + key, value); } catch { /* private mode */ } },
};

// ------------------------------------------------------------------ API

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = { "X-Ctp500-Client": "web" };
  const token = store.get("token", "");
  if (token) headers.Authorization = "Bearer " + token;
  if (body !== undefined && !(body instanceof Blob)) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : (body instanceof Blob ? body : JSON.stringify(body)) });
  } catch {
    throw new Error("Can't reach the print server");
  }
  if (res.status === 401) {
    const t = prompt("This printer needs its access token:");
    if (t) { store.set("token", t.trim()); return api(path, { method, body, raw }); }
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

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

// ------------------------------------------------------------------ building the print

function noteBlocksFrom(text) {
  // The server parses markup; send it as one markup document.
  return { markup: text, size: Number($("#note-size").value), font: $("#note-font").value };
}

function buildDoc() {
  switch (state.tab) {
    case "note": {
      const text = $("#note").value;
      if (!text.trim()) return null;
      return { blocks: [{ type: "markup", ...noteBlocksFrom(text) }] };
    }
    case "picture": {
      if (!state.picture) return null;
      const blocks = [];
      const above = $("#pic-above").value, below = $("#pic-below").value;
      if (above.trim()) blocks.push({ type: "markup", markup: above });
      blocks.push({
        type: "image", data: state.picture.data, name: state.picture.name,
        dither: $("#pic-dither").value, width: Number($("#pic-width").value),
        detail: Number($("#pic-detail").value), weight: Number($("#pic-weight").value),
        brightness: Number($("#pic-bright").value), contrast: Number($("#pic-contrast").value),
        sideways: $("#pic-rotate").checked, auto_contrast: $("#pic-auto").checked,
        invert: $("#pic-invert").checked,
      });
      if (below.trim()) blocks.push({ type: "markup", markup: below });
      return { blocks };
    }
    case "qr": {
      let data;
      if (state.qrKind === "wifi") {
        const ssid = $("#wifi-ssid").value;
        if (!ssid) return null;
        const esc = s => s.replace(/([\\;,:"])/g, "\\$1");
        const sec = $("#wifi-sec").value;
        data = `WIFI:T:${sec};S:${esc(ssid)};` + (sec === "nopass" ? "" : `P:${esc($("#wifi-pass").value)};`) +
          ($("#wifi-hidden").checked ? "H:true;" : "") + ";";
      } else {
        data = $("#qr-data").value.trim();
        if (!data) return null;
      }
      const blocks = [{ type: "qr", data, size: Number($("#qr-size").value), level: "M" }];
      const caption = $("#qr-caption").value.trim() || (state.qrKind === "wifi" ? `Wi-Fi: ${$("#wifi-ssid").value}` : "");
      if (caption) blocks.push({ type: "text", text: caption, align: "center", size: 24, bold: true });
      return { blocks };
    }
    case "banner": {
      const text = $("#banner-text").value.trim();
      if (!text) return null;
      return { blocks: [{ type: "banner", text, font: $("#banner-font").value, bold: $("#banner-bold").checked }] };
    }
    default:
      return null;
  }
}

// ------------------------------------------------------------------ preview

let previewTimer = 0;
function schedulePreview(delay = 250) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, delay);
}

async function updatePreview() {
  const doc = buildDoc();
  const img = $("#preview"), empty = $("#paper-empty"), meta = $("#meta");
  if (!doc) {
    img.hidden = true; empty.hidden = false; meta.textContent = " "; meta.className = "meta";
    $("#print-btn").disabled = true; state.lastDoc = "";
    return;
  }
  // photo tones are tuned to the darkness they print at, so the preview needs it too
  if ($("#darkness").value !== "auto") doc.darkness = $("#darkness").value;
  const key = JSON.stringify(doc);
  if (key === state.lastDoc) return;
  const seq = ++state.previewSeq;
  try {
    const res = await api("/api/preview", { method: "POST", body: doc, raw: true });
    if (seq !== state.previewSeq) return;
    const blob = await res.blob();
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    img.hidden = false; empty.hidden = true;
    const mm = Number(res.headers.get("X-Print-Length-Mm") || 0);
    meta.className = "meta";
    meta.textContent = mm >= 100 ? `${(mm / 10).toFixed(1)} cm of paper` : `${mm} mm of paper`;
    $("#print-btn").disabled = false;
    state.lastDoc = key;
  } catch (err) {
    if (seq !== state.previewSeq) return;
    meta.className = "meta error"; meta.textContent = err.message;
    $("#print-btn").disabled = true; state.lastDoc = "";
  }
}

// ------------------------------------------------------------------ printing

async function doPrint() {
  const doc = buildDoc();
  if (!doc) return;
  const btn = $("#print-btn");
  btn.disabled = true;
  try {
    if ($("#darkness").value !== "auto") doc.darkness = $("#darkness").value;
    doc.copies = Math.max(1, Math.min(20, Number($("#copies").value) || 1));
    const { job } = await api("/api/print", { method: "POST", body: doc });
    state.printing = job.id;
    showProgress(0);
    pollStatus(true);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function showProgress(p) {
  const bar = $("#progress");
  if (p === null) { bar.hidden = true; return; }
  bar.hidden = false;
  $("#progress-bar").style.width = Math.round(p * 100) + "%";
}

// ------------------------------------------------------------------ status

let statusTimer = 0;
async function pollStatus(soon = false) {
  clearTimeout(statusTimer);
  let next = 4000;
  try {
    const s = await api("/api/status");
    renderStatus(s);
    if (state.printing) {
      next = 600;
      const job = (s.queue || []).find(j => j.id === state.printing);
      if (job) {
        showProgress(job.progress);
        if (job.status === "waiting" && state.waitingShown !== job.id) {
          state.waitingShown = job.id;
          toast(`Waiting for the printer (${job.error || "not ready"}); it prints once that's fixed`, "error");
        }
      } else {
        const done = await api("/api/jobs/" + state.printing);
        state.printing = null;
        showProgress(null);
        if (done.status === "done") toast("Printed");
        else if (done.status === "failed") toast(done.error || "The print failed", "error");
        else if (done.status === "cancelled") toast("Cancelled");
        if (!$("#panel-history").hidden) loadHistory();
      }
    }
  } catch (err) {
    renderStatus(null, err.message);
    next = 5000;
  }
  statusTimer = setTimeout(pollStatus, soon ? 300 : next);
}

function renderStatus(s, error) {
  const pill = $("#status"), text = $("#status-text");
  pill.className = "status";
  if (!s) { pill.classList.add("bad"); text.textContent = "Server offline"; pill.title = error || ""; return; }
  const p = s.printer;
  pill.title = p.last_error || p.name;
  const waiting = (s.queue || []).find(j => j.status === "waiting");
  if (waiting) {
    pill.classList.add("bad"); pill.title = waiting.error || "";
    text.textContent = problemText(p.last_status, true) ? `Waiting: ${problemText(p.last_status, true)}` : "Waiting for the printer";
  }
  else if (p.state === "printing" || (s.queue || []).length) { pill.classList.add("busy"); text.textContent = "Printing…"; }
  else if (p.state === "connecting") { pill.classList.add("busy"); text.textContent = "Connecting…"; }
  else if (p.state === "error") { pill.classList.add("bad"); text.textContent = "Printer not reachable"; }
  else if (problemText(p.last_status, true)) {
    // from the last time the printer was asked; tapping the pill asks again
    pill.classList.add("bad"); text.textContent = problemText(p.last_status, true); pill.title = "Tap to check again";
  }
  else if (p.simulated) { pill.classList.add("ok"); text.textContent = "Simulated printer"; }
  else if (p.connected) { pill.classList.add("ok"); text.textContent = "Connected"; }
  else { pill.classList.add("ok"); text.textContent = "Ready"; }
}

const PROBLEMS = { out_of_paper: "Out of paper or lid open", lid_open: "Lid open", overheated: "Too hot, cooling down",
  low_battery: "Battery low" };
function problemText(status, blockingOnly = false) {
  const list = (status && status.problems) || [];
  const shown = list.filter(p => !blockingOnly || p === "out_of_paper" || p === "lid_open");
  return shown.map(p => PROBLEMS[p] || `Problem (${p})`).join(", ");
}

async function checkPrinter() {
  const text = $("#status-text");
  text.textContent = "Checking…";
  try {
    const r = await api("/api/check", { method: "POST" });
    const st = r.status || {};
    if (r.printer.simulated) toast("Simulated printer is fine");
    else if (problemText(st)) toast(`The printer says: ${problemText(st)}`, "error");
    else toast(st.firmware ? `Printer OK · firmware ${st.firmware}` : "Connected to the printer");
  } catch (err) {
    toast(err.message, "error");
  }
  pollStatus();
}

// ------------------------------------------------------------------ pictures

const MAX_SIDE = 1600;
function loadPicture(file) {
  if (!file || !file.type.startsWith("image/")) { toast("That isn't a picture", "error"); return; }
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = () => {
    const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    const ctx = canvas.getContext("2d");
    const keepAlpha = /png|gif|webp|svg/.test(file.type);
    if (!keepAlpha) { ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height); }
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL(keepAlpha ? "image/png" : "image/jpeg", 0.92);
    state.picture = { data: dataUrl.split(",")[1], name: file.name || "Picture" };
    $("#pic-thumb").src = url; $("#pic-thumb").hidden = false; $("#drop-empty").hidden = true;
    switchTab("picture");
    schedulePreview(0);
  };
  img.onerror = () => toast("Couldn't read that picture", "error");
  img.src = url;
}

function clearPicture() {
  state.picture = null;
  $("#pic-thumb").hidden = true; $("#drop-empty").hidden = false; $("#pic-file").value = "";
  schedulePreview(0);
}

// ------------------------------------------------------------------ tabs, toolbar, templates

function switchTab(tab) {
  state.tab = tab;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  $$(".pane").forEach(p => p.classList.toggle("active", p.dataset.pane === tab));
  if (tab === "camera") loadPhotos();
  if (tab !== "templates") schedulePreview(0);
}

// ------------------------------------------------------------------ camera photos

function withToken(url) {          // plain links can't send the Authorization header
  const token = store.get("token", "");
  return token ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : url;
}

async function loadPhotos() {
  const box = $("#photos");
  $("#photos-zip").href = withToken("/api/camera/photos.zip");
  try {
    const { photos } = await api("/api/camera/photos");
    box.replaceChildren();
    if (!photos.length) {
      box.innerHTML = '<div class="empty">No photos yet. Hold OK on the camera to send its photos here.</div>';
      return;
    }
    for (const p of photos) {
      const card = document.createElement("div");
      card.className = "photo";
      card.innerHTML = '<img loading="lazy" alt=""><div class="sub"></div><div class="actions">' +
        '<button type="button" class="ghost small" data-act="print">Print</button>' +
        '<button type="button" class="ghost small" data-act="edit">Edit</button>' +
        '<a data-act="save" download>Save</a>' +
        '<button type="button" class="ghost small" data-act="delete">Delete</button></div>';
      card.querySelector("img").src = withToken(p.thumb);
      card.querySelector('[data-act="save"]').href = withToken(p.download);
      card.querySelector('[data-act="save"]').setAttribute("download", p.name);
      card.querySelector(".sub").textContent = timeAgo(p.time);
      card.querySelector('[data-act="print"]').onclick = async () => {
        try {
          const { job } = await api(`/api/camera/photo/${encodeURIComponent(p.name)}/print`, { method: "POST" });
          state.printing = job.id; showProgress(0); pollStatus(true);
        } catch (err) { toast(err.message, "error"); }
      };
      card.querySelector('[data-act="edit"]').onclick = async () => {
        try {
          const res = await api(p.url, { raw: true });
          loadPicture(new File([await res.blob()], p.name, { type: "image/jpeg" }));
        } catch (err) { toast(err.message, "error"); }
      };
      card.querySelector('[data-act="delete"]').onclick = async () => {
        if (!confirm("Delete this photo from the print server? (It stays on the camera's SD card.)")) return;
        try {
          await api(`/api/camera/photo/${encodeURIComponent(p.name)}/delete`, { method: "POST" });
          loadPhotos();
        } catch (err) { toast(err.message, "error"); }
      };
      box.append(card);
    }
  } catch (err) {
    box.innerHTML = '<div class="empty"></div>';
    box.firstChild.textContent = err.message;
  }
}

function insertAtLineStart(prefix) {
  const ta = $("#note");
  const start = ta.value.lastIndexOf("\n", ta.selectionStart - 1) + 1;
  ta.setRangeText(prefix, start, start, "end");
  ta.focus(); schedulePreview();
}

function insertInline(text) {
  const ta = $("#note");
  ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
  ta.focus(); schedulePreview();
}

function wrapLine(marker) {
  const ta = $("#note");
  const start = ta.value.lastIndexOf("\n", ta.selectionStart - 1) + 1;
  let end = ta.value.indexOf("\n", ta.selectionStart);
  if (end < 0) end = ta.value.length;
  const line = ta.value.slice(start, end);
  const next = line.startsWith(marker) && line.endsWith(marker) && line.length > marker.length * 2
    ? line.slice(marker.length, -marker.length) : marker + line + marker;
  ta.setRangeText(next, start, end, "end");
  ta.focus(); schedulePreview();
}

const TEMPLATES = [
  { name: "To-do list", desc: "Checkboxes with today's date", text: "# To do\n[center] {weekday}, {date}\n---\n- [ ] \n- [ ] \n- [ ] \n" },
  { name: "Shopping list", desc: "Grouped checklist", text: "# Shopping\n---\n## Produce\n- [ ] \n## Dairy\n- [ ] \n## Other\n- [ ] \n" },
  { name: "Sticky note", desc: "Big, bold reminder", text: "[huge][bold][center] Don't forget\n\n[big][center] \n\n[small][center] {datetime}" },
  { name: "Daily plan", desc: "Morning / afternoon / evening", text: "# {weekday}\n[center] {date}\n===\n## Morning\n- [ ] \n## Afternoon\n- [ ] \n## Evening\n- [ ] \n---\n**Notes**\n\n\n" },
  { name: "Receipt", desc: "Items with prices", text: "# Receipt\n[center] {datetime}\n- - -\nCoffee ........ $3.50\nBagel ........ $2.25\n- - -\n**Total ........ $5.75**\n\n[center] Thank you!" },
  { name: "Label", desc: "Big text to stick on a box", text: "[huge][bold][center] KITCHEN\n---\n[center] Plates, cups, utensils" },
  { name: "Message", desc: "A note for someone", text: "> For you\n\n[big] \n\n[right] — {weekday}" },
  { name: "Wi-Fi card", desc: "QR code guests can scan", tab: "qr", qr: "wifi" },
  { name: "Banner", desc: "Huge sideways letters", tab: "banner" },
];

function renderTemplates() {
  const box = $("#templates");
  for (const t of TEMPLATES) {
    const card = document.createElement("button");
    card.type = "button"; card.className = "card";
    card.innerHTML = "<strong></strong><span></span>";
    card.querySelector("strong").textContent = t.name;
    card.querySelector("span").textContent = t.desc;
    card.onclick = () => {
      if (t.tab) {
        switchTab(t.tab);
        if (t.qr) setQrKind(t.qr);
        return;
      }
      const ta = $("#note");
      if (ta.value.trim() && !confirm("Replace the note you're writing?")) return;
      ta.value = t.text;
      saveDraft();
      switchTab("note");
      const blank = ta.value.indexOf("- [ ] \n");
      const pos = blank >= 0 ? blank + 6 : ta.value.length;
      ta.focus(); ta.setSelectionRange(pos, pos);
    };
    box.append(card);
  }
}

function setQrKind(kind) {
  state.qrKind = kind;
  $$("#qr-kind button").forEach(b => b.classList.toggle("active", b.dataset.kind === kind));
  $$("[data-qr]").forEach(el => { el.hidden = el.dataset.qr !== kind; });
  schedulePreview(0);
}

// ------------------------------------------------------------------ panels

function openPanel(name) {
  $("#scrim").hidden = false;
  $$(".panel").forEach(p => { p.hidden = p.id !== "panel-" + name; });
  if (name === "history") loadHistory();
  if (name === "automate") renderAutomate();
  if (name === "settings") loadSettings();
}

// ------------------------------------------------------------------ settings (stored on the server)

const SETTING_INPUTS = {          // input id -> [server setting, kind]
  "pad-top": ["padding_top_mm", "number"],
  "pad-bottom": ["padding_bottom_mm", "number"],
  "photo-look": ["photo_look", "text"],
  "cam-style": ["camera_style", "text"],
  "cam-detail": ["camera_detail", "number"],
  "cam-weight": ["camera_weight", "number"],
  "cam-frame": ["camera_frame", "bool"],
  "cam-sideways": ["camera_sideways", "bool"],
  "cam-zoom": ["camera_zoom", "bool"],
  "cam-sharpen": ["camera_sharpen", "bool"],
  "cam-caption": ["camera_caption", "text"],
};

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    for (const [id, [key, kind]] of Object.entries(SETTING_INPUTS)) {
      if (kind === "bool") $("#" + id).checked = !!s[key];
      else $("#" + id).value = s[key];
    }
  } catch (err) { toast(err.message, "error"); }
  refreshCameraPreview();
}

// The newest photo printed with the camera settings as they stand: changes show as you make them.
let camPreviewSeq = 0;
async function refreshCameraPreview() {
  const seq = ++camPreviewSeq;
  const img = $("#cam-preview"), meta = $("#cam-preview-meta");
  try {
    const { photos } = await api("/api/camera/photos");
    if (!photos.length) return;
    meta.textContent = "Drawing the preview…";
    const res = await api(`/api/camera/photo/${encodeURIComponent(photos[0].name)}/preview`, { method: "POST", body: {}, raw: true });
    const blob = await res.blob();
    if (seq !== camPreviewSeq) return;
    if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
    img.hidden = false;
    meta.textContent = `Your newest photo with these settings · ${res.headers.get("X-Print-Length-Mm")} mm of paper`;
  } catch (err) {
    if (seq === camPreviewSeq) meta.textContent = err.message;
  }
}

let settingsTimer = 0;
function saveSettingsSoon() {
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(async () => {
    const body = {};
    for (const [id, [key, kind]] of Object.entries(SETTING_INPUTS)) {
      const el = $("#" + id);
      if (kind === "bool") { body[key] = el.checked; continue; }
      if (kind === "text") { body[key] = el.value; continue; }
      const v = el.value.trim();
      if (v === "") return;
      body[key] = Number(v);
    }
    try {
      await api("/api/settings", { method: "POST", body });
      toast("Saved");
      refreshCameraPreview();
      state.lastDoc = "";        // the preview includes the padding: redraw it
      schedulePreview(0);
    } catch (err) { toast(err.message, "error"); loadSettings(); }
  }, 500);
}

function stepSetting(id, by) {
  const input = $("#" + id);
  const next = Math.max(Number(input.min), Math.min(Number(input.max), (Number(input.value) || 0) + by));
  input.value = next;
  saveSettingsSoon();
}
function closePanels() { $("#scrim").hidden = true; $$(".panel").forEach(p => { p.hidden = true; }); }

function timeAgo(t) {
  const s = Math.round(Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function loadHistory() {
  const list = $("#history-list");
  try {
    const { jobs } = await api("/api/jobs");
    list.replaceChildren();
    if (!jobs.length) { list.innerHTML = '<div class="empty">Nothing printed yet</div>'; return; }
    for (const j of jobs) {
      const row = document.createElement("div");
      row.className = "job";
      row.innerHTML = '<img loading="lazy" alt=""><div><div class="title"></div><div class="sub"></div></div><button type="button" class="ghost small">Reprint</button>';
      row.querySelector("img").src = j.preview;
      row.querySelector(".title").textContent = j.title || "Print";
      const sub = row.querySelector(".sub");
      sub.textContent = `${timeAgo(j.created)} · ${j.length_mm} mm · ${j.source}` + (j.copies > 1 ? ` · ${j.copies} copies` : "") + " · ";
      const st = document.createElement("span");
      st.textContent = j.status === "failed" || j.status === "waiting" ? `${j.status}: ${j.error || ""}` : j.status;
      if (j.status === "failed" || j.status === "waiting") st.className = "failed";
      sub.append(st);
      row.querySelector("button").onclick = async () => {
        try {
          const { job } = await api(`/api/jobs/${j.id}/reprint`, { method: "POST" });
          state.printing = job.id; showProgress(0); pollStatus(true); toast("Reprinting");
        } catch (err) { toast(err.message, "error"); }
      };
      list.append(row);
    }
  } catch (err) {
    list.innerHTML = '<div class="empty"></div>';
    list.firstChild.textContent = err.message;
  }
}

function renderAutomate() {
  const base = location.origin;
  const tokenNote = store.get("token", "") ? ' -H "Authorization: Bearer YOUR_TOKEN"' : "";
  const sections = [
    ["Print a note from any computer", "Plain text, with the same formatting as the Note tab.",
      `curl${tokenNote} --data-binary "Hello from curl" ${base}/api/print/text`],
    ["PowerShell", "Works on Windows without installing anything.",
      `Invoke-RestMethod -Method Post -Uri ${base}/api/print/text -Body "Hello from PowerShell"`],
    ["Print a picture", "Any PNG or JPEG. Options: dither=floyd|atkinson|threshold, rotate=90, brightness, contrast, caption.",
      `curl --data-binary @photo.jpg "${base}/api/print/image?dither=atkinson&caption=Hi"`],
    ["Print a full layout (JSON)", "Mix text, pictures (base64), QR codes, lists and lines.",
      `curl -H "Content-Type: application/json" -d '{"blocks":[{"type":"heading","text":"Hi"},{"type":"qr","data":"https://example.com"}]}' ${base}/api/print`],
    ["Home Assistant", "Add to configuration.yaml, restart, then call the action rest_command.print_note with a message.",
      `rest_command:\n  print_note:\n    url: "${base}/api/print/text"\n    method: post\n    content_type: "text/plain; charset=utf-8"\n    payload: "{{ message }}"`],
    ["Home Assistant automation example", "Prints a note when the dryer finishes.",
      `actions:\n  - action: rest_command.print_note\n    data:\n      message: "# Laundry\\nThe dryer finished at {time}"`],
    ["Wait until it's printed", "Add ?wait=1 and the call returns after printing (status 200), or with the error.",
      `curl --data-binary "Done!" "${base}/api/print/text?wait=1"`],
    ["Check the printer", "State, queue and version as JSON.", `curl ${base}/api/status`],
  ];
  const box = $("#automate");
  box.replaceChildren();
  const intro = document.createElement("p");
  intro.textContent = "Everything in this app is also an HTTP API, so scripts, Home Assistant and other apps can print.";
  box.append(intro);
  for (const [title, desc, code] of sections) {
    const h = document.createElement("h3"); h.textContent = title;
    const p = document.createElement("p"); p.textContent = desc;
    const wrap = document.createElement("div"); wrap.className = "code";
    const pre = document.createElement("pre"); pre.textContent = code;
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "ghost small"; copy.textContent = "Copy";
    copy.onclick = () => navigator.clipboard.writeText(code).then(() => toast("Copied"), () => toast("Couldn't copy", "error"));
    wrap.append(pre, copy);
    box.append(h, p, wrap);
  }
}

// ------------------------------------------------------------------ wiring

function saveDraft() { store.set("note", $("#note").value); }

function init() {
  renderTemplates();
  $("#note").value = store.get("note", "");
  // "darkness2": the old "darkness" key held Normal from before Auto existed (2026-10-30), which would pin photos
  // to Normal; everyone starts on Auto
  $("#darkness").value = store.get("darkness2", "auto");
  $("#note-font").value = store.get("font", "sans");
  $("#note-size").value = store.get("size", "24");

  $$(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $$("[data-insert]").forEach(b => b.addEventListener("click", () =>
    b.hasAttribute("data-inline") ? insertInline(b.dataset.insert) : insertAtLineStart(b.dataset.insert)));
  $$("[data-wrap]").forEach(b => b.addEventListener("click", () => wrapLine(b.dataset.wrap)));
  $("#note").addEventListener("input", () => { saveDraft(); schedulePreview(); });
  $("#note-font").addEventListener("change", e => { store.set("font", e.target.value); schedulePreview(0); });
  $("#note-size").addEventListener("change", e => { store.set("size", e.target.value); schedulePreview(0); });
  $("#darkness").addEventListener("change", e => { store.set("darkness2", e.target.value); state.lastDoc = ""; schedulePreview(0); });

  // every other input re-renders the preview
  $$(".pane input, .pane select, .pane textarea").forEach(el => {
    if (el.id === "note" || el.id === "pic-file") return;
    el.addEventListener(el.type === "range" ? "change" : "input", () => schedulePreview(el.type === "text" ? 300 : 50));
  });
  $$("input[type=range]").forEach(el => el.addEventListener("input", () => schedulePreview(150)));

  $("#pic-file").addEventListener("change", e => loadPicture(e.target.files[0]));
  const showDrawingOptions = () => {
    const style = $("#pic-dither").value;
    const drawing = $("#pic-dither").selectedOptions[0]?.parentElement?.label === "Drawing" ||
      style === "photo" || style === "photoline";
    $$("[data-drawing]").forEach(el => { el.hidden = !drawing; });
    store.set("picstyle", $("#pic-dither").value);
  };
  $("#pic-dither").value = store.get("picstyle", "photo");
  if (!$("#pic-dither").value) $("#pic-dither").value = "photo";
  $("#pic-dither").addEventListener("change", showDrawingOptions);
  showDrawingOptions();
  $("#pic-clear").addEventListener("click", e => { e.preventDefault(); clearPicture(); });
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach(ev => document.addEventListener(ev, e => {
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); drop.classList.add("over"); }
  }));
  ["dragleave", "drop"].forEach(ev => document.addEventListener(ev, () => drop.classList.remove("over")));
  document.addEventListener("drop", e => {
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) { e.preventDefault(); loadPicture(file); }
  });
  document.addEventListener("paste", e => {
    const item = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith("image/"));
    if (item) { e.preventDefault(); loadPicture(item.getAsFile()); }
  });

  $$("#qr-kind button").forEach(b => b.addEventListener("click", () => setQrKind(b.dataset.kind)));
  $("#print-btn").addEventListener("click", doPrint);
  document.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doPrint(); }
    if (e.key === "Escape") closePanels();
  });
  $("#status").addEventListener("click", checkPrinter);
  $("#feed-btn").addEventListener("click", async () => {
    try { await api("/api/feed?lines=3", { method: "POST" }); } catch (err) { toast(err.message, "error"); }
  });
  $$("[data-panel]").forEach(b => b.addEventListener("click", () => openPanel(b.dataset.panel)));
  Object.keys(SETTING_INPUTS).forEach(id => {
    $("#" + id).addEventListener("input", saveSettingsSoon);
    $("#" + id).addEventListener("change", saveSettingsSoon);
  });
  $$("[data-step]").forEach(b => b.addEventListener("click", () => stepSetting(b.dataset.step, Number(b.dataset.by))));
  $$("[data-close]").forEach(b => b.addEventListener("click", closePanels));
  $("#scrim").addEventListener("click", closePanels);

  pollStatus();
  schedulePreview(0);
}

init();
