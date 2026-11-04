# Camera app (Adafruit MEMENTO)

Turns an [Adafruit MEMENTO](https://www.adafruit.com/product/5420) into an instant camera for the CTP500: take a
photo and it prints, as a thermal-tuned photo with outlines, on the print server's printer. Every photo is also
saved on the camera's microSD card and synced to the server's photo gallery.

**You need:** the MEMENTO board (Adafruit product 5420: ESP32-S3 with an OV5640 5 MP autofocus camera and a 1.54"
screen), a microSD card (32 GB or less, FAT32), a LiPo battery (or USB power), and a print server
([set up first](../README.md#quick-start)). Optional: the [print box](../esp32/README.md) for printing away from home.
Tested with CircuitPython 10.3.1 and the 2026-10-30 library bundle (adafruit_pycamera 1.5.7).

## Set it up

1. **CircuitPython:** install CircuitPython 10 on the MEMENTO as Adafruit's MEMENTO guide shows (double-tap reset,
   drop the `.uf2` on the boot drive). A drive called `CIRCUITPY` appears.
2. **The app:** with the camera plugged in, on any computer with this repo and Python:

   ```sh
   pip install circup            # installs the CircuitPython libraries for you
   python camera/install.py      # asks for your Wi-Fi and the print server's address
   ```

   It writes `settings.toml` on the camera (only if it has none yet), installs the libraries, and copies
   `code.py`. The camera restarts by itself.
3. Put the microSD card in (power off first), switch on, take a photo.

**By hand instead:** copy `settings.toml.example` to the drive as `settings.toml` and fill it in; copy these
libraries from the [CircuitPython bundle](https://circuitpython.org/libraries) to the drive's `lib` folder:
`adafruit_pycamera`, `adafruit_bitmap_font`, `adafruit_display_text`, `adafruit_bus_device`, `adafruit_register`,
`adafruit_aw9523`, `adafruit_lis3dh`, `adafruit_debouncer`, `adafruit_ticks`, `adafruit_connection_manager`,
`adafruit_requests`, `adafruit_ntp`, `adafruit_pixelbuf`, `neopixel`; then copy `code.py` to the drive.

**Updating:** run `python camera/install.py` again (your settings stay), or copy the new `code.py` over.

## Using it

| Button | |
|---|---|
| Shutter (tap) | take a photo (and print it, in PRINT mode) |
| Shutter (hold) | open / close the gallery |
| Up (hold) | PRINT mode on / off (the screen shows PRT instead of REC) |
| OK (hold) | sync new photos to the server now (OK is the left button under the screen) |
| Left | refocus |
| Right, then Up / Down | pick a setting (resolution, effect) and change it |
| In the gallery | Left / Right browse, **tap OK** prints the photo on screen, hold OK deletes it |

- **PRINT mode** (on at power-up with `INSTANT_PRINT = 1`): every shot prints right away. How it prints (style,
  outlines, frame, caption, sideways) is in the web app's **Settings**, with a preview of your newest photo. A print
  shot autofocuses and uses a faster shutter, so people moving don't smear.
- **Sync:** photos upload to the server's gallery when you hold OK, while the camera is plugged into a computer, and
  with each print at home (up to 5 waiting photos at a time).
- **Clock:** set from the internet whenever the camera is online, so photos are named by when they were taken.
- **Away from home:** with the `CAMERA_AWAY_*` settings it prints on the [print box](../esp32/README.md) when home
  Wi-Fi isn't in range, or when the server doesn't answer within 6 s; those photos sync once it's home.
- **Battery:** the screen dims after `IDLE_DIM_AFTER` seconds without a button press; Wi-Fi is only on while
  sending.

## Settings (settings.toml)

| Setting | |
|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | home Wi-Fi |
| `CAMERA_SERVER` | where photos sync: `http://<server>:8500/api/camera/upload` |
| `CAMERA_PRINTER` | where shots print: `http://<server>:8500/api/camera/print` |
| `INSTANT_PRINT` | `1` = PRINT mode at power-up |
| `CAMERA_AWAY_SSID`, `CAMERA_AWAY_PASSWORD`, `CAMERA_AWAY_PRINTER` | the print box (its name and password from its `secrets.h`; `http://192.168.4.1/api/camera/print`) |
| `TZ_OFFSET`, `TZ_DST` | hours from UTC in winter (e.g. `-5`), and `"us"` to add the summer hour on the US dates |
| `CAMERA_NAME` | the name on the start-up screen (or put a 240x240 24-bit `splash.bmp` on the drive) |
| `IDLE_DIM_AFTER`, `DEEP_SLEEP_AFTER` | seconds before the screen dims / it sleeps (0 = never) |
| `CAMERA_DENOISE`, `CAMERA_SHARPNESS`, `CAMERA_AWB` | sensor tuning: 0-8, -3..3, `"simple"` white balance for colored LED light |

If the server has a token, end both URLs with `?token=<token>` (`install.py` does this for you).

## Troubleshooting

- **"No SD":** the card's contacts are touchy. The app tries the card again at power-up and before each shot and
  retries a failed save; if it keeps coming back, re-seat the card or try another one.
- **It froze while starting:** a watchdog restarts it after 30 s by itself.
- **Nothing prints:** the serial console (115200 baud on the camera's USB port, e.g. `screen` or PuTTY) logs
  `wifi up`, `print: sent in ...` or the error. Check the URLs in settings.toml open in a browser on your Wi-Fi.
- **Dark or pink photos indoors:** the sensor is small; more white light helps most. A stuck bright pixel can be
  painted out on the server: set `camera_stuck_pixel` (see the main README's Settings).
