# PC helper (Windows, optional)

The print server runs fine on its own. While a Windows PC runs this helper, the server hands it the heavy jobs:

| Job | On the helper | Server alone (Pi 5) |
|---|---|---|
| AI drawings (line art, sketches, Photo + outlines) | 0.05 s on an NVIDIA card, ~0.5 s on the processor | ~2.5 s |
| A new photo's print preview | ~1 s | ~3 s |
| Sharpen faces (GFPGAN 1.4) | ~0.2 s a photo, NVIDIA card | not available |
| Ready-to-print lithophanes (sliced by Bambu Studio for an A1 mini) | ~5 s | not available (the STL still is) |

The helper only calls out to the server (no firewall rule needed). When the PC is off or asleep, the server does
everything itself again.

## Set it up

Needs Python 3.10–3.12 from [python.org](https://www.python.org/downloads/windows/) (or
[uv](https://docs.astral.sh/uv/)). In PowerShell, from this repo:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\install.ps1 -Server http://raspberrypi.local:8500
```

It saves the server's address in `desktop\helper.json`, makes the helper's own Python environment, installs what
it needs (with an NVIDIA card: PyTorch's CUDA libraries + onnxruntime-gpu, about 3 GB, once), downloads the AI
models and adds the scheduled task **CTP500 helper** (starts at logon, no window, below-normal priority). The
server's status shows it: `GET /api/status` → `"helper": {"online": true, ...}`.

| Option | |
|---|---|
| `-HelperHome D:\ctp500-helper` | where its Python, models and log go (default `%LOCALAPPDATA%\ctp500-helper`) |
| `-Cpu` | no NVIDIA card (or skip the 3 GB): drawings run on the processor, still faster than a Pi |
| `-NoTask` | set up only; run `<home>\.venv\Scripts\python.exe desktop\worker.py` yourself |

If the server has a token, add `"token": "..."` to `desktop\helper.json`. Run `install.ps1` again after updating
the repo. The log is `<home>\worker.log`. To remove it: `Unregister-ScheduledTask "CTP500 helper"`, then delete
the home folder.

**Ready to print** needs [Bambu Studio](https://bambulab.com/en/download/studio) installed in its default folder (or
set `"bambu_studio"` in helper.json). Every sliced file is checked before it's handed over: the A1 mini's own start
G-code must be there and every extruding move must stay on its 180 x 180 mm bed.
