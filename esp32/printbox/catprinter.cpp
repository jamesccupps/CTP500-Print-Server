#include "catprinter.h"

#include <NimBLEDevice.h>
#include <Preferences.h>

#include <vector>

#include "config.h"

namespace catprinter {
namespace {

const uint32_t IDLE_DISCONNECT_MS = 60000;
// Paced writes: a 5 KB job sent in one 84 ms burst never finished printing (2026-10-30), its buffer overflowed before
// the printer could ask for a pause. 112-byte writes 20 ms apart (~5.6 KB/s) printed; it prints ~2.5 KB/s.
const size_t MAX_WRITE = 112;
const uint32_t WRITE_GAP_MS = 20;

NimBLEClient *client = nullptr;
NimBLERemoteCharacteristic *writeChar = nullptr;
Preferences prefs;
String address;  // "AA:BB:CC:DD:EE:FF" once known
volatile bool paused = false, stateSeen = false;
volatile uint8_t stateFlags = 0;
uint32_t lastUse = 0;
const char *lastStatus = "not found yet";

uint8_t crc8(const uint8_t *data, size_t n) {
  uint8_t crc = 0;
  for (size_t i = 0; i < n; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}

void packet(std::vector<uint8_t> &out, uint8_t cmd, const uint8_t *payload, uint16_t n) {
  out.insert(out.end(), {0x51, 0x78, cmd, 0x00, (uint8_t)(n & 0xFF), (uint8_t)(n >> 8)});
  out.insert(out.end(), payload, payload + n);
  out.push_back(crc8(payload, n));
  out.push_back(0xFF);
}

void onNotify(NimBLERemoteCharacteristic *, uint8_t *d, size_t n, bool) {
  for (size_t i = 0; i + 6 < n; i++) {
    if (d[i] != 0x51 || d[i + 1] != 0x78) continue;
    if (d[i + 2] == 0xAE) {
      paused = d[i + 6] != 0;  // AE 10 = hold on, AE 00 = go on
    } else if (d[i + 2] == 0xA3) {
      stateFlags = d[i + 6];   // 0 = fine; 01 = no paper (an open lid reads the same on this printer)
      stateSeen = true;
    }
  }
}

bool findPrinter() {
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setActiveScan(true);
  // listen half the time: a full-time scan starved the Wi-Fi network and a laptop on it dropped off (2026-10-30)
  scan->setInterval(100);
  scan->setWindow(50);
  NimBLEScanResults found = scan->getResults(8000, false);
  for (int i = 0; i < found.getCount(); i++) {
    const NimBLEAdvertisedDevice *dev = found.getDevice(i);
    if (dev->getName().find(PRINTER_NAME) != std::string::npos ||
        dev->isAdvertisingService(NimBLEUUID((uint16_t)0xAF30))) {
      address = dev->getAddress().toString().c_str();
      prefs.putString("addr", address);
      Serial.printf("printer found: %s\n", address.c_str());
      return true;
    }
  }
  return false;
}

String connect() {
  if (client && client->isConnected() && writeChar) return "";
  if (address.isEmpty() && !findPrinter()) {
    lastStatus = "not found yet";
    return "Can't find the printer: is it on and nearby?";
  }
  if (!client) {
    client = NimBLEDevice::createClient();
    client->setConnectTimeout(8000);
  }
  bool ok = false;
  for (int attempt = 0; attempt < 3 && !ok; attempt++) {
    ok = client->connect(NimBLEAddress(address.c_str(), BLE_ADDR_PUBLIC));
    if (!ok) delay(1000);
  }
  if (!ok) {
    lastStatus = "ready";
    return "Couldn't connect to the printer: is it on, and not connected to a phone or the Pi?";
  }
  NimBLERemoteService *svc = client->getService(NimBLEUUID((uint16_t)0xAE30));
  NimBLERemoteCharacteristic *nt = svc ? svc->getCharacteristic(NimBLEUUID((uint16_t)0xAE02)) : nullptr;
  writeChar = svc ? svc->getCharacteristic(NimBLEUUID((uint16_t)0xAE01)) : nullptr;
  if (!writeChar || !nt || !nt->subscribe(true, onNotify)) {
    client->disconnect();
    writeChar = nullptr;
    return "That printer doesn't have the expected BLE service";
  }
  lastStatus = "connected";
  delay(300);  // the first notification can be missed right after subscribing
  return "";
}

void send(const std::vector<uint8_t> &data) {
  size_t chunk = client->getMTU() > 3 ? client->getMTU() - 3 : 20;
  if (chunk > MAX_WRITE) chunk = MAX_WRITE;
  for (size_t i = 0; i < data.size(); i += chunk) {
    uint32_t waited = 0;
    while (paused && waited < 20000) {
      delay(10);
      waited += 10;
    }
    size_t n = data.size() - i < chunk ? data.size() - i : chunk;
    writeChar->writeValue(data.data() + i, n, false);
    delay(WRITE_GAP_MS);
  }
}

// Ask the printer how it is; true if it answered (flags in stateFlags)
bool askState(uint32_t waitMs) {
  std::vector<uint8_t> q;
  uint8_t zero = 0;
  packet(q, 0xA3, &zero, 1);
  stateSeen = false;
  writeChar->writeValue(q.data(), q.size(), false);
  for (uint32_t t = 0; t < waitMs && !stateSeen; t += 20) delay(20);
  return stateSeen;
}

}  // namespace

void begin() {
  prefs.begin("printbox", false);
  address = prefs.getString("addr", "");
  NimBLEDevice::init("printbox");
  NimBLEDevice::setMTU(247);
  lastStatus = address.isEmpty() ? "not found yet" : "ready";
}

String print(const uint8_t *rows, int height, int feedRows, uint8_t level, uint16_t energy) {
  String err = connect();
  if (err.length()) return err;
  if (askState(1500) && (stateFlags & 0x03)) {
    lastStatus = "out of paper or lid open";
    return "The printer says it's out of paper or the lid is open";
  }
  static const uint8_t LSB[256] = {
#define R2(n) n, n + 2 * 64, n + 1 * 64, n + 3 * 64
#define R4(n) R2(n), R2(n + 2 * 16), R2(n + 1 * 16), R2(n + 3 * 16)
#define R6(n) R4(n), R4(n + 2 * 4), R4(n + 1 * 4), R4(n + 3 * 4)
      R6(0), R6(2), R6(1), R6(3)};  // bit-reversed bytes: the printer wants the leftmost dot in bit 0
  std::vector<uint8_t> job;
  job.reserve((size_t)(height + feedRows) * 56 + 64);
  uint8_t lv = 0x30 + level, mode = 0, speed = 20, zero = 0;
  uint8_t en[2] = {(uint8_t)(energy & 0xFF), (uint8_t)(energy >> 8)};
  packet(job, 0xA4, &lv, 1);
  packet(job, 0xAF, en, 2);
  packet(job, 0xBE, &mode, 1);
  packet(job, 0xBD, &speed, 1);
  uint8_t row[PAPER_DOTS / 8];
  for (int y = 0; y < height + feedRows; y++) {
    for (int x = 0; x < PAPER_DOTS / 8; x++) row[x] = y < height ? LSB[rows[y * (PAPER_DOTS / 8) + x]] : 0;
    packet(job, 0xA2, row, sizeof row);
    if ((y + 1) % 200 == 0) packet(job, 0xBD, &speed, 1);
  }
  packet(job, 0xBD, &zero, 1);
  packet(job, 0xA3, &zero, 1);  // answered once the printer gets here: that's "printed"
  stateSeen = false;
  send(job);
  uint32_t limit = 8000 + (uint32_t)(height + feedRows) * 25;
  for (uint32_t t = 0; t < limit && !stateSeen; t += 50) delay(50);
  lastUse = millis();
  if (!stateSeen) return "The printer didn't confirm the print (it may still have printed)";
  if (stateFlags & 0x03) {
    lastStatus = "out of paper or lid open";
    return "The printer ran out of paper (or the lid opened) partway through";
  }
  lastStatus = "connected";
  return "";
}

void idle() {
  if (client && client->isConnected() && millis() - lastUse > IDLE_DISCONNECT_MS) {
    client->disconnect();
    writeChar = nullptr;
    lastStatus = "ready";
  }
}

const char *status() {
  if (client && !client->isConnected() && strcmp(lastStatus, "connected") == 0) lastStatus = "ready";
  return lastStatus;
}

}  // namespace catprinter
