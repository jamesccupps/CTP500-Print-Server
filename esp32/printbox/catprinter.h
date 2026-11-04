// The CTP500 ("Mini Printer-1025") over BLE: the same "cat printer" packets the Pi sends over Classic Bluetooth
// (51 78 cmd 00 len data crc8 FF), written to service AE30 / characteristic AE01, replies and flow control on AE02.
#pragma once
#include <Arduino.h>

namespace catprinter {

void begin();
// Print packed rows (48 bytes per row, MSB = leftmost dot, 1 = black) followed by feedRows of blank paper.
// Returns an empty string when the printer confirmed the job, else what went wrong.
String print(const uint8_t *rows, int height, int feedRows, uint8_t level, uint16_t energy);
// Let go of the printer after a while unused (it can sleep, and its phone app can connect).
void idle();
// "connected", "ready" (known, not connected), "not found yet", "out of paper or lid open"
const char *status();

}  // namespace catprinter
