// Portable print box: an ESP32-S3 (16 MB flash, 8 MB PSRAM) that prints photos on the CTP500 over BLE, for when the
// print server isn't around. The camera and a phone join its Wi-Fi network.
#pragma once

// The box's Wi-Fi network: copy secrets.example.h to secrets.h (kept out of git) and pick a password. The camera's
// settings.toml needs the same name and password as CAMERA_AWAY_SSID / CAMERA_AWAY_PASSWORD.
#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy secrets.example.h to secrets.h and set the box's Wi-Fi name and password"
#endif
#define BOX_CHANNEL 6

// The printer, over BLE. Found by name the first time, then remembered (NVS).
#define PRINTER_NAME "Mini Printer"

// Prints match the server's camera prints: Photo style with the punchy tones (picked on paper, 2026-10-30),
// landscape shots sideways (along the paper, as big as the paper allows), no frame, normal darkness.
#define PAPER_DOTS 384
#define MARGIN_DOTS 6            // render.py's default margin
#define PAD_TOP_ROWS 48          // 6 mm
#define PAD_BOTTOM_ROWS 48       // 6 mm
#define FEED_ROWS 64             // 2 lines: past the tear bar
#define DARKNESS_LEVEL 3         // catproto "normal": level 3, energy 14000
#define DARKNESS_ENERGY 14000
#define DETAIL 0.5f              // unsharp amount, as camera_detail
#define SIDEWAYS true

#define MAX_UPLOAD (6 * 1024 * 1024)
#define QUEUE_LENGTH 3
