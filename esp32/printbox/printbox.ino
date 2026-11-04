// Portable print box for the CTP500: an ESP32-S3 on a USB battery that makes its own Wi-Fi network (BOX_SSID in
// secrets.h), takes photos from the camera (the same /api/camera/print call it sends the print server) and from a
// phone (the page at http://192.168.4.1), turns them into print rows like the server's Photo style, and prints
// them over BLE.
// Built 2026-10-30 because the printer only does BLE besides Classic Bluetooth, and the ESP32-S3 has only BLE.
//
// Board: ESP32S3 Dev Module, 16 MB flash, OPI PSRAM, USB CDC on boot (see ../README.md for the build line).
#include <WiFi.h>

#include "catprinter.h"
#include "config.h"
#include "page.h"
#include "photo.h"

struct Job {
  uint8_t *data;
  size_t len;
  char name[48];
};

static QueueHandle_t jobs;
static volatile int waiting = 0;
static volatile bool printing = false;
static char lastResult[160] = "";
static NetworkServer http(80);

static void led(uint8_t r, uint8_t g, uint8_t b) {
#ifdef RGB_BUILTIN
  rgbLedWrite(RGB_BUILTIN, r, g, b);
#endif
}

// ------------------------------------------------------------------ printing (its own task, so the web server
// keeps answering while a print takes its ~15 s)

static void printTask(void *) {
  for (;;) {
    Job job;
    if (xQueueReceive(jobs, &job, pdMS_TO_TICKS(5000)) != pdTRUE) {
      catprinter::idle();
      continue;
    }
    waiting--;
    printing = true;
    led(0, 0, 24);
    uint32_t t0 = millis();
    uint8_t *rows = nullptr;
    String err;
    int height = photo::render(job.data, job.len, &rows, err);
    free(job.data);
    uint32_t drawn = millis() - t0;
    if (height > 0) err = catprinter::print(rows, height, FEED_ROWS, DARKNESS_LEVEL, DARKNESS_ENERGY);
    free(rows);
    snprintf(lastResult, sizeof lastResult, "%s %s (drawn in %.1f s, done in %.1f s)", job.name,
             err.length() ? err.c_str() : "printed", drawn / 1000.0, (millis() - t0) / 1000.0);
    Serial.println(lastResult);
    printing = false;
    if (err.length()) led(32, 0, 0); else led(0, 12, 0);
  }
}

// ------------------------------------------------------------------ a small HTTP server: the camera's call, the
// phone page and its status. Bodies go straight into PSRAM (a camera JPEG is ~60 KB, a shrunk phone photo ~200 KB).

static void reply(NetworkClient &c, int code, const char *ctype, const char *body, size_t len) {
  const char *text = code == 200 ? "OK" : code == 404 ? "Not Found" : code == 400 ? "Bad Request" :
                     code == 413 ? "Payload Too Large" : "Service Unavailable";
  c.printf("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %u\r\nCache-Control: no-store\r\n"
           "Connection: close\r\n\r\n", code, text, ctype, (unsigned)len);
  size_t sent = 0;
  while (sent < len) {
    size_t n = c.write((const uint8_t *)body + sent, min((size_t)4096, len - sent));
    if (n == 0) break;
    sent += n;
  }
}

static void json(NetworkClient &c, int code, const String &body) {
  reply(c, code, "application/json", body.c_str(), body.length());
}

static String quoted(const char *s) {
  String out = "\"";
  for (; *s; s++) {
    if (*s == '"' || *s == '\\') out += '\\';
    if ((uint8_t)*s >= 0x20) out += *s;
  }
  return out + "\"";
}

static bool readLine(NetworkClient &c, String &line, uint32_t timeoutMs) {
  line = "";
  uint32_t t = millis();
  while (millis() - t < timeoutMs) {
    while (c.available()) {
      char ch = c.read();
      if (ch == '\n') return true;
      if (ch != '\r' && line.length() < 512) line += ch;
    }
    if (!c.connected()) return false;
    delay(1);
  }
  return false;
}

static void handle(NetworkClient c) {
  String request, line;
  if (!readLine(c, request, 3000)) return;
  size_t length = 0;
  String name = "photo.jpg";
  while (readLine(c, line, 3000) && line.length()) {
    String lower = line;
    lower.toLowerCase();
    if (lower.startsWith("content-length:")) length = strtoul(line.c_str() + 15, nullptr, 10);
    else if (lower.startsWith("x-filename:")) name = line.substring(11);
  }
  name.trim();
  int sp1 = request.indexOf(' '), sp2 = request.indexOf(' ', sp1 + 1);
  String method = request.substring(0, sp1), path = request.substring(sp1 + 1, sp2 > 0 ? sp2 : request.length());
  int q = path.indexOf('?');
  if (q >= 0) path = path.substring(0, q);
  if (path != "/api/status") {
    Serial.printf("%s %s (%u bytes) from %s\n", method.c_str(), path.c_str(), (unsigned)length,
                  c.remoteIP().toString().c_str());
  }

  if (method == "GET" && (path == "/" || path == "/index.html")) {
    reply(c, 200, "text/html; charset=utf-8", PAGE_HTML, strlen(PAGE_HTML));
    return;
  }
  if (method == "GET" && path == "/api/status") {
    String body = String("{\"box\":") + quoted(BOX_SSID) + ",\"printer\":" + quoted(catprinter::status()) +
                  ",\"printing\":" + (printing ? "true" : "false") + ",\"queued\":" + String((int)waiting) +
                  ",\"last\":" + quoted(lastResult) + "}";
    json(c, 200, body);
    return;
  }
  if (method != "POST") {
    json(c, 404, "{\"error\":\"not here\"}");
    return;
  }
  // POST: read the whole body first (answering without reading it resets the connection on some clients)
  if (length == 0 || length > MAX_UPLOAD) {
    json(c, length ? 413 : 400, "{\"error\":\"send a JPEG photo as the request body\"}");
    return;
  }
  uint8_t *body = (uint8_t *)ps_malloc(length);
  if (!body) {
    json(c, 503, "{\"error\":\"out of memory\"}");
    return;
  }
  size_t got = 0;
  uint32_t last = millis();
  while (got < length && millis() - last < 15000) {
    int n = c.read(body + got, length - got);
    if (n > 0) {
      got += n;
      last = millis();
    } else {
      if (!c.connected()) break;
      delay(2);
    }
  }
  if (got < length) {
    Serial.printf("  body stopped at %u of %u bytes\n", (unsigned)got, (unsigned)length);
    free(body);
    return;  // the sender gave up
  }
  if (path == "/api/camera/upload") {
    // the camera's sync: photos belong on the home print server, so refuse in a way it retries later (5xx)
    free(body);
    json(c, 503, "{\"error\":\"the print box doesn't keep photos; they sync at home\"}");
    return;
  }
  if (path != "/api/camera/print" && path != "/api/print/photo") {
    free(body);
    json(c, 404, "{\"error\":\"not here\"}");
    return;
  }
  Job job{body, length, {0}};
  strncpy(job.name, name.c_str(), sizeof job.name - 1);
  if (xQueueSend(jobs, &job, 0) != pdTRUE) {
    free(body);
    json(c, 503, "{\"error\":\"the printer is busy, try again in a moment\"}");
    return;
  }
  waiting++;
  json(c, 200, String("{\"name\":") + quoted(job.name) + ",\"job\":{\"status\":\"queued\"}}");
}

// ------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\nprint box: PSRAM %u KB free\n", (unsigned)(ESP.getFreePsram() / 1024));
  led(12, 8, 0);
  WiFi.mode(WIFI_AP);
  WiFi.softAP(BOX_SSID, BOX_PASSWORD, BOX_CHANNEL, 0, 4);
  Serial.printf("Wi-Fi \"%s\" up, page at http://%s\n", BOX_SSID, WiFi.softAPIP().toString().c_str());
  http.begin();
  catprinter::begin();
  jobs = xQueueCreate(QUEUE_LENGTH, sizeof(Job));
  xTaskCreatePinnedToCore(printTask, "print", 12288, nullptr, 1, nullptr, 0);
  led(0, 12, 0);
}

void loop() {
  NetworkClient c = http.accept();
  if (c) {
    c.setNoDelay(true);
    handle(c);
    c.stop();
  }
  delay(2);
}
