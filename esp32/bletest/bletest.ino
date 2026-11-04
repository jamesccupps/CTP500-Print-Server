// BLE test for the portable print box: can this ESP32-S3 drive the CTP500 ("Mini Printer-1025") over BLE?
// Connects by address, finds service AE30 (write AE01, notify AE02), asks for state (A3) and firmware (A8),
// then prints a short strip of bars. Output on the USB serial port.
#include <NimBLEDevice.h>

static const char *PRINTER_ADDR = "AA:BB:CC:DD:EE:FF";   // your printer (python -m ctp500 find)

static uint8_t crc8(const uint8_t *data, size_t n) {
  uint8_t crc = 0;
  for (size_t i = 0; i < n; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}

static void packet(std::vector<uint8_t> &out, uint8_t cmd, const uint8_t *payload, uint16_t n) {
  out.insert(out.end(), {0x51, 0x78, cmd, 0x00, (uint8_t)(n & 0xFF), (uint8_t)(n >> 8)});
  out.insert(out.end(), payload, payload + n);
  out.push_back(crc8(payload, n));
  out.push_back(0xFF);
}

static volatile bool paused = false, jobDone = false;
static std::vector<uint8_t> replies;

static void onNotify(NimBLERemoteCharacteristic *, uint8_t *data, size_t len, bool) {
  replies.insert(replies.end(), data, data + len);
  for (size_t i = 0; i + 6 < len; i++) {
    if (data[i] == 0x51 && data[i + 1] == 0x78 && data[i + 2] == 0xAE) paused = data[i + 6] != 0;   // flow control
    if (data[i] == 0x51 && data[i + 1] == 0x78 && data[i + 2] == 0xA3) jobDone = true;
  }
}

static void dump(const char *label) {
  Serial.printf("%s (%u bytes):", label, (unsigned)replies.size());
  for (uint8_t b : replies) Serial.printf(" %02x", b);
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  delay(3000);
  Serial.println("\nBLE test starting");
  NimBLEDevice::init("printbox");
  NimBLEDevice::setMTU(247);
  NimBLEClient *client = NimBLEDevice::createClient();
  client->setConnectTimeout(10000);
  NimBLEAddress addr(PRINTER_ADDR, BLE_ADDR_PUBLIC);
  bool ok = false;
  for (int attempt = 1; attempt <= 4 && !ok; attempt++) {
    uint32_t t = millis();
    ok = client->connect(addr);
    Serial.printf("connect attempt %d: %s in %lu ms\n", attempt, ok ? "ok" : "failed", (unsigned long)(millis() - t));
    if (!ok) delay(1500);
  }
  if (!ok) { Serial.println("RESULT could not connect"); return; }
  Serial.printf("MTU %u\n", client->getMTU());
  NimBLERemoteService *svc = client->getService(NimBLEUUID((uint16_t)0xAE30));
  NimBLERemoteCharacteristic *wr = svc ? svc->getCharacteristic(NimBLEUUID((uint16_t)0xAE01)) : nullptr;
  NimBLERemoteCharacteristic *nt = svc ? svc->getCharacteristic(NimBLEUUID((uint16_t)0xAE02)) : nullptr;
  if (!wr || !nt) { Serial.println("RESULT printer service not found"); return; }
  Serial.printf("subscribe: %d\n", nt->subscribe(true, onNotify));

  uint8_t zero = 0;
  std::vector<uint8_t> q;
  packet(q, 0xA3, &zero, 1);
  packet(q, 0xA8, &zero, 1);
  wr->writeValue(q.data(), 9, false);
  delay(600);
  wr->writeValue(q.data() + 9, 9, false);
  delay(800);
  dump("replies");

  // the job: 24 rows of bars, 64 blank rows to feed it out, then A3 (answered when it's done)
  std::vector<uint8_t> job;
  uint8_t level = 0x33, speed = 20, mode = 0;
  uint8_t energy[2] = {(uint8_t)(14000 & 0xFF), (uint8_t)(14000 >> 8)};
  packet(job, 0xA4, &level, 1);
  packet(job, 0xAF, energy, 2);
  packet(job, 0xBE, &mode, 1);
  packet(job, 0xBD, &speed, 1);
  uint8_t row[48];
  for (int y = 0; y < 24 + 64; y++) {
    for (int x = 0; x < 48; x++) row[x] = (y < 24 && (x / 6) % 2 == 0) ? 0xFF : 0x00;
    packet(job, 0xA2, row, 48);
  }
  speed = 0;
  packet(job, 0xBD, &speed, 1);
  packet(job, 0xA3, &zero, 1);

  replies.clear();
  // Paced: the whole job in one 84 ms burst (240-byte writes, 4 ms apart) never finished printing, so the
  // printer's buffer must have overflowed before its pause request arrived. ~5 KB/s is still twice what it prints.
  size_t chunk = client->getMTU() - 3;
  if (chunk > 112) chunk = 112;
  uint32_t t = millis();
  int pauses = 0;
  for (size_t i = 0; i < job.size(); i += chunk) {
    while (paused) { pauses++; delay(10); }
    size_t n = (job.size() - i < chunk) ? job.size() - i : chunk;
    wr->writeValue(job.data() + i, n, false);
    delay(20);
  }
  Serial.printf("sent %u bytes in %lu ms, chunk %u, pause waits %d\n", (unsigned)job.size(), (unsigned long)(millis() - t),
                (unsigned)chunk, pauses);
  uint32_t deadline = millis() + 15000;
  while (!jobDone && millis() < deadline) delay(20);
  Serial.printf("RESULT %s after %lu ms\n", jobDone ? "printed" : "no end-of-job reply", (unsigned long)(millis() - t));
  dump("end replies");
  client->disconnect();
}

void loop() { delay(1000); }
