# Portable print box (ESP32-S3)

For printing away from home, where the print server isn't. An ESP32-S3 (16 MB flash, 8 MB PSRAM, "N16R8") on a
USB power bank makes its own Wi-Fi network and prints on the CTP500 over **BLE**. The camera and a phone join it.

```
 camera / phone ──Wi-Fi (BOX_SSID)──▶ ESP32-S3 print box ──BLE──▶ CTP500
```

Why BLE: the ESP32-S3 has no Classic Bluetooth. Cat-protocol CTP500s ("Mini Printer-…") also advertise BLE service
AF30 and take the same cat-printer packets as over Classic: write to AE01 (service AE30), replies and flow control on
AE02 (tested 2026-10-30 from a Pi with bleak, then from the ESP32).

## Using it

- Power the ESP32 from a USB power bank, next to the printer (switched on).
- **Phone:** join the box's Wi-Fi (Android may warn it has no internet: stay connected), open
  **http://192.168.4.1**, Take photo / Choose photo, Print (3 s to tap again and cancel).
- **Camera:** nothing to do. When home Wi-Fi isn't in range it joins the box and prints there (settings.toml
  `CAMERA_AWAY_SSID`, `CAMERA_AWAY_PASSWORD`, `CAMERA_AWAY_PRINTER`). At home it tries the server first; if that
  doesn't answer in 6 s (switched off), it says "Print box...", switches to the box's Wi-Fi and prints there, and for
  the next 10 minutes goes straight to the box. Away prints aren't marked synced, so the photos still go to the
  server later.
- The onboard RGB LED (if the board has one on GPIO 48): green ready, blue printing, red last print failed.

Prints look like the server's camera prints with the punchy Photo style: dehaze, autocontrast, sharpen, gamma 0.8,
Atkinson, landscape shots sideways, 6 mm paper above and below, normal darkness. No AI outlines (the server only), no
captions or notes yet.

## API (same calls as the server where they overlap)

| Call | |
|---|---|
| `POST /api/camera/print` | raw JPEG body (the camera's call), `X-Filename`; answers 200 once queued |
| `POST /api/print/photo` | the same, from the phone page |
| `POST /api/camera/upload` | 503 on purpose: the camera's sync waits for the server |
| `GET /api/status` | `{"printer": "connected"/"ready"/"not found yet"/"out of paper or lid open", "printing", "queued", "last"}` |

## Building

1. Copy `printbox/secrets.example.h` to `printbox/secrets.h` and pick the box's Wi-Fi name and password (the camera's
   `CAMERA_AWAY_*` settings use the same).
2. Arduino core esp32 3.3.x, libraries **NimBLE-Arduino** 2.5 and **JPEGDEC** 1.8 (Library Manager).
3. Board: ESP32S3 Dev Module, Flash 16 MB, PSRAM OPI, USB CDC on boot, partition scheme "16M Flash (3MB APP/9.9MB
   FATFS)". With arduino-cli:

```sh
FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB"
arduino-cli compile -b "$FQBN" --output-dir build/printbox printbox
arduino-cli upload -b "$FQBN" -p COM6 --input-dir build/printbox
```

Serial log at 115200 on the board's USB: decode time, haze/dehaze, printer found, each print's result.
`bletest/` is the first BLE test (connect, status, a strip of bars; set your printer's address in it).

## Measured (2026-10-30)

- A MEMENTO JPEG 1024x768: decoded at 1/2 in ~125 ms, drawn in 3.0 s, first print 34 s (8 s BLE search for the
  printer, then remembered in NVS), next print 28 s; the printer's own feed is most of it. The upload answers in
  0.06 s.
- BLE writes are paced (112 bytes, 20 ms apart): one 5 KB burst overflowed the printer and never finished.
- A full-time BLE scan starved the box's Wi-Fi (a laptop dropped off), so the scan listens half the time.
- The first photo code ran 3+ s solid on one core (column-wise passes over PSRAM); on a phone photo the task
  watchdog rebooted the box mid-print, which also dropped its Wi-Fi. Now passes run row by row, the dehaze's wide
  blurs use a quarter-size grid, and the work pauses a tick every 16 rows: a 960x1280 phone photo draws in 2.2 s
  (decode 0.28, resize 0.39, dehaze 0.63, tones+dither 0.83).
- Camera end to end, away from home Wi-Fi: "wifi up (away)" in 4.5 s (scan + connect), photo sent in 0.9 s, camera
  ready again 8.9 s after the shot. Back home: the camera picked home Wi-Fi, its sync sent the away photo to the
  server, and the next shot went to the server. An away shot right after a power-on has a counter name (no internet
  on the box to set the clock); the server dates it by arrival.
- Server switched off at home: home Wi-Fi 2.3 s, server timeout 6 s, box Wi-Fi 1.9 s, sent in 1.0 s, camera ready
  14.8 s after the shot.
- CircuitPython 10.3.1 on this board could scan but not connect to the printer (its ESP32 BLE central mode has open
  bugs: circuitpython#9337, #10801, #9708), hence Arduino.
