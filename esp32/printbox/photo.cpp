#include "photo.h"

#include <JPEGDEC.h>
#include <math.h>

#include "config.h"

// Speed and the watchdog: the first version took 3+ s of solid work on one core (column-wise passes over PSRAM),
// and on a phone photo the task watchdog rebooted the box mid-print (2026-10-30). Now every pass walks memory
// row by row, the dehaze's wide blurs run on a quarter-size grid (they only feed a smooth mean/spread), and the
// work pauses for a tick every few rows so the idle task and Wi-Fi keep running.

namespace photo {
namespace {

JPEGDEC decoder;  // large: one, used by the print task only

struct Gray {
  int w = 0, h = 0;
  uint8_t *px = nullptr;
};

inline void breathe(int y) {
  if ((y & 15) == 15) vTaskDelay(1);
}

int onDraw(JPEGDRAW *d) {
  Gray *g = (Gray *)d->pUser;
  const uint8_t *src = (const uint8_t *)d->pPixels;
  int used = d->iWidthUsed > 0 ? d->iWidthUsed : d->iWidth;
  for (int y = 0; y < d->iHeight; y++) {
    int gy = d->y + y;
    if (gy >= g->h) break;
    int n = min(used, g->w - d->x);
    if (n > 0) memcpy(g->px + gy * g->w + d->x, src + y * d->iWidth, n);
  }
  return 1;
}

float *fbuf(size_t n) { return (float *)ps_malloc(n * sizeof(float)); }

// Area-average downscale (bilinear when enlarging): rows first, then whole rows mixed together.
void resize(const uint8_t *src, int sw, int sh, float *dst, int tw, int th) {
  float *tmp = fbuf((size_t)tw * sh);
  float sx = (float)sw / tw, sy = (float)sh / th;
  for (int y = 0; y < sh; y++) {
    const uint8_t *row = src + (size_t)y * sw;
    float *out = tmp + (size_t)y * tw;
    for (int x = 0; x < tw; x++) {
      if (sx <= 1) {
        float fx = (x + 0.5f) * sx - 0.5f;
        int x0 = max(0, (int)floorf(fx)), x1 = min(sw - 1, x0 + 1);
        float t = fx - floorf(fx);
        out[x] = row[x0] * (1 - t) + row[x1] * t;
      } else {
        float a = x * sx, b = a + sx, acc = 0, w = 0;
        for (int i = (int)a; i < (int)ceilf(b) && i < sw; i++) {
          float cover = min(b, (float)i + 1) - max(a, (float)i);
          acc += row[i] * cover;
          w += cover;
        }
        out[x] = acc / w;
      }
    }
    breathe(y);
  }
  for (int y = 0; y < th; y++) {
    float *out = dst + (size_t)y * tw;
    if (sy <= 1) {
      float fy = (y + 0.5f) * sy - 0.5f;
      int y0 = max(0, (int)floorf(fy)), y1 = min(sh - 1, y0 + 1);
      float t = fy - floorf(fy);
      const float *r0 = tmp + (size_t)y0 * tw, *r1 = tmp + (size_t)y1 * tw;
      for (int x = 0; x < tw; x++) out[x] = r0[x] * (1 - t) + r1[x] * t;
    } else {
      float a = y * sy, b = a + sy, w = 0;
      for (int x = 0; x < tw; x++) out[x] = 0;
      for (int i = (int)a; i < (int)ceilf(b) && i < sh; i++) {
        float cover = min(b, (float)i + 1) - max(a, (float)i);
        const float *r = tmp + (size_t)i * tw;
        for (int x = 0; x < tw; x++) out[x] += r[x] * cover;
        w += cover;
      }
      for (int x = 0; x < tw; x++) out[x] /= w;
    }
    breathe(y);
  }
  free(tmp);
}

// Gaussian blur of sigma ~ three box passes, each a running sum along rows, then down columns with one running sum
// per column (so memory is always read in order). scratch: w*h floats; line: max(w, h) floats.
void gaussian(float *a, int w, int h, float sigma, float *scratch, float *line) {
  int r = (int)lroundf((sqrtf(12 * sigma * sigma / 3 + 1) - 1) / 2);
  if (r < 1) return;
  float norm = 1.0f / (2 * r + 1);
  for (int pass = 0; pass < 3; pass++) {
    for (int y = 0; y < h; y++) {
      float *row = a + (size_t)y * w, acc = 0;
      for (int i = -r; i <= r; i++) acc += row[min(max(i, 0), w - 1)];
      for (int x = 0; x < w; x++) {
        line[x] = acc * norm;
        acc += row[min(x + r + 1, w - 1)] - row[max(x - r, 0)];
      }
      memcpy(row, line, w * sizeof(float));
      breathe(y);
    }
    float *acc = line;  // one running sum per column
    for (int x = 0; x < w; x++) acc[x] = 0;
    for (int i = -r; i <= r; i++) {
      const float *row = a + (size_t)min(max(i, 0), h - 1) * w;
      for (int x = 0; x < w; x++) acc[x] += row[x];
    }
    for (int y = 0; y < h; y++) {
      float *out = scratch + (size_t)y * w;
      const float *add = a + (size_t)min(y + r + 1, h - 1) * w, *sub = a + (size_t)max(y - r, 0) * w;
      for (int x = 0; x < w; x++) {
        out[x] = acc[x] * norm;
        acc[x] += add[x] - sub[x];
      }
      breathe(y);
    }
    memcpy(a, scratch, (size_t)w * h * sizeof(float));
  }
}

// ai.local_contrast + ai.dehaze_strength: brightness relative to the neighbourhood, mixed in as much as the photo is
// hazy (MEMENTO dim-room shots: 0.55, clear shots ~0.2). The neighbourhood mean and spread are smooth (sigma ~2.3% of
// the width), so they're worked out on a quarter-size grid and sampled back. Values 0..1.
void dehaze(float *lum, int w, int h) {
  const int f = 4;
  int sw = (w + f - 1) / f, sh = (h + f - 1) / f;
  size_t sn = (size_t)sw * sh;
  float *small = fbuf(sn), *mean = fbuf(sn), *sq = fbuf(sn), *scratch = fbuf(sn), *line = fbuf(max(sw, sh));
  for (int y = 0; y < sh; y++) {
    for (int x = 0; x < sw; x++) {
      float acc = 0;
      int n = 0;
      for (int yy = y * f; yy < min(h, y * f + f); yy++)
        for (int xx = x * f; xx < min(w, x * f + f); xx++, n++) acc += lum[(size_t)yy * w + xx];
      small[(size_t)y * sw + x] = acc / n;
    }
    breathe(y);
  }
  auto blurBoth = [&](float sigma) {
    for (size_t i = 0; i < sn; i++) {
      mean[i] = small[i];
      sq[i] = small[i] * small[i];
    }
    gaussian(mean, sw, sh, sigma / f, scratch, line);
    gaussian(sq, sw, sh, sigma / f, scratch, line);
  };
  // haze(): spread within ~12-px (of 512) neighbourhoods, averaged over the middle of the picture
  blurBoth(12.0f * w / 512);
  double c = 0;
  int cn = 0;
  for (int y = sh / 4; y < 3 * sh / 4; y++)
    for (int x = sw / 4; x < 3 * sw / 4; x++, cn++) {
      size_t i = (size_t)y * sw + x;
      c += sqrtf(fmaxf(0, sq[i] - mean[i] * mean[i]));
    }
  c /= max(1, cn);
  float strength = fminf(0.55f, 0.2f + 0.5f * fminf(1, fmaxf(0, (0.10f - (float)c) / 0.07f)));
  blurBoth(fmaxf(2.0f, w * 0.0234f));
  for (size_t i = 0; i < sn; i++) sq[i] = sqrtf(fmaxf(0, sq[i] - mean[i] * mean[i])) + 0.04f;  // now the spread
  for (int y = 0; y < h; y++) {
    float fy = fminf(fmaxf((y + 0.5f) / f - 0.5f, 0), sh - 1.001f);
    int y0 = (int)fy;
    float ty = fy - y0;
    const float *m0 = mean + (size_t)y0 * sw, *m1 = mean + (size_t)min(y0 + 1, sh - 1) * sw;
    const float *s0 = sq + (size_t)y0 * sw, *s1 = sq + (size_t)min(y0 + 1, sh - 1) * sw;
    float *row = lum + (size_t)y * w;
    for (int x = 0; x < w; x++) {
      float fx = fminf(fmaxf((x + 0.5f) / f - 0.5f, 0), sw - 1.001f);
      int x0 = (int)fx, x1 = min(x0 + 1, sw - 1);
      float tx = fx - x0;
      float m = (m0[x0] * (1 - tx) + m0[x1] * tx) * (1 - ty) + (m1[x0] * (1 - tx) + m1[x1] * tx) * ty;
      float s = (s0[x0] * (1 - tx) + s0[x1] * tx) * (1 - ty) + (s1[x0] * (1 - tx) + s1[x1] * tx) * ty;
      float norm = fminf(1, fmaxf(0, 0.5f + (row[x] - m) / s * 0.18f));
      row[x] = (1 - strength) * row[x] + strength * norm;
    }
    breathe(y);
  }
  Serial.printf("haze %.3f -> dehaze %.2f\n", c, strength);
  free(small);
  free(mean);
  free(sq);
  free(scratch);
  free(line);
}

// The rest of thermal_photo: autocontrast (1% cutoff), unsharp mask, gamma 0.8 (punchy), Atkinson. Values 0..1 in,
// ink mask out (1 = black).
void tonesAndDither(float *lum, int w, int h, uint8_t *ink) {
  size_t n = (size_t)w * h;
  int hist[256] = {0};
  for (size_t i = 0; i < n; i++) hist[min(255, max(0, (int)(lum[i] * 255 + 0.5f)))]++;
  size_t cut = n / 100, acc = 0;
  int lo = 0, hi = 255;
  for (lo = 0; lo < 255 && (acc += hist[lo]) <= cut; lo++) {}
  acc = 0;
  for (hi = 255; hi > 0 && (acc += hist[hi]) <= cut; hi--) {}
  float span = hi > lo ? (hi - lo) / 255.0f : 1, base = lo / 255.0f;
  float *blur = fbuf(n), *scratch = fbuf(n), *line = fbuf(max(w, h));
  for (size_t i = 0; i < n; i++) lum[i] = blur[i] = fminf(1, fmaxf(0, (lum[i] - base) / span));
  gaussian(blur, w, h, 1.6f, scratch, line);
  free(scratch);
  free(line);
  static float gamma[1025];
  for (int i = 0; i <= 1024; i++) gamma[i] = 255 * powf(i / 1024.0f, 0.8f);
  float amount = (60 + 100 * DETAIL) / 100.0f, threshold = 2 / 255.0f;
  for (size_t i = 0; i < n; i++) {
    float diff = lum[i] - blur[i];
    float v = fminf(1, fmaxf(0, fabsf(diff) >= threshold ? lum[i] + amount * diff : lum[i]));
    lum[i] = gamma[(int)(v * 1024 + 0.5f)];  // now 0..255 for the dither
  }
  free(blur);
  // Atkinson: 1/8 of the error to six neighbours (3/4 in all), so light areas stay clean on thermal paper
  for (int y = 0; y < h; y++) {
    float *row = lum + (size_t)y * w;
    float *next = y + 1 < h ? row + w : nullptr, *next2 = y + 2 < h ? row + 2 * w : nullptr;
    uint8_t *out = ink + (size_t)y * w;
    for (int x = 0; x < w; x++) {
      float old = row[x], val = old >= 128 ? 255 : 0;
      out[x] = val == 0;
      float err = (old - val) / 8;
      if (err == 0) continue;
      if (x + 1 < w) row[x + 1] += err;
      if (x + 2 < w) row[x + 2] += err;
      if (next) {
        if (x > 0) next[x - 1] += err;
        next[x] += err;
        if (x + 1 < w) next[x + 1] += err;
      }
      if (next2) next2[x] += err;
    }
    breathe(y);
  }
}

}  // namespace

int render(const uint8_t *jpeg, size_t len, uint8_t **rows, String &err) {
  *rows = nullptr;
  if (len < 4 || jpeg[0] != 0xFF || jpeg[1] != 0xD8) {
    err = "That isn't a JPEG photo";
    return 0;
  }
  Gray g;
  if (!decoder.openRAM((uint8_t *)jpeg, (int)len, onDraw)) {
    err = "Couldn't read the photo";
    return 0;
  }
  int jw = decoder.getWidth(), jh = decoder.getHeight();
  bool sideways = SIDEWAYS && jw > jh;
  // the picture's size on paper: sideways, as tall as the paper (minus the margins) and as long as it needs
  int ph = sideways ? PAPER_DOTS - 2 * MARGIN_DOTS : (int)lroundf((float)PAPER_DOTS * jh / jw);
  int pw = sideways ? (int)lroundf((float)ph * jw / jh) : PAPER_DOTS;
  // decode at the smallest scale that still has at least the print's pixels
  int scale = 1, opt = 0;
  const int scales[3][2] = {{8, JPEG_SCALE_EIGHTH}, {4, JPEG_SCALE_QUARTER}, {2, JPEG_SCALE_HALF}};
  for (auto &s : scales) {
    if (jw / s[0] >= pw && jh / s[0] >= ph) {
      scale = s[0];
      opt = s[1];
      break;
    }
  }
  g.w = (jw + scale - 1) / scale;
  g.h = (jh + scale - 1) / scale;
  g.px = (uint8_t *)ps_calloc((size_t)g.w * g.h, 1);
  if (!g.px) {
    decoder.close();
    err = "Not enough memory for that photo";
    return 0;
  }
  decoder.setPixelType(EIGHT_BIT_GRAYSCALE);
  decoder.setUserPointer(&g);
  uint32_t t0 = millis();
  int ok = decoder.decode(0, 0, opt);
  decoder.close();
  if (!ok) {
    free(g.px);
    err = "Couldn't decode the photo";
    return 0;
  }
  uint32_t t1 = millis();

  size_t n = (size_t)pw * ph;
  float *lum = fbuf(n);
  uint8_t *ink = (uint8_t *)ps_malloc(n);
  if (!lum || !ink) {
    free(g.px);
    free(lum);
    free(ink);
    err = "Not enough memory for that photo";
    return 0;
  }
  resize(g.px, g.w, g.h, lum, pw, ph);
  free(g.px);
  for (size_t i = 0; i < n; i++) lum[i] /= 255;
  uint32_t t2 = millis();
  dehaze(lum, pw, ph);
  uint32_t t3 = millis();
  tonesAndDither(lum, pw, ph, ink);
  free(lum);
  Serial.printf("photo %dx%d: decode %lu ms (1/%d of %dx%d), resize %lu, dehaze %lu, tones+dither %lu\n", pw, ph,
                (unsigned long)(t1 - t0), scale, jw, jh, (unsigned long)(t2 - t1), (unsigned long)(t3 - t2),
                (unsigned long)(millis() - t3));

  // lay it on the paper: sideways shots are turned a quarter clockwise (render.py: ROTATE_270), so the picture's top
  // runs along the right edge of the strip; margins as render.py
  int bodyRows = sideways ? pw : ph;
  int height = PAD_TOP_ROWS + bodyRows + PAD_BOTTOM_ROWS;
  const int rb = PAPER_DOTS / 8;
  uint8_t *out = (uint8_t *)ps_calloc((size_t)height * rb, 1);
  if (!out) {
    free(ink);
    err = "Not enough memory for that photo";
    return 0;
  }
  for (int y = 0; y < bodyRows; y++) {
    uint8_t *row = out + (size_t)(PAD_TOP_ROWS + y) * rb;
    for (int x = 0; x < PAPER_DOTS; x++) {
      bool black;
      if (sideways) {
        // strip (x, y) <- landscape canvas (y, PAPER_DOTS - 1 - x); the picture sits MARGIN_DOTS into the canvas
        int cy = PAPER_DOTS - 1 - x - MARGIN_DOTS;
        black = cy >= 0 && cy < ph && ink[(size_t)cy * pw + y];
      } else {
        black = ink[(size_t)y * pw + x];
      }
      if (black) row[x >> 3] |= 0x80 >> (x & 7);
    }
    breathe(y);
  }
  free(ink);
  *rows = out;
  return height;
}

}  // namespace photo
