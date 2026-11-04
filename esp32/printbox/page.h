// The phone page: take or choose a photo, see it, print it (with the same 3 s cancel window as the Pi's Photos page).
#pragma once

static const char PAGE_HTML[] = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print box</title>
<style>
:root{--bg:#f6f4ef;--fg:#1c1c1a;--muted:#6b6a64;--accent:#1f6f5c;--card:#fff;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161715;--fg:#ecebe6;--muted:#a3a29c;--card:#22231f;--accent:#4fb39a;--bad:#f2b8b5}}
*{box-sizing:border-box}body{margin:0;font:16px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:520px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}.status{color:var(--muted);font-size:14px;min-height:20px}
.status.bad{color:var(--bad)}
.buttons{display:flex;gap:10px;margin:16px 0}
button,label.btn{flex:1;border:0;border-radius:12px;padding:14px;font:600 16px system-ui;background:var(--card);color:var(--fg);
 text-align:center;box-shadow:0 1px 3px #0002;cursor:pointer}
button.primary{background:var(--accent);color:#fff}button:disabled{opacity:.5}
.photo{background:var(--card);border-radius:12px;padding:10px;box-shadow:0 1px 3px #0002}
.photo img{width:100%;border-radius:8px;display:block}
input[type=file]{display:none}.hint{color:var(--muted);font-size:13px;margin-top:14px}
</style></head><body><main>
<h1>Print box</h1>
<div class="status" id="status">Checking the printer...</div>
<div class="buttons">
 <label class="btn">Take photo<input type="file" id="cam" accept="image/*" capture="environment"></label>
 <label class="btn">Choose photo<input type="file" id="pick" accept="image/*"></label>
</div>
<div class="photo" id="photo" hidden><img id="preview" alt="">
 <div class="buttons"><button class="primary" id="print">Print</button></div></div>
<p class="hint">Landscape photos print sideways, as big as the paper allows. The camera prints here by itself when
it's away from home Wi-Fi.</p>
</main><script>
"use strict";
const $ = s => document.querySelector(s);
let photo = null, timer = null, left = 0;
async function shrink(file) {   // phone photos are ~12 MP; 1280 px is plenty for 48 mm of paper
  let src;
  try { src = await createImageBitmap(file, {imageOrientation: "from-image"}); }
  catch { src = await new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = URL.createObjectURL(file); }); }
  const w = src.width || src.naturalWidth, h = src.height || src.naturalHeight, s = Math.min(1, 1280 / Math.max(w, h));
  const c = document.createElement("canvas");
  c.width = Math.round(w * s); c.height = Math.round(h * s);
  const ctx = c.getContext("2d"); ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(src, 0, 0, c.width, c.height);
  return new Promise(ok => c.toBlob(ok, "image/jpeg", 0.9));
}
async function chosen(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  cancel(true);
  setStatus("Getting the photo ready...");
  photo = await shrink(f);
  $("#preview").src = URL.createObjectURL(photo);
  $("#photo").hidden = false;
  refresh();
}
function cancel(quiet) {
  if (!timer) return;
  clearInterval(timer); timer = null; $("#print").textContent = "Print";
  if (!quiet) setStatus("Print cancelled");
}
function printClicked() {
  if (timer) { cancel(); return; }   // a second tap cancels
  left = 3; $("#print").textContent = "Printing in 3... tap to cancel";
  timer = setInterval(() => {
    if (--left > 0) { $("#print").textContent = `Printing in ${left}... tap to cancel`; return; }
    cancel(true); send();
  }, 1000);
}
async function send() {
  $("#print").disabled = true; setStatus("Sending...");
  try {
    const r = await fetch("/api/print/photo", {method: "POST", body: photo,
      headers: {"Content-Type": "image/jpeg", "X-Filename": "phone.jpg"}});
    const j = await r.json().catch(() => ({}));
    setStatus(r.ok ? "Printing..." : (j.error || "The print box said no"), !r.ok);
  } catch { setStatus(OFFLINE, true); }
  $("#print").disabled = false;
}
function setStatus(text, bad) { const s = $("#status"); s.textContent = text; s.className = "status" + (bad ? " bad" : ""); }
async function refresh() {
  try {
    const s = await (await fetch("/api/status")).json();
    if (timer) return;
    const p = s.printing ? "printing" : s.queued ? `${s.queued} waiting` : "";
    setStatus(`Printer: ${s.printer}${p ? " · " + p : ""}${s.last ? " · last: " + s.last : ""}`,
      /out of paper|can't|couldn't|didn't/i.test(s.printer + " " + s.last));
  } catch { setStatus(OFFLINE, true); }
}
// The box's Wi-Fi has no internet, so Android may keep sending over mobile data and never reach it
const OFFLINE = "Can't reach the print box. If this phone has mobile data on, turn it off, or tap the Wi-Fi " +
  "\"no internet\" notification and choose to stay connected, then try again.";
$("#cam").onchange = chosen; $("#pick").onchange = chosen; $("#print").onclick = printClicked;
refresh(); setInterval(refresh, 3000);
</script></body></html>
)HTML";
