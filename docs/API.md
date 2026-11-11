# HTTP API

Everything the web app does goes through this API, so scripts, Home Assistant or anything that can send an HTTP
request can print. The server listens on port 8500.

- Print calls answer right away with the job (`202`). Add `?wait=1` to answer after printing (`200`, or `502` with
  the error).
- Every print call takes `darkness` (`light`, `normal`, `dark`, `darkest`, 0–255, or `auto`), `copies` and `feed`.
- With [more than one printer](../README.md#a-second-printer-phomemo-m02-pro), every print call also takes
  `printer`: an `id` from `/api/status` (`ctp500`, `m02pro`) or its name. Without it a print goes to the main
  printer. Each printer has its own queue.
- With a token set (`python3 -m ctp500 config token=...`), send `Authorization: Bearer <token>` or add `?token=`.
- Browser requests from other web sites are refused, so a page you visit can't print on your printer.

## Printing

| Call | Body |
|---|---|
| `POST /api/print/text` | plain text with [note formatting](../README.md#note-formatting) (`?size=`, `?font=`, `?markup=0` for exact text) |
| `POST /api/print/image` | a PNG/JPEG file (`?style=photo\|photoline\|floyd\|atkinson\|threshold\|line\|artist\|sketch\|coloring\|poster\|halftone\|hatch`, `detail=0..1`, `weight=1..3`, `rotate=90`, `brightness`, `contrast`, `width`, `caption`) |
| `POST /api/print` | a JSON layout (below) |
| `POST /api/preview` | the same JSON; answers with the PNG it would print, prints nothing |
| `POST /api/feed?lines=3` | feed paper (`&printer=`) |
| `GET /api/status` | `printer` (the main one), `printers` (all of them: `id`, `label`, `model`, `width`, `dpi`, `paper_mm`, state), `camera_printer`, queue, version, PC helper |
| `GET /api/settings`, `POST /api/settings` | padding (`padding_top_mm`, `padding_bottom_mm`, 0–50), `photo_look` (`punchy`/`even`), camera print settings, `camera_printer` (a printer id, or `""` for the main one) |
| `POST /api/check` | connect to the printer now (`?printer=`) |
| `GET /api/jobs`, `GET /api/jobs/<id>.png`, `POST /api/jobs/<id>/reprint`, `POST /api/jobs/<id>/cancel` | history; each job says which `printer` it went to, and a reprint goes there too (or `?printer=` another) |

```sh
curl --data-binary "# Hello" http://<server>:8500/api/print/text
curl --data-binary @photo.jpg "http://<server>:8500/api/print/image?style=photo"
curl --data-binary "Sticker" "http://<server>:8500/api/print/text?printer=m02pro"
```

```powershell
Invoke-RestMethod -Method Post -Uri http://<server>:8500/api/print/text -Body "Hello from PowerShell"
```

## JSON layouts

```json
{"blocks": [
  {"type": "heading", "text": "Hi", "level": 1},
  {"type": "text", "text": "Any text", "size": 24, "font": "sans", "bold": false, "align": "left", "invert": false},
  {"type": "image", "data": "<base64>", "style": "line", "detail": 0.5, "weight": 2, "width": 384, "rotate": 0, "brightness": 1.0, "contrast": 1.0},
  {"type": "qr", "data": "https://example.com", "size": 260, "caption": "Scan me"},
  {"type": "row", "left": "Coffee", "right": "$3.50", "leader": true},
  {"type": "list", "items": ["one", "two"]},
  {"type": "checklist", "items": ["todo", {"text": "done", "done": true}]},
  {"type": "line", "style": "solid", "thickness": 2},
  {"type": "space", "height": 24},
  {"type": "banner", "text": "WOW"},
  {"type": "markup", "markup": "## Note formatting\n- [ ] works here too"}
], "darkness": "normal", "copies": 1}
```

A layout can also set `padding_top_mm` and `padding_bottom_mm` (otherwise the server's settings apply).
Shorthands: `{"text": "..."}`, `{"markup": "..."}`, or a bare list of blocks. Sizes are in CTP500 dots (8 dots =
1 mm; the paper is 384 dots wide). On the M02 Pro (576 dots at 300 dpi) everything is drawn 1.5 times as many dots,
so a layout comes out the same size on either printer; `/api/preview` answers with that printer's dots.

## Photos

| Call | |
|---|---|
| `POST /api/camera/upload` | raw JPEG body, `X-Filename` header: adds it to the gallery (the camera's sync) |
| `POST /api/camera/print` | the same, and prints it with the camera print settings (on `camera_printer`); answers as soon as it's queued |
| `GET /api/camera/photos` | every photo, newest first, each with `taken` |
| `GET /api/camera/photo/<name>` (`?download=1`), `/api/camera/thumb/<name>`, `/api/camera/photos.zip` | the original, a thumbnail, all of them |
| `POST /api/camera/photo/<name>/print` | print it (`style`, `detail`, `weight`, `frame`, `caption`, `sideways`, `zoom`, `sharpen`, `printer` override the settings) |
| `POST /api/camera/photo/<name>/preview` | the PNG it would print (same options) |
| `POST /api/camera/photo/<name>/delete` | delete it from the server |
| `GET /api/camera/litho/<name>.png`, `.stl`, `.3mf` | lithophane preview, 3D model, ready-to-print file (needs the PC helper); `?size=` long side in mm (30–170), `?shape=flat\|curved\|relief` |

## PC helper

| Call | |
|---|---|
| `GET /api/helper/next?kinds=draw,restore,slice&name=PC` | the helper's long poll: a job (`X-Job-Id`, `X-Job-Kind`, `X-Job-Params`, body), or 204 after 25 s |
| `POST /api/helper/result/<id>` | its answer: the body, `X-Job-Meta` (JSON) or `X-Job-Error` |

## Home Assistant

```yaml
rest_command:
  print_note:
    url: "http://<server>:8500/api/print/text"
    method: post
    content_type: "text/plain; charset=utf-8"
    payload: "{{ message }}"
```

Then any automation can use the action `rest_command.print_note` with
`message: "# Laundry\nThe dryer finished at {time}"`. For printer alerts the other way round, set the server's
`notify_url` to a Home Assistant webhook: it gets `{"title": ..., "message": ...}` when prints wait (out of paper,
lid open, printer off) and when they go out after all.
