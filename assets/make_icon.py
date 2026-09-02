#!/usr/bin/env python3
"""Regenerate studon-client.png (the tray icon) from studon-client-source.png.

The source is the StudOn wordmark as downloaded: dark navy on transparent, with
a soft grey drop shadow. That is close to unreadable on a dark Plasma panel, so
this script

  1. drops the shadow  — the logo is saturated blue, the shadow is grey, so
     low-saturation pixels can simply go;
  2. trims to the logo's bounding box and re-centres it in a square, so the
     glyph fills the 22px the tray actually gives it instead of floating in the
     empty margin the shadow used to occupy;
  3. lifts every pixel's lightness by a constant offset (hue and alpha
     untouched), so the anti-aliased edges stay smooth and the brand hue
     survives.

Usage:  python3 make_icon.py [target_lightness] [margin]   (defaults 0.76, 0.0)

MARGIN is the transparent padding kept around the glyph, as a fraction of its
longest side. 0.0 makes the logo fill the square edge to edge, which is as large
as it can get without clipping: the wordmark is 138x146, so a square canvas
already leaves 4px of slack on the left and right and none top to bottom.
"""
import colorsys
import os
import sys
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "studon-client-source.png")
TARGET = os.path.join(HERE, "studon-client.png")

# Grey-vs-blue cutoff: logo pixels span ~69 in max(rgb)-min(rgb), shadow ~10.
SATURATION_CUTOFF = 25


def drop_shadow(im):
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a and max(r, g, b) - min(r, g, b) < SATURATION_CUTOFF:
                px[x, y] = (0, 0, 0, 0)
    return im


def square_trim(im, margin):
    logo = im.crop(im.getbbox())
    side = int(max(logo.size) * (1 + 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(logo, ((side - logo.width) // 2, (side - logo.height) // 2), logo)
    return canvas


def brighten(im, target_lightness):
    px = im.load()
    opaque = [colorsys.rgb_to_hls(*[v / 255 for v in px[x, y][:3]])[1]
              for y in range(im.height) for x in range(im.width)
              if px[x, y][3] > 200]
    offset = target_lightness - sum(opaque) / len(opaque)
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            nr, ng, nb = colorsys.hls_to_rgb(h, min(1.0, l + offset), s)
            px[x, y] = (round(nr * 255), round(ng * 255), round(nb * 255), a)
    return im


def main():
    target = float(sys.argv[1]) if len(sys.argv) > 1 else 0.76
    margin = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    source = drop_shadow(Image.open(SOURCE).convert("RGBA"))
    icon = brighten(square_trim(source, margin), target)
    icon.save(TARGET)
    print(f"Wrote {TARGET} ({icon.width}x{icon.height}, "
          f"lightness {target}, margin {margin})")


if __name__ == "__main__":
    main()
