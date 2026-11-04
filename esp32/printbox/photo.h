// A JPEG photo to print rows, the way the Pi prints camera photos in the Photo style (punchy tones): dehaze as much
// as the photo needs, stretch contrast, sharpen, lift the midtones (gamma 0.8), Atkinson dithering; landscape shots
// sideways. Ported from ctp500/styles.py thermal_photo and render.py _sideways (no AI outlines on the box).
#pragma once
#include <Arduino.h>

namespace photo {

// Packed rows, 48 bytes each (MSB = leftmost dot, 1 = black), blank paper above and below included.
// Returns the number of rows (0 on failure, with err set); *rows is heap memory the caller frees.
int render(const uint8_t *jpeg, size_t len, uint8_t **rows, String &err);

}  // namespace photo
