# SPDX-License-Identifier: MIT
"""
CTP500 camera app - an instant camera for the Adafruit MEMENTO (ESP32-S3 + OV5640
autofocus camera board) that prints on a CTP500 thermal printer.

Built on Adafruit's PyCamera library (CircuitPython 9.x / 10.x). Keeps the
stock camera experience - live preview + the built-in settings HUD - and adds:

  * XGA (1024x768) / Normal defaults, sensor tuned for natural color (saturation
    0, lens shading, pixel clean-up) and continuous autofocus; everything still
    adjustable from the on-camera settings menu.
  * Capture saves a JPEG to the SD card named by when it was taken
    (IMG_YYYYMMDD_HHMMSS.jpg once the clock is set by a sync, else IMGxxxxx.jpg).
  * On-device GALLERY (long-press shutter) to review and delete shots.
  * WiFi SYNC: queues shots on the SD card and uploads any not-yet-sent ones
    to your home server whenever the camera is plugged into USB (i.e. charging
    or on the bench). The radio only comes up during a sync, to save battery.
  * PRINT: sends a shot to the CTP500 print server (CAMERA_PRINTER; away from
    home Wi-Fi, the portable print box: CAMERA_AWAY_*), which
    prints it the way its Settings say (photo + outlines, sideways, by
    default). PRINT mode prints every shot right after it's taken; the
    gallery prints the one on screen.

Controls
  Shutter (tap) ............ take a photo (and print it, in PRINT mode). Focus is
                             continuous, so the photo is taken ~0.5 s after the
                             press; the click plays at that moment.
  Shutter (hold) ........... open / close the gallery
  OK (hold) ................ sync to the server now over WiFi (works on battery);
                             OK is the left button under the screen. A print at
                             home also sends up to CATCH_UP_MAX waiting photos.
  Up (hold) ................ PRINT mode on / off (top-left shows PRT instead of REC)
  Left ..................... refocus the lens (capture itself stays instant)
  Right .................... pick a setting (resolution, effect)
  Up / Down ................ change the selected setting
  In gallery: Left/Right = browse, Shutter-hold = exit, OK (tap) = print this
              photo, OK-hold = delete

Setup
  1. Install CircuitPython on the MEMENTO, then copy the adafruit_pycamera
     library + its deps to /lib (use the Project Bundle or circup).
  2. Format an SD card <=32GB as FAT32 and insert it (power off first).
  3. Put your WiFi + server URL in settings.toml (see the included template).
  4. Drop this file on the CIRCUITPY drive as code.py.

NOTE: button API verified against adafruit_pycamera 1.5.7 (shutter is a
debouncer Button with .short_count/.long_press; the d-pad/OK/SELECT are plain
Debouncers with .fell/.rose/.value/.current_duration). Settings reset to
XGA / Normal on every power-on; change them in the menu for the session.
"""

import os
import time
import gc

import rtc
import board
import microcontroller
import espcamera
import wifi
import alarm
import socketpool
import supervisor
import storage
import displayio
import vectorio
import neopixel
import digitalio
import terminalio
import adafruit_ntp
from analogio import AnalogIn
from jpegio import JpegDecoder
from displayio import Bitmap, TileGrid, ColorConverter, Colorspace, Group
from adafruit_display_text import bitmap_label

import adafruit_requests
import adafruit_pycamera

# ---------------------------------------------------------------------------
# config (from settings.toml)
# ---------------------------------------------------------------------------
SSID     = os.getenv("WIFI_SSID")        # NOT CIRCUITPY_WIFI_* on purpose, so the
PASSWORD = os.getenv("WIFI_PASSWORD")    # core doesn't auto-connect on every boot
def _setting(name):
    # CAMERA_<name>; the BABYCAM_<name> settings from before the rename (2026-11-04) still work
    value = os.getenv("CAMERA_" + name)
    return value if value is not None else os.getenv("BABYCAM_" + name)


SERVER   = _setting("SERVER")          # e.g. http://raspberrypi.local:8500/api/camera/upload
PRINTER  = _setting("PRINTER")         # e.g. http://raspberrypi.local:8500/api/camera/print
# Away from home: the portable print box (an ESP32-S3 by the printer, its own Wi-Fi network). When the home network
# isn't in range, prints go there instead; syncing waits for home (the box doesn't keep photos).
AWAY_SSID     = _setting("AWAY_SSID")       # the box's BOX_SSID
AWAY_PASSWORD = _setting("AWAY_PASSWORD")
AWAY_PRINTER  = _setting("AWAY_PRINTER")    # http://192.168.4.1/api/camera/print
NETWORKS = [n for n in (("home", SSID, PASSWORD), ("away", AWAY_SSID, AWAY_PASSWORD)) if n[1] and n[2]]

# PRINT mode at power-on (1 = print every shot); Up-hold toggles it for the session
try:
    print_mode = bool(int(os.getenv("INSTANT_PRINT") or 0)) and bool(PRINTER or AWAY_PRINTER)
except (TypeError, ValueError):
    print_mode = False

def _int_env(name):
    try:
        v = _setting(name)
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


DENOISE = _int_env("DENOISE")      # sensor noise reduction: 0 = its own (auto), 1-8 fixed
SHARPNESS = _int_env("SHARPNESS")  # sensor sharpening -3..3; unset = its own
SENSOR_TEST = _setting("SENSOR_TEST")   # "denoise:sharpness[:simple],...": test shots at power-up
# White balance: the sensor's "advanced" AWB only corrects along natural light (warm..cool), so pink/purple LED
# room light stays magenta; "simple" is gray-world and corrects any color. The driver calls the switch `dcw`
# (OV5640 0x5183 bit 7; dcw False = simple). 2026-10-31: camera shots ran B/G 1.25 vs a phone's 0.92 in the same rooms.
AWB_SIMPLE = str(_setting("AWB") or "").strip().lower() == "simple"

# Hours from UTC for photo names (the clock is set from NTP when online). With TZ_DST = "us", TZ_OFFSET is the
# standard (winter) offset and the summer hour is added on the US dates (2nd Sunday in March to 1st Sunday in
# November, 2:00 local): US Eastern is TZ_OFFSET = -5. Without it, TZ_OFFSET is used as is. (2026-11-01: summer time
# ended, the setting still said -4, and photos were named an hour ahead.)
try:
    TZ_OFFSET = float(os.getenv("TZ_OFFSET") or 0)
except (TypeError, ValueError):
    TZ_OFFSET = 0
TZ_DST = str(os.getenv("TZ_DST") or "").strip().lower()

# power saving (on battery only). seconds of no input before each step; 0 = off
try:
    IDLE_DIM_AFTER = int(os.getenv("IDLE_DIM_AFTER") or 90)     # screen+camera off
except (TypeError, ValueError):
    IDLE_DIM_AFTER = 90
try:
    DEEP_SLEEP_AFTER = int(os.getenv("DEEP_SLEEP_AFTER") or 0)  # 0 = never deep sleep
except (TypeError, ValueError):
    DEEP_SLEEP_AFTER = 0

# defaults: index 4 == 1024x768 (XGA); effect 0 == Normal; mode 0 == JPEG.
# Measured 2026-10-30: 1600x1200 gets no exposure benefit indoors (~100 ms either
# way), reads out in 224 ms vs 104 ms (more skew on movement) and makes 3-8x bigger
# files; 1024x768 is still 2.7x the printer's 384 dots. UXGA stays in the menu.
DEFAULT_RESOLUTION = 4
DEFAULT_EFFECT     = 0

MAX_RES_INDEX = 7        # 1600x1200; captures above this fail on MEMENTO (issue #1)
OK_HOLD_S     = 1.0      # seconds to hold OK for a hold-action
CATCH_UP_MAX  = 5        # after a print at home, send up to this many waiting photos (~1 s each)
AF_ON_CAPTURE = False    # True = autofocus every shot (sharper but ~0.5-1s slower);
                         # False = instant capture, focus once at boot / tap Left.
                         # PRINT mode always autofocuses: a soft photo draws badly.
JPEG_QUALITY  = 8        # espcamera 0..63, lower = better (library default 12); 1600x1200
                         # tested fine at 6 and 10 (87-135 KB)
SETTLE_S      = 0.15     # after switching to full-res JPEG, before the kept frame
                         # (exposure carries over from the preview; was 0.3)
# PRINT mode shutter cap. Indoors auto exposure sits at ~885 lines (~1/10 s) with
# the gain at its ceiling, which smears anything that moves. For a print shot the
# exposure is cut to this many lines (~1/30 s at ~106 us a line) and the gain
# raised by the same factor (max 1023 = 64x): grain instead of smear. The sensor's
# own AEC max-exposure registers (0x3A02/03) didn't limit it, so it's done by hand.
FAST_SHUTTER_LINES = 312
# Regular (REC) shots get a gentler cap, ~1/20 s: the uncapped ~1/10 s blurred a
# moving child, and 1/30 s adds more grain than a keepsake photo needs (2026-10-30).
REC_SHUTTER_LINES = 470

# Auto exposure metering: weights of the sensor's 4x4 zones (OV5640 0x5688-0x568F,
# two 4-bit zones per register; the default 0x11s weigh every zone the same).
# This weighs the middle and lower middle most, where faces are, and the top
# row least, where lamps and string lights are: with lights in the shot the
# average-everything metering darkened the whole picture for them (2026-10-30).
#   2  6  6  2
#   6 14 14  6
#  10 14 14 10
#   6 10 10  6
METERING_WEIGHTS = (0x62, 0x26, 0xE6, 0x6E, 0xEA, 0xAE, 0xA6, 0x6A)

SD       = "/sd"
MANIFEST = "/sd/.uploaded"

# ---------------------------------------------------------------------------
# watchdog: a freeze inside C code (starting the SD card or the sensor; the
# card's contacts are touchy) can't be caught in Python; it showed as "No SD"
# and a camera that wouldn't start until reset (2026-10-31). The chip's
# task watchdog restarts the camera instead; feed() runs in every loop and
# before each long step (Wi-Fi, uploads, prints). Off again when code.py ends.
# ---------------------------------------------------------------------------
WATCHDOG_S = 30
_watchdog_restart = False
try:
    from watchdog import WatchDogMode
    # reset_reason is the last HARDWARE reset and survives reloads: only a fresh start counts
    _watchdog_restart = (microcontroller.cpu.reset_reason == microcontroller.ResetReason.WATCHDOG
                         and supervisor.runtime.run_reason == supervisor.RunReason.STARTUP)
    print("reset reason:", microcontroller.cpu.reset_reason, supervisor.runtime.run_reason)
    microcontroller.watchdog.timeout = WATCHDOG_S
    microcontroller.watchdog.mode = WatchDogMode.RESET
except Exception as e:
    print("watchdog:", e)


def feed():
    try:
        microcontroller.watchdog.feed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# camera init
# ---------------------------------------------------------------------------
# After a watchdog restart the last start probably froze on the card: start
# without it this time (no boot loop); the first shot tries the card again.
_mount_sd_card = adafruit_pycamera.PyCamera.mount_sd_card
if _watchdog_restart:
    def _skip_sd(self):
        raise RuntimeError("SD skipped after a watchdog restart")
    adafruit_pycamera.PyCamera.mount_sd_card = _skip_sd
pycam = adafruit_pycamera.PyCamera()
adafruit_pycamera.PyCamera.mount_sd_card = _mount_sd_card
feed()


def sd_mounted():
    # NOT os.stat(SD): CIRCUITPY has an empty /sd folder (the mount point), so
    # that passed with no card and the shot then failed writing ("No SD?").
    try:
        storage.getmount(SD)
        return True
    except OSError:
        return False


def ensure_sd(tries=3):
    # The card's contacts are touchy (2026-10-31: pressing the case showed
    # "No SD", and some power-ups started without the card until a restart). PyCamera() tries the card once and carries on without it; this
    # mounts it again whenever the card-detect switch says a card is there.
    if sd_mounted():
        return True
    for attempt in range(tries):
        try:
            pycam.card_detect.update()
            if not pycam.card_detect.value:
                return False                 # really no card in the slot
            pycam.mount_sd_card()
            if sd_mounted():
                print("sd: mounted (try %d)" % (attempt + 1))
                return True
        except Exception as e:
            print("sd mount:", e)
        time.sleep(0.3)
    return False


SD_GONE_S = 1.0          # card-detect must stay open this long before the card counts as removed
_card_gone_at = None

pycam.mode = 0                       # JPEG
pycam.resolution = DEFAULT_RESOLUTION
pycam.effect = DEFAULT_EFFECT


def tune_sensor():
    # The library's init_camera() sets saturation to +3 (the max), which is most
    # of the pink/purple cast; put it back to neutral and turn on the sensor's
    # own clean-up (lens shading = darker purple corners, bad/white pixels,
    # gamma). init_camera() runs again after every WiFi sync/print and wake,
    # so this is called after each one. Each property is optional per driver.
    for prop, value in (("saturation", 0), ("lenc", True), ("bpc", True),
                        ("wpc", True), ("raw_gma", True), ("quality", JPEG_QUALITY)):
        try:
            setattr(pycam.camera, prop, value)
        except Exception as e:
            print("tune", prop, e)
    for prop, value in (("denoise", DENOISE), ("sharpness", SHARPNESS), ("dcw", not AWB_SIMPLE)):
        if value is not None:
            try:
                setattr(pycam.camera, prop, value)
            except Exception as e:
                print("tune", prop, e)
    try:                                     # face-weighted metering (see METERING_WEIGHTS)
        for i, w in enumerate(METERING_WEIGHTS):
            pycam.write_camera_register(0x5688 + i, w)
    except Exception as e:
        print("tune metering", e)


tune_sensor()

try:
    wifi.radio.enabled = False       # stays off until sync() needs it
except Exception as e:
    print("radio:", e)

# `splash` is the library's display root group (it holds the HUD labels we
# repurpose in _osd_setup / update_osd, plus it's where display_message draws).
# The gallery swaps root_group to an image group and back to this.
LIB_SPLASH = pycam.display.root_group

decoder = JpegDecoder()

# ---------------------------------------------------------------------------
# custom on-screen OSD  (matches the mockup)
#
# The preview is a 240x176 frame blitted straight to the framebuffer at
# y_offset=32, so the top ~32px and the bottom ~32px of the 240x240 panel are
# never touched by the preview. We paint our status bars there as a displayio
# overlay with opaque black backgrounds, which also hides the library's stock
# HUD that lives in the same strips. The middle stays clean automatically.
# ---------------------------------------------------------------------------
AMBER = 0xEF9F27
DIM   = 0xBA7517
ORANGE = 0xFF8C1A
RED   = 0xE24B4A
BLACK = 0x000000

# charge-status LED (onboard NeoPixel) — tweak colors/brightness to taste
LED_BRIGHT   = 0.15
LED_CHARGING = (255, 90, 0)     # amber while charging
LED_FULL     = (0, 90, 0)       # green when topped off
LED_BATTERY  = (0, 0, 0)        # off on battery

RES_NAMES = ["1:1", "QVGA", "VGA", "SVGA", "XGA", "720P", "SXGA",
             "UXGA", "1080P", "QXGA", "QHD", "WQXGA", "5MP"]
EFFECT_NAMES = ["NORM", "INV", "B&W", "RED", "GRN", "BLU", "SEPIA", "SOLAR"]


def _pal(color):
    p = displayio.Palette(1)
    p[0] = color
    return p


def _label(text, color, x, y, anchor=(0.0, 0.0), scale=1):
    lbl = bitmap_label.Label(terminalio.FONT, text=text, color=color, scale=scale)
    lbl.anchor_point = anchor
    lbl.anchored_position = (x, y)
    return lbl


# palettes reused by the vector shapes
PAL_BLACK = _pal(BLACK)
PAL_AMBER = _pal(AMBER)
PAL_RED = _pal(RED)
PAL_BG = _pal(0x101418)                  # near-black splash backdrop

# battery sense: 1:2 divider on board.BATTERY_MONITOR (first read is stale)
try:
    _batt_adc = AnalogIn(board.BATTERY_MONITOR)
    _ = _batt_adc.value                      # prime / discard the stale reading
except Exception as e:
    print("batt:", e)
    _batt_adc = None

# LiPo voltage -> percent (rough resting-curve, interpolated)
_BATT_CURVE = ((3.30, 0), (3.50, 10), (3.60, 20), (3.70, 35), (3.75, 45),
               (3.80, 55), (3.85, 65), (3.90, 72), (3.95, 80), (4.00, 86),
               (4.10, 94), (4.20, 100))


def _v_to_pct(v):
    if v <= _BATT_CURVE[0][0]:
        return 0
    if v >= _BATT_CURVE[-1][0]:
        return 100
    for (v0, p0), (v1, p1) in zip(_BATT_CURVE, _BATT_CURVE[1:]):
        if v <= v1:
            return int(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


def read_battery_voltage():
    if _batt_adc is None:
        return None
    try:
        _ = _batt_adc.value                  # toss the first (stale) sample
        time.sleep(0.001)                    # let the sample cap charge
        s = 0
        for _ in range(4):                   # average a few to cut ADC noise
            s += _batt_adc.value
        vref = getattr(_batt_adc, "reference_voltage", 3.3)
        return (s / 4) * vref / 65535 * 2    # *2 for the 1:2 divider
    except Exception:
        return None


# onboard status NeoPixel, repurposed as a charge indicator
try:
    if hasattr(board, "NEOPIXEL_POWER"):
        _np_pwr = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
        _np_pwr.switch_to_output(value=True)
    status_pix = neopixel.NeoPixel(board.NEOPIXEL, 1,
                                   brightness=LED_BRIGHT, auto_write=True)
    status_pix[0] = (0, 0, 0)
except Exception as e:
    print("pixel:", e)
    status_pix = None
_led_state = None

# ---------------------------------------------------------------------------
# OSD: instead of drawing our own overlay (which loses to the library's HUD on
# this partial-refresh panel - when the library re-renders one of its labels,
# only that rectangle repaints and our overlay on top is not re-composited), we
# COMMANDEER the library's own corner labels. The library renders exactly these
# every frame, positioned to stay clear of the blit() preview zone, so putting
# our text in them means one label set, no overlap, nothing to hide or clear.
#
#   _res_label    top-left   (x=0,  y=10, scale 2)  -> REC + frame counter
#   _sd_label     top-right  (x=170,y=10, scale 2)  -> battery %
#   _effect_label bottom-left(x=4,  y=10, scale 2)  -> resolution + effect
#   _mode_label   bottom-right                       -> date + time (rescaled)
#   _timelapse*   (preview zone)                     -> blanked
# ---------------------------------------------------------------------------
def _osd_setup():
    # blank the timelapse row (it sits down in the preview area)
    for _a in ("timelapsestatus_label", "timelapse_rate_label", "timelapse_submode_label"):
        lbl = getattr(pycam, _a, None)
        if lbl is not None:
            try:
                lbl.text = ""
            except Exception:
                pass
    # recolor the corner labels to our amber/orange palette
    for _a, _c in (("_res_label", AMBER), ("_sd_label", AMBER),
                   ("_effect_label", AMBER), ("_mode_label", ORANGE)):
        lbl = getattr(pycam, _a, None)
        if lbl is not None:
            try:
                lbl.color = _c
            except Exception:
                pass
    # the date+time is long; shrink and slide the bottom-right label so it fits
    try:
        pycam._mode_label.scale = 1
        pycam._mode_label.x = 150            # within _botbar (which is at y=210)
        pycam._mode_label.y = 14
    except Exception as e:
        print("osd setup:", e)


_osd_setup()

PREVIEW_GROUP = LIB_SPLASH               # library's live-preview root (for the gallery)


shot_count = 0
_blink_on = True
_last_osd = 0
_batt_pct = None
_last_batt = 0


def refresh_shot_count():
    global shot_count
    shot_count = len(list_photos())


def _set(attr, text, color=None):
    lbl = getattr(pycam, attr, None)
    if lbl is None:
        return
    try:
        if lbl.text != text:
            lbl.text = text
        if color is not None and lbl.color != color:
            lbl.color = color
    except Exception:
        pass


def update_osd():
    global _blink_on, _last_osd, _batt_pct, _last_batt, _led_state
    now = time.monotonic()
    if now - _last_osd < 0.5:
        return
    _last_osd = now
    _blink_on = not _blink_on

    # top-left (_res_label): blinking REC dot + frame counter (PRT in PRINT mode)
    dot = "*" if _blink_on else " "
    _set("_res_label", "%s%s %03d" % (dot, "PRT" if print_mode else "REC", shot_count % 1000), AMBER)

    if now - _last_batt > 5:                  # battery is slow-moving; sample rarely
        _last_batt = now
        v = read_battery_voltage()
        if v is None:
            _set("_sd_label", "--%", AMBER)
        else:
            pct = _v_to_pct(v)
            _batt_pct = pct if _batt_pct is None else int(_batt_pct * 0.6 + pct * 0.4)
            _set("_sd_label", "%d%%" % _batt_pct, RED if _batt_pct <= 15 else AMBER)

        # charge-status LED: amber charging / green full / off on battery
        try:
            plugged = supervisor.runtime.usb_connected
        except Exception:
            plugged = False
        if not plugged:
            state = "battery"
        elif _batt_pct is not None and _batt_pct >= 98:
            state = "full"
        else:
            state = "charging"
        if status_pix is not None and state != _led_state:
            _led_state = state
            status_pix[0] = (LED_FULL if state == "full"
                             else LED_CHARGING if state == "charging"
                             else LED_BATTERY)

    # bottom-left (_effect_label): resolution + effect
    try:
        r = RES_NAMES[pycam.resolution]
    except Exception:
        r = "RES"
    try:
        e = EFFECT_NAMES[pycam.effect]
    except Exception:
        e = "FX%d" % pycam.effect
    _set("_effect_label", "%s %s" % (r, e), AMBER)

    # bottom-right (_mode_label): date + time (scale 1, set in _osd_setup)
    if _clock_set():
        t = time.localtime()
        _set("_mode_label", "%02d/%02d %02d:%02d" % (
            t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min), ORANGE)
    else:
        _set("_mode_label", "--/-- --:--", ORANGE)


def _restore_live_ui():
    # Return to the live UI after a full-screen takeover (boot splash or a gallery
    # photo). LIB_SPLASH only holds the corner HUD labels, so a plain root_group
    # swap + refresh repaints just those small label boxes - it does NOT clear the
    # top/bottom 32px status bars, and the preview blit only covers rows 32-207.
    # Without an explicit clear, leftover splash/photo pixels survive behind the
    # HUD. Force one full black repaint first (the proven Bitmap-group mechanism),
    # then show the live UI on top of clean black. Refresh a few times each step:
    # a single swap can partially repaint on this panel (cp issue #28).
    try:
        blackg = Group()
        blackg.append(TileGrid(Bitmap(240, 240, 1), pixel_shader=PAL_BLACK))
        pycam.display.root_group = blackg
        for _ in range(2):
            pycam.display.refresh()
            time.sleep(0.04)
    except Exception as e:
        print("clear:", e)
    try:
        pycam.display.root_group = LIB_SPLASH
        for _ in range(2):
            pycam.display.refresh()
            time.sleep(0.04)
    except Exception as e:
        print("restore:", e)

# ---------------------------------------------------------------------------
# retro boot splash
# ---------------------------------------------------------------------------
SPLASH_SECONDS = 2.2


def _ctext(text, color, cx, cy, scale=1):
    lbl = bitmap_label.Label(terminalio.FONT, text=text, color=color, scale=scale)
    lbl.anchor_point = (0.5, 0.5)
    lbl.anchored_position = (cx, cy)
    return lbl


def boot_splash():
    # Cover the whole screen using the SAME mechanism the gallery uses (the only
    # thing proven to fully repaint this panel): swap root_group to a group whose
    # backdrop is a Bitmap TileGrid. A vectorio Rectangle does NOT work here - on
    # this panel blit() writes pixels directly and desyncs displayio's dirty
    # tracking, so a vector rect on top won't re-send the blitted region (only the
    # text labels paint). A fresh root with a Bitmap forces a real full repaint.
    #
    # Customise: set CAMERA_NAME in settings.toml, or drop a 240x240 splash.bmp on
    # the CIRCUITPY drive (or /sd/splash.bmp) and it'll be shown instead.
    try:
        g = Group()
        shown_bmp = False
        for path in ("/sd/splash.bmp", "/splash.bmp"):
            if not _exists(path):                # absent here is normal; skip quietly
                continue
            try:
                odb = displayio.OnDiskBitmap(path)
                g.append(TileGrid(odb, pixel_shader=odb.pixel_shader))
                shown_bmp = True
                break
            except Exception as e:               # present but undecodable -> say why
                print("splash bmp", path, e)     # (must be uncompressed 24-bit BMP)

        if not shown_bmp:
            bg = Bitmap(240, 240, 1)             # solid backdrop via Bitmap
            pal = displayio.Palette(1)
            pal[0] = 0x101418
            g.append(TileGrid(bg, pixel_shader=pal))
            # retro amber frame
            for r in (vectorio.Rectangle(pixel_shader=PAL_AMBER, width=240, height=3, x=0, y=0),
                      vectorio.Rectangle(pixel_shader=PAL_AMBER, width=240, height=3, x=0, y=237),
                      vectorio.Rectangle(pixel_shader=PAL_AMBER, width=3, height=240, x=0, y=0),
                      vectorio.Rectangle(pixel_shader=PAL_AMBER, width=3, height=240, x=237, y=0)):
                g.append(r)
            name = str(os.getenv("CAMERA_NAME") or os.getenv("BABY_NAME") or "CAMERA")[:16]
            scale = 4 if len(name) <= 7 else (3 if len(name) <= 10 else 2)
            g.append(_ctext(name, AMBER, 120, 98, scale=scale))
            g.append(_ctext("* instant camera *", ORANGE, 120, 144, scale=1))
            g.append(_ctext("rec ready", DIM, 120, 210, scale=1))

        print("splash:", "bmp" if shown_bmp else "text")   # serial diagnostic
        # Mirror the gallery's show_image() path (the proven full-screen path on
        # this panel): swap root_group once, then leave the display alone while it
        # holds. Two boot-time display quirks are documented in CircuitPython:
        #  - refresh is degraded for ~the first minute after boot (cp issue #10176),
        #    so we pause briefly to let the panel settle before the swap;
        #  - a swap sometimes only PARTIALLY repaints and "repeated calls help"
        #    (cp issue #28), so we refresh a few times to complete the paint.
        # We do NOT poll the shutter here - that was dismissing the splash early.
        time.sleep(0.25)
        pycam.display.root_group = g
        for _ in range(3):
            pycam.display.refresh()
            time.sleep(0.06)
        time.sleep(SPLASH_SECONDS)               # just hold it; do NOT touch display
    except Exception as e:
        print("splash:", e)
    finally:
        _restore_live_ui()                       # clear splash pixels, show live UI

# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _clock_set():
    # RTC reads year 2000 until it's been set (via NTP at sync)
    try:
        return time.localtime().tm_year >= 2024
    except Exception:
        return False


def _nth_sunday(year, month, n):
    # day of the month of its n-th Sunday (tm_wday: Monday = 0)
    first = time.localtime(time.mktime((year, month, 1, 12, 0, 0, 0, -1, -1))).tm_wday
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def utc_offset(utc_secs):
    # hours to add to UTC at that moment (TZ_OFFSET, plus the summer hour with TZ_DST = "us")
    if TZ_DST != "us":
        return TZ_OFFSET
    y = time.localtime(utc_secs).tm_year
    start = time.mktime((y, 3, _nth_sunday(y, 3, 2), 0, 0, 0, 0, -1, -1)) + int((2 - TZ_OFFSET) * 3600)
    end = time.mktime((y, 11, _nth_sunday(y, 11, 1), 0, 0, 0, 0, -1, -1)) + int((1 - TZ_OFFSET) * 3600)
    return TZ_OFFSET + (1 if start <= utc_secs < end else 0)


_clock_set_at = None       # monotonic time of the last NTP set (this run)


def set_clock(pool):
    # local time from NTP (UTC), with the summer hour when it applies
    global _clock_set_at
    feed()
    utc = time.mktime(adafruit_ntp.NTP(pool, tz_offset=0).datetime)
    offset = utc_offset(utc)
    rtc.RTC().datetime = time.localtime(utc + int(offset * 3600))
    _clock_set_at = time.monotonic()
    print("clock set: UTC%+g" % offset)


def clock_stale():
    # never set in this run (the RTC survives a reload, a wrong hour with it), or not for 6 hours (the camera can
    # stay on across a daylight saving change)
    return _clock_set_at is None or time.monotonic() - _clock_set_at > 6 * 3600


def next_name():
    # preferred: real capture time, so the server can stamp it accurately
    if _clock_set():
        t = time.localtime()
        base = "IMG_%04d%02d%02d_%02d%02d%02d" % (
            t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
        name = base + ".jpg"
        seq = 1
        while _exists("%s/%s" % (SD, name)):       # same-second collision guard
            name = "%s_%d.jpg" % (base, seq)
            seq += 1
        return name
    # fallback (clock not yet set this power cycle): sortable counter
    n = 0
    try:
        for f in os.listdir(SD):
            if (len(f) == 12 and f.startswith("IMG")     # exactly IMG00000.jpg
                    and f[3:8].isdigit() and f[8:].lower() == ".jpg"):
                n = max(n, int(f[3:8]))
    except OSError:
        pass
    return "IMG%05d.jpg" % (n + 1)


def continuous_focus():
    # The OV5640's AF firmware can keep refocusing on its own while you aim (the
    # library defines the command, 0x04, but never sends it). Tested 2026-10-30:
    # status 0x20 "focused (continuous)" within ~0.1 s, and it keeps running
    # through the full-res capture and back to preview. Its ack can time out
    # even when it starts, so the status register is what counts. Needs the AF
    # firmware, which init_camera() reloads: call it after every init_camera().
    try:
        pycam._send_autofocus_command(0x04, "continuous autofocus")
    except Exception as e:
        print("caf:", e)


def focus_is_continuous():
    try:
        return pycam.autofocus_status in (0x00, 0x20)   # focusing / focused, continuous
    except Exception:
        return False


def refocus():
    # one-shot autofocus on demand (tap Left); blocks briefly
    try:
        pycam.display_message("Focus...", color=0xEF9F27)
        pycam.autofocus()
        pycam.tone(1320, 0.04)
    except Exception as e:
        print("af:", e)
    continuous_focus()                       # a one-shot focus stops continuous mode


def _gain():
    return ((pycam.read_camera_register(0x350A) & 0x03) << 8) | pycam.read_camera_register(0x350B)


def fast_shutter(limit=FAST_SHUTTER_LINES):
    # Trade a long indoor shutter for gain at the same brightness: limit lines
    # (FAST_SHUTTER_LINES in PRINT mode, REC_SHUTTER_LINES otherwise). Reads where
    # auto exposure settled in the live preview; only acts when the shutter is
    # longer than the limit. Returns True if it took over (the caller must hand
    # control back with auto_exposure()).
    try:
        lines = pycam.get_camera_autosettings()["exposure"]
        gain = _gain()
        if lines <= limit or gain <= 0:
            return False
        new_gain = min(0x3FF, gain * lines // limit)
        pycam.set_camera_exposure(limit)     # also turns auto exposure off
        pycam.camera.gain_ctrl = False
        pycam.write_camera_register(0x350A, (new_gain >> 8) & 0x03)
        pycam.write_camera_register(0x350B, new_gain & 0xFF)
        print("fast shutter: %d -> %d lines, gain %d -> %d" % (lines, limit, gain, new_gain))
        return True
    except Exception as e:
        print("fast shutter:", e)
        return False


def auto_exposure():
    try:
        pycam.set_camera_exposure(None)
        pycam.set_camera_gain(None)
    except Exception as e:
        print("auto exposure:", e)


def capture_still():
    # pycam.capture_into_jpeg(), but kinder to exposure: it grabs the very first
    # frame 0.1s after switching the sensor to full-res JPEG, before exposure has
    # settled (dim, soft shots). Here: switch, re-apply the tuning (a frame-size
    # change rewrites sensor registers), wait, throw one frame away, keep the next.
    pycam.camera.reconfigure(
        pixel_format=espcamera.PixelFormat.JPEG,
        frame_size=pycam.resolution_to_frame_size[pycam.resolution],
    )
    tune_sensor()
    time.sleep(SETTLE_S)
    try:
        pycam.camera.take(1)                 # settling frame, discarded
    except Exception as e:
        print("settle frame:", e)
    jpeg = pycam.camera.take(1)
    if jpeg is None:
        print("JPEG capture failed")
    else:
        print("captured %d bytes, %dx%d" % (len(jpeg), pycam.camera.width, pycam.camera.height))
    return jpeg


def sensor_test(spec):
    # Tuning shots (CAMERA_SENSOR_TEST, e.g. "0:,8:,0:-2,8:-2"): one REC-style
    # capture per denoise:sharpness pair (blank sharpness = the sensor's own;
    # put those first, manual sharpening stays until a restart), saved as
    # TEST_dn<d>_sh<s>.jpg; plugged into a computer they sync to the Pi like any
    # photo. Without the setting, leftover TEST_ shots are removed at power-up.
    global DENOISE, SHARPNESS, AWB_SIMPLE
    keep = (DENOISE, SHARPNESS, AWB_SIMPLE)
    _forget_test_shots()
    for pair in str(spec).split(","):
        parts = (pair.strip().split(":") + ["", "", ""])[:3]
        try:
            DENOISE = int(parts[0]) if parts[0].strip() else 0
            SHARPNESS = int(parts[1]) if parts[1].strip() else None
        except ValueError:
            continue
        AWB_SIMPLE = parts[2].strip().lower() == "simple"
        feed()
        tune_sensor()                        # the preview runs with this pair, so AWB/exposure settle on it
        pycam.display_message("Test %s" % pair.strip(), color=0xEF9F27)
        time.sleep(2.5)                      # exposure + white balance settle in the preview between shots
        try:
            pycam.set_camera_wb(pycam.get_camera_autosettings()["wb"])
            fast_shutter(REC_SHUTTER_LINES)
            jpeg = capture_still()           # its tune_sensor() applies DENOISE / SHARPNESS
            if jpeg:
                name = "TEST_dn%d_sh%s%s.jpg" % (DENOISE, "auto" if SHARPNESS is None else SHARPNESS,
                                                 "_awbsimple" if AWB_SIMPLE else "")
                with open("%s/%s" % (SD, name), "wb") as f:
                    f.write(jpeg)
                print("sensor test:", name, len(jpeg))
        except Exception as e:
            print("sensor test:", e)
        finally:
            try:
                pycam.set_camera_wb(None)
            except Exception:
                pass
            auto_exposure()
            try:
                pycam.live_preview_mode()
            except Exception as e:
                print("preview restore:", e)
    DENOISE, SHARPNESS, AWB_SIMPLE = keep
    tune_sensor()


def _forget_test_shots():
    # test shots reuse their names run after run: drop them from the upload list so new ones are sent again
    try:
        keep = [n for n in _uploaded_set() if not n.startswith("TEST_dn")]
        with open(MANIFEST + ".tmp", "w") as f:
            for n in sorted(keep):
                f.write(n + "\n")
        os.rename(MANIFEST + ".tmp", MANIFEST)
    except OSError as e:
        print("forget test shots:", e)


def remove_test_shots():
    if any(f.startswith("TEST_dn") for f in list_photos()):
        _forget_test_shots()
    for f in list_photos():
        if f.startswith("TEST_dn"):
            try:
                os.remove("%s/%s" % (SD, f))
            except OSError as e:
                print("remove test shot:", e)


def take_photo():
    global shot_count
    if not ensure_sd():                      # no card even after a re-mount: skip the
        pycam.display_message("No SD", color=0xFF0000)   # wasted full-res capture and
        time.sleep(0.5)                      # give immediate feedback instead
        return
    saved = False
    # Continuous focus keeps the lens sharp while aiming, so normally there's no
    # focus wait. If it isn't running, PRINT mode falls back to a one-shot focus
    # (~1.1 s): then a soft beep + "Hold still", because the photo comes later.
    # The click always plays at the real capture: a camera lowered at an early
    # click photographed a lap (2026-10-30).
    focusing = (AF_ON_CAPTURE or print_mode) and not focus_is_continuous()
    try:
        if focusing:
            pycam.tone(700, 0.03)
            pycam.display_message("Hold still", color=0xEF9F27)
            try:
                pycam.autofocus()            # off by default: AF is the slow part
            except Exception as e:
                print("af:", e)
            continuous_focus()
        try:                                 # carry the live preview's converged
            pycam.set_camera_wb(             # auto-white-balance into the still so
                pycam.get_camera_autosettings()["wb"])   # the JPEG isn't pink:
        except Exception as e:               # capture_into_jpeg grabs only 0.1s after
            print("wb lock:", e)             # a sensor reset, before AWB re-converges
        # moving people: some grain beats smear (more so for a print)
        fast_shutter(FAST_SHUTTER_LINES if print_mode else REC_SHUTTER_LINES)
        jpeg = capture_still()               # buffer is valid ONLY until the mode
        pycam.tone(1200, 0.04)               # the shutter click, at the real capture
        if jpeg:                             # changes, so write it BEFORE restoring
            name = next_name()               # preview (that's what corrupted shots)
            for attempt in (1, 2):           # a squeezed case can reset the card: the
                try:                         # mount looks fine but writes fail; re-mount once
                    with open("%s/%s" % (SD, name), "wb") as f:
                        f.write(jpeg)
                    shot_count += 1
                    saved = True
                    break
                except OSError as e:
                    print("save:", e)
                    if attempt == 1:
                        jpeg = bytes(jpeg)   # own copy: the re-mount re-inits the display + SPI
                        pycam.unmount_sd_card()
                        if ensure_sd():
                            continue
                    pycam.display_message("No SD?", color=0xFF0000)
                    time.sleep(0.5)
                    break
        else:
            pycam.display_message("Failed", color=0xFF0000)
            time.sleep(0.5)
    except Exception as e:
        print("capture:", e)
        pycam.display_message("Failed", color=0xFF0000)
        time.sleep(0.5)
    finally:
        try:
            pycam.set_camera_wb(None)        # live preview back to auto white balance
        except Exception:
            pass
        auto_exposure()                      # undo fast_shutter() (harmless otherwise)
        try:
            pycam.live_preview_mode()        # restore preview AFTER the file is written
        except Exception as e:
            print("preview restore:", e)
            try:                             # sensor wedged in JPEG mode: rebuild it
                pycam.init_camera()          # (same recovery sync()/wake_up() use)
                tune_sensor()                    # init_camera() resets saturation to +3
                continuous_focus()               # init_camera() reloads the AF firmware
                pycam.live_preview_mode()
            except Exception as e2:
                print("cam rebuild:", e2)
        gc.collect()

    if saved:
        pycam.tone(1500, 0.04)               # single quick confirm beep
        if print_mode:
            print_photo(name)                # shows "Printing..." straight away
        else:
            pycam.display_message("Saved", color=0x00FF00)
            time.sleep(0.25)                 # brief, so "Saved" is visible

# ---------------------------------------------------------------------------
# on-device gallery (jpegio)
# ---------------------------------------------------------------------------
def list_photos():
    try:
        return sorted(f for f in os.listdir(SD) if f.lower().endswith(".jpg"))
    except OSError:
        return []


def _gallery_message(text, color):
    lbl = bitmap_label.Label(terminalio.FONT, text=text, color=color, scale=2)
    lbl.anchor_point = (0.5, 0.5)
    lbl.anchored_position = (120, 120)
    g = Group()
    g.append(lbl)
    pycam.display.root_group = g
    pycam.display.refresh()
    time.sleep(0.8)


def show_image(path):
    try:
        w, h = decoder.open(path)
        scale = 0
        while (w >> scale) > 240 or (h >> scale) > 240:   # fit WHOLE image on panel
            scale += 1
        bw, bh = w >> scale, h >> scale
        bmp = Bitmap(bw, bh, 65535)
        decoder.decode(bmp, scale=scale)
        tg = TileGrid(bmp, pixel_shader=ColorConverter(
            input_colorspace=Colorspace.RGB565_SWAPPED))
        tg.x = (240 - bw) // 2               # >=0: centered, nothing cropped
        tg.y = (240 - bh) // 2
        g = Group()
        g.append(TileGrid(Bitmap(240, 240, 1), pixel_shader=PAL_BLACK))  # opaque bg:
        g.append(tg)                         # full repaint, no remnants between shots
        pycam.display.root_group = g
        pycam.display.refresh()
        gc.collect()
    except Exception as e:
        print("decode:", e)
        _gallery_message("Bad JPEG", 0xFF0000)


def gallery():
    photos = list_photos()
    if not photos:
        pycam.display_message("No photos", color=0xEF9F27)
        time.sleep(0.8)
        return
    i = len(photos) - 1                      # start on the newest
    show_image("%s/%s" % (SD, photos[i]))
    pycam.tone(1000, 0.04)
    ok_armed = False
    exit_armed = False                       # the long-press that OPENED the gallery
                                             # must be released before it can close it

    while True:
        feed()
        pycam.keys_debounce()
        if pycam.shutter_button.value:       # Pull.UP: True == shutter released
            exit_armed = True
        if pycam.left.fell:
            i = (i - 1) % len(photos)
            show_image("%s/%s" % (SD, photos[i]))
        elif pycam.right.fell:
            i = (i + 1) % len(photos)
            show_image("%s/%s" % (SD, photos[i]))

        if pycam.ok.fell:                    # OK is a plain Debouncer: time the hold
            ok_armed = True
        elif pycam.ok.rose:
            if ok_armed:                     # released before the hold fired: a tap
                ok_armed = False             # -> print the photo on screen
                print_photo(photos[i])
                show_image("%s/%s" % (SD, photos[i]))   # the messages drew over it
            ok_armed = False
        if ok_armed and not pycam.ok.value and pycam.ok.current_duration > OK_HOLD_S:
            ok_armed = False                 # hold OK to delete (fires once)
            try:
                os.remove("%s/%s" % (SD, photos[i]))
                pycam.tone(400, 0.15)
            except OSError as e:
                print("delete:", e)
            photos = list_photos()
            if not photos:
                break
            i = min(i, len(photos) - 1)
            show_image("%s/%s" % (SD, photos[i]))

        if exit_armed and pycam.shutter.long_press:   # hold shutter again to exit
            break
        time.sleep(0.03)

    # drain the exit hold so the main loop doesn't immediately re-open the gallery
    while not pycam.shutter_button.value:
        pycam.keys_debounce()
        time.sleep(0.01)

    _restore_live_ui()                         # back to live preview (clears remnants)
    refresh_shot_count()
    pycam.tone(1000, 0.04)

# ---------------------------------------------------------------------------
# WiFi (radio up only for a sync or a print)
# ---------------------------------------------------------------------------
# (channel, bssid) of the last good access point per network, kept in NVM (adafruit_pycamera uses bytes 1-5) so
# even the first print after power-on skips the full scan (3.6 s for that connect, 2026-10-30): home at 64-71,
# the print box at 72-79, and which of them worked last at 80.
_HINT_AT = {"home": 64, "away": 72}
_LAST_AT = 80
_HINT_MAGIC = 0xB7


def _load_ap_hint(where):
    try:
        at = _HINT_AT[where]
        b = microcontroller.nvm[at:at + 8]
        if b[0] == _HINT_MAGIC and 1 <= b[1] <= 14:
            return (b[1], bytes(b[2:8]))
    except Exception as e:
        print("hint:", e)
    return None


def _save_ap_hint(where, hint):
    try:
        at = _HINT_AT[where]
        data = bytes([_HINT_MAGIC, hint[0]]) + bytes(hint[1])
        if len(data) == 8 and bytes(microcontroller.nvm[at:at + 8]) != data:
            microcontroller.nvm[at:at + 8] = data    # only when it changed
        last = 1 if where == "away" else 0
        if microcontroller.nvm[_LAST_AT] != last:
            microcontroller.nvm[_LAST_AT] = last
    except Exception as e:
        print("hint:", e)


_ap_hints = {where: _load_ap_hint(where) for where in _HINT_AT}
try:
    _last_net = "away" if microcontroller.nvm[_LAST_AT] == 1 else "home"
except Exception:
    _last_net = "home"


def wifi_connect(prefer_away=False):
    # Home Wi-Fi whenever it's in range (prints go to the Pi, photos sync), else
    # the print box's network. After a home connection the home access point is
    # tried directly (channel + BSSID, no scan); after a box connection one scan
    # comes first, so a box left switched on at home never keeps the camera from
    # the Pi. prefer_away (the Pi isn't answering): the box first, home only if
    # the box isn't around. Returns (pool, "home" or "away").
    global _last_net
    feed()
    t0 = time.monotonic()
    wifi.radio.enabled = True
    order = sorted(NETWORKS, key=lambda n: n[0] != "away") if prefer_away else NETWORKS
    first = order[0] if order else None
    if first and _ap_hints.get(first[0]) and (prefer_away or _last_net == first[0] == "home"):
        hint = _ap_hints[first[0]]
        try:
            wifi.radio.connect(first[1], first[2], channel=hint[0], bssid=hint[1], timeout=4)
        except Exception as e:
            print("wifi direct:", e)
    if not wifi.radio.connected:
        seen = {}
        try:
            for net in wifi.radio.start_scanning_networks():
                if net.ssid not in seen or net.rssi > seen[net.ssid].rssi:
                    seen[net.ssid] = net
        finally:
            wifi.radio.stop_scanning_networks()
        feed()
        for where, ssid, password in order:           # home first (unless prefer_away)
            ap = seen.get(ssid)
            if ap is None:
                continue
            try:
                wifi.radio.connect(ssid, password, channel=ap.channel, bssid=ap.bssid, timeout=8)
            except Exception as e:
                print("wifi", where, e)
            if wifi.radio.connected:
                break
    if not wifi.radio.connected:
        raise ConnectionError("no known WiFi in range")
    ap = wifi.radio.ap_info
    where = "away" if (AWAY_SSID and ap.ssid == AWAY_SSID) else "home"
    _ap_hints[where] = (ap.channel, bytes(ap.bssid))
    _save_ap_hint(where, _ap_hints[where])
    _last_net = where
    print("wifi up (%s) %.2fs" % (where, time.monotonic() - t0))
    return socketpool.SocketPool(wifi.radio), where


# ---------------------------------------------------------------------------
# WiFi sync  (queue on SD -> drain to server while docked)
# ---------------------------------------------------------------------------
def _uploaded_set():
    try:
        with open(MANIFEST) as f:
            return set(line.strip() for line in f if line.strip())
    except OSError:
        return set()


def pending():
    done = _uploaded_set()
    return [f for f in list_photos() if f not in done]


def _mark_uploaded(name):
    try:
        with open(MANIFEST, "a") as m:
            m.write(name + "\n")
    except OSError as e:
        print("manifest:", e)


def compact_manifest():
    # drop names no longer on the card so the file + RAM set stay small
    try:
        keep = _uploaded_set() & set(list_photos())
        with open(MANIFEST + ".tmp", "w") as f:
            for n in sorted(keep):
                f.write(n + "\n")
        os.rename(MANIFEST + ".tmp", MANIFEST)
    except OSError as e:
        print("compact:", e)


_sync_away_until = 0      # after finding only the print box, automatic syncs rest a while


def _send_waiting(pool, files):
    # Upload photos the Pi doesn't have yet; returns how many it took.
    session = adafruit_requests.Session(pool)
    sent = 0
    for idx, name in enumerate(files):
        feed()
        pycam.display_message("Up %d/%d" % (idx + 1, len(files)), color=0xEF9F27)
        try:
            with open("%s/%s" % (SD, name), "rb") as f:
                r = session.post(SERVER, data=f, timeout=10,   # streamed from SD (M4)
                                 headers={"Content-Type": "image/jpeg",
                                          "X-Filename": name})
            status = r.status_code
            r.close()
        except Exception as e:
            print("upload:", e)
            break                        # network trouble -> retry next time
        if status in (200, 204):
            _mark_uploaded(name)
            sent += 1
        elif 400 <= status < 500:        # permanent reject: skip, don't wedge
            print("server rejected", name, status)
            _mark_uploaded(name)
        else:
            break                        # 5xx -> server trouble, retry later
        gc.collect()
    return sent


def sync(force=False):
    global _sync_away_until
    files = pending()
    if not (SSID and PASSWORD and SERVER):
        if force:
            pycam.display_message("No WiFi cfg", color=0xFF0000)
            time.sleep(0.7)
        return
    if not files and not force:
        return
    if not force and time.monotonic() < max(_sync_away_until, _pi_down_until):
        return
    away = False

    pycam.display_message("WiFi...", color=0xEF9F27)
    pycam.camera.deinit()                    # free the network path FIRST: on
    sent = 0                                 # CP 10.x espcamera blocks the radio
    try:                                     # after any capture (#10678, #9241)
        pool, where = wifi_connect()
        if where != "home":
            # only the print box is around: it doesn't keep photos (and has no internet for the clock), so the
            # sync waits for home; automatic syncs try again in 15 minutes rather than every minute
            away = True
            _sync_away_until = time.monotonic() + 900
            files = []

        if not away:
            try:                             # set the clock while we're online
                set_clock(pool)
            except Exception as e:
                print("ntp:", e)

        sent = _send_waiting(pool, files)
    except Exception as e:
        print("wifi:", e)
    finally:
        wifi.radio.enabled = False           # radio down BEFORE the camera returns
        try:
            pycam.init_camera()              # espcamera has no .init(); rebuild it
            tune_sensor()                    # init_camera() resets saturation to +3
            continuous_focus()               # init_camera() reloads the AF firmware
        except Exception as e:
            print("cam reinit:", e)

    if sent:
        compact_manifest()
        pycam.display_message("Synced %d" % sent, color=0x00FF00)
        pycam.tone(1200, 0.06)
        time.sleep(0.5)
    elif force:
        pycam.display_message("Sync at home" if away else "Up to date", color=0xEF9F27)
        time.sleep(0.6)

# ---------------------------------------------------------------------------
# print  (one photo -> the CTP500 print server, which draws + prints it)
# ---------------------------------------------------------------------------
_pi_down_until = 0         # the Pi didn't answer: prints go to the print box for a while


def _post_photo(pool, url, name, timeout):
    feed()
    session = adafruit_requests.Session(pool)
    t1 = time.monotonic()
    with open("%s/%s" % (SD, name), "rb") as f:
        r = session.post(url, data=f, timeout=timeout,      # streamed from SD
                         headers={"Content-Type": "image/jpeg", "X-Filename": name})
    status = r.status_code
    r.close()
    print("print: sent to %s in %.2fs, status %s" % (url, time.monotonic() - t1, status))
    return status


def print_photo(name):
    # Same radio dance as sync(): camera off first (CP 10.x espcamera blocks the
    # radio after a capture), radio down again BEFORE the camera comes back.
    # The server answers as soon as the job is queued, so the camera is free
    # while the paper is still coming out. At home the Pi gets it; if the Pi
    # doesn't answer (switched off: 2026-10-31, "standalone" with only the
    # camera, the box and the printer on) the print box does, and for the next
    # 10 minutes prints go straight to the box. After a print at home, photos
    # still waiting (taken on battery or printed on the box) go up in the same
    # connection, at most CATCH_UP_MAX (the camera can't shoot meanwhile).
    global _pi_down_until
    if not (NETWORKS and (PRINTER or AWAY_PRINTER)):
        pycam.display_message("No printer cfg", color=0xFF0000)
        time.sleep(0.7)
        return False
    pycam.display_message("Printing...", color=0xEF9F27)
    t0 = time.monotonic()
    pycam.camera.deinit()
    status = None
    where = None
    sent = 0
    try:
        pool, where = wifi_connect(prefer_away=bool(AWAY_PRINTER) and time.monotonic() < _pi_down_until)
        if where == "home" and PRINTER:
            try:
                status = _post_photo(pool, PRINTER, name, 6)   # the Pi answers at once
            except Exception as e:
                print("print: the Pi didn't answer:", e)
                status = None
            if (status is None or status >= 500) and AWAY_PRINTER:
                _pi_down_until = time.monotonic() + 600
                pycam.display_message("Print box...", color=0xEF9F27)
                try:
                    wifi.radio.stop_station()
                except Exception as e:
                    print("stop station:", e)
                pool, where = wifi_connect(prefer_away=True)
        if where == "away":
            status = _post_photo(pool, AWAY_PRINTER, name, 20)
        elif not PRINTER:
            raise ConnectionError("no printer set for the home network")
        if where == "home" and clock_stale():      # set the clock now (after the print is queued),
            try:                                   # so the next photos get dated names
                set_clock(pool)
            except Exception as e:
                print("ntp:", e)
        if where == "home" and status is not None and 200 <= status < 300:
            _mark_uploaded(name)             # the Pi keeps it in its gallery (the box doesn't: syncs at home)
            waiting = pending()[:CATCH_UP_MAX]
            if waiting and SERVER:
                t1 = time.monotonic()
                sent = _send_waiting(pool, waiting)
                print("print: caught up %d of %d waiting in %.2fs" % (sent, len(waiting), time.monotonic() - t1))
    except Exception as e:
        print("print:", e)
    finally:
        wifi.radio.enabled = False
        try:
            pycam.init_camera()
            tune_sensor()                    # init_camera() resets saturation to +3
            continuous_focus()               # init_camera() reloads the AF firmware
        except Exception as e:
            print("cam reinit:", e)
        gc.collect()
        print("print: camera back after %.2fs" % (time.monotonic() - t0))

    if sent:
        compact_manifest()
    if status is not None and 200 <= status < 300:
        pycam.display_message("Printing!", color=0x00FF00)
        pycam.tone(1500, 0.05)
        pycam.tone(1900, 0.05)
        time.sleep(0.6)
        return True
    print("print failed:", status)
    pycam.display_message("No printer" if status is None else "Print error", color=0xFF0000)
    pycam.tone(400, 0.15)
    time.sleep(0.9)
    return False

# ---------------------------------------------------------------------------
# settings menu (drives the library's own built-in setting properties + HUD)
# ---------------------------------------------------------------------------
SETTINGS = ("resolution", "effect")      # mode/timelapse_rate don't work here; no LED ring
cur_setting = 0


def bump_setting(delta):
    name = SETTINGS[cur_setting]
    try:
        if name == "resolution":
            pycam.resolution = (pycam.resolution + delta) % (MAX_RES_INDEX + 1)
        else:
            setattr(pycam, name, getattr(pycam, name) + delta)
        pycam.tone(880, 0.02)
    except Exception as e:
        print("setting", name, e)

# ---------------------------------------------------------------------------
# input handling
# ---------------------------------------------------------------------------
def read_inputs():
    global cur_setting, last_activity, _ok_armed, _up_armed, _card_gone_at
    pycam.keys_debounce()

    if any_button():
        last_activity = time.monotonic()

    # SD hot-plug (the library debounces card_detect via keys_debounce). A press
    # on the case blips the switch: only a card gone for SD_GONE_S is unmounted,
    # and a card that comes back is checked and re-mounted if it needs it.
    try:
        if pycam.card_detect.fell:
            print("SD switch open")
            _card_gone_at = time.monotonic()
        elif pycam.card_detect.rose:
            print("SD switch closed")
            _card_gone_at = None
            if not ensure_sd():
                pycam.display_message("SD err", color=0xFF0000)
            refresh_shot_count()
        elif _card_gone_at is not None and time.monotonic() - _card_gone_at > SD_GONE_S:
            print("SD removed")
            _card_gone_at = None
            pycam.unmount_sd_card()
            refresh_shot_count()
    except Exception as e:
        print("card:", e)

    if pycam.shutter.long_press:             # hold shutter -> gallery
        gallery()
        last_activity = time.monotonic()     # don't idle right after a long browse
        return
    if pycam.shutter.short_count:            # tap -> photo
        take_photo()
        last_activity = time.monotonic()
        return

    # OK is a plain Debouncer (no long_press); time the hold manually
    if pycam.ok.fell:
        _ok_armed = True
    elif pycam.ok.rose:
        _ok_armed = False
    if _ok_armed and not pycam.ok.value and pycam.ok.current_duration > OK_HOLD_S:
        _ok_armed = False                    # hold OK -> sync now (works on battery)
        sync(force=True)
        last_activity = time.monotonic()     # sync can take a while; reset timer
        return

    if pycam.right.fell:                     # next setting category
        cur_setting = (cur_setting + 1) % len(SETTINGS)
        pycam.tone(660, 0.02)
    if pycam.left.fell:                       # tap Left -> refocus on demand
        refocus()
    # Up: tap = next value of the setting, hold = PRINT mode on/off. The tap fires
    # on release so a hold doesn't also change the setting.
    if pycam.up.fell:
        _up_armed = True
    elif pycam.up.rose:
        if _up_armed:
            bump_setting(+1)
        _up_armed = False
    if _up_armed and not pycam.up.value and pycam.up.current_duration > OK_HOLD_S:
        _up_armed = False
        toggle_print_mode()
        return
    if pycam.down.fell:
        bump_setting(-1)


def toggle_print_mode():
    global print_mode
    if not (PRINTER or AWAY_PRINTER):
        pycam.display_message("No printer cfg", color=0xFF0000)
        time.sleep(0.7)
        return
    print_mode = not print_mode
    pycam.display_message("Print ON" if print_mode else "Print OFF",
                          color=0x00FF00 if print_mode else 0xEF9F27)
    pycam.tone(1500 if print_mode else 700, 0.06)
    time.sleep(0.6)
    update_osd()

# ---------------------------------------------------------------------------
# power saving: dim + idle on battery, optional deep sleep
# ---------------------------------------------------------------------------
last_activity = 0
idle = False
idle_since = 0
_ok_armed = False
_up_armed = False


def any_button():
    try:
        return bool(pycam.shutter.short_count or pycam.shutter.long_press or
                    pycam.shutter.pressed or
                    pycam.up.fell or pycam.down.fell or pycam.left.fell or
                    pycam.right.fell or pycam.ok.fell or pycam.select.fell)
    except Exception:
        try:                                 # at minimum keep shutter-wake working
            return bool(pycam.shutter.short_count or pycam.shutter.pressed)
        except Exception:
            return False


def enter_idle():
    # kill the big draws (backlight + camera sensor); keep polling buttons
    global idle, idle_since
    try:
        pycam.display.brightness = 0
    except Exception:
        pass
    try:
        pycam.camera.deinit()
    except Exception:
        pass
    idle = True
    idle_since = time.monotonic()


def wake_up():
    global idle, last_activity
    try:
        pycam.init_camera()                  # espcamera has no .init(); rebuild it
        tune_sensor()                    # init_camera() resets saturation to +3
        continuous_focus()               # init_camera() reloads the AF firmware
    except Exception as e:
        print("wake:", e)
    try:
        pycam.display.brightness = 1.0
    except Exception:
        pass
    pycam.tone(1100, 0.03)
    idle = False
    last_activity = time.monotonic()


def deep_sleep():
    # lowest power; wakes on the shutter button (GPIO0) and RESTARTS the program
    try:
        pycam.display.brightness = 0
        pycam.camera.deinit()
        if status_pix is not None:
            status_pix[0] = (0, 0, 0)        # don't let the LED latch through sleep
    except Exception:
        pass
    try:
        pycam.shutter_button.deinit()        # PinAlarm needs exclusive GPIO0
        pin = alarm.pin.PinAlarm(pin=board.BUTTON, value=False, pull=True)
        try:
            microcontroller.watchdog.deinit()   # asleep, nothing feeds it
        except Exception:
            pass
        alarm.exit_and_deep_sleep_until_alarms(pin)   # does not return
    except Exception as e:
        print("deep sleep:", e)
        supervisor.reload()                  # recover cleanly rather than spin

# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
refresh_shot_count()

last_activity = time.monotonic()
last_sync_check = 0
_intro_done = False                      # boot splash + first autofocus

while True:
    feed()
    try:
        plugged = supervisor.runtime.usb_connected
    except Exception:
        plugged = False

    # ---- idle (screen + camera off, still polling) ----
    if idle:
        pycam.keys_debounce()
        if plugged or any_button():
            wake_up()
            continue
        if DEEP_SLEEP_AFTER > 0 and (time.monotonic() - idle_since) > DEEP_SLEEP_AFTER:
            deep_sleep()                     # restarts the program on wake
        time.sleep(0.05)
        continue

    # Boot intro: show the splash FIRST. PyCamera() already initialised the camera
    # and the display (init_display does root_group=splash + refresh), so the
    # root_group swap + refresh inside boot_splash paints on its own - no priming
    # blit is needed. The old pre-splash blit only flashed a raw, unfocused camera
    # frame on top of the splash for one frame. Focus once after; capture stays
    # instant (continuous_capture works straight out of init_camera at HQVGA).
    if not _intro_done:
        if _watchdog_restart:
            print("sd: not tried at power-up (watchdog restart)")
        elif not ensure_sd():
            print("sd: no card at power-up")
        elif not SENSOR_TEST:
            remove_test_shots()
        refresh_shot_count()
        boot_splash()
        continuous_focus()                   # keeps focusing while aiming: no wait at the shot
        if SENSOR_TEST and sd_mounted():
            sensor_test(SENSOR_TEST)
            refresh_shot_count()
        last_activity = time.monotonic()
        _intro_done = True
        continue

    # ---- active: live preview + OSD ----
    try:
        pycam.blit(pycam.continuous_capture())
        update_osd()
        pycam.display.refresh()
    except Exception as e:
        print("preview:", e)

    try:
        read_inputs()
    except Exception as e:
        print("input:", e)

    # drop into idle after inactivity on battery
    if (not plugged) and IDLE_DIM_AFTER > 0 and \
            (time.monotonic() - last_activity) > IDLE_DIM_AFTER:
        enter_idle()
        continue

    # auto-sync when docked (nothing here is latency-sensitive; check rarely)
    now = time.monotonic()
    if now - last_sync_check > 60:
        last_sync_check = now
        try:
            if plugged and pending():
                sync()
        except Exception as e:
            print("sync:", e)
