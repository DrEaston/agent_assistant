"""Turn an animated GIF/WebP into frames on one shared canvas, and measure how much the picture jumps.

Every clip is fitted so that Dieter sits in the same place and at the same size: a clip's first frame (or last, if
only its end pose is known yet) is matched to where that pose sits in clips already placed, starting from the
reference clip. Matching uses the outline of what isn't transparent (its box), aligned on the bottom centre, where
his paws are. What's left over after that is real drift, and the scores below report it.
"""

import io
import math

from PIL import Image, ImageChops, ImageSequence, ImageStat

CANVAS = 512
MARGIN = 24          # around the reference clip's first frame
SCORE_SIZE = 128     # frames are compared at this size (fine detail and GIF noise don't count)
ALPHA_EDGE = 40      # alpha above this counts as Dieter
OK, LOOK = 4.0, 8.0  # seam score: under OK is smooth, under LOOK worth a look, above it a visible jump


def decode(data):
    """[(RGBA image, ms)] for every frame; GIF/WebP frames come back already composited by Pillow."""
    picture = Image.open(io.BytesIO(data))
    frames = []
    for frame in ImageSequence.Iterator(picture):
        frames.append((frame.convert("RGBA").copy(), int(frame.info.get("duration") or 100)))
    return frames


def box(image):
    """Where Dieter is in a frame: (left, top, right, bottom), or None if it's empty."""
    return image.getchannel("A").point(lambda a: 255 if a > ALPHA_EDGE else 0).getbbox()


def fit(source_box, target_box):
    """Scale and offset that put source_box onto target_box: same area, same bottom centre."""
    sw, sh = source_box[2] - source_box[0], source_box[3] - source_box[1]
    tw, th = target_box[2] - target_box[0], target_box[3] - target_box[1]
    scale = math.sqrt((tw * th) / max(1, sw * sh))
    dx = (target_box[0] + target_box[2]) / 2 - (source_box[0] + source_box[2]) / 2 * scale
    dy = target_box[3] - source_box[3] * scale
    return scale, dx, dy


def first_fit(source_box):
    """For the reference clip: fill the canvas (less the margin), sitting on the bottom margin."""
    sw, sh = source_box[2] - source_box[0], source_box[3] - source_box[1]
    scale = min((CANVAS - 2 * MARGIN) / max(1, sw), (CANVAS - 2 * MARGIN) / max(1, sh))
    target = (CANVAS / 2 - sw * scale / 2, CANVAS - MARGIN - sh * scale, CANVAS / 2 + sw * scale / 2, CANVAS - MARGIN)
    return fit(source_box, target)


def place(frames, scale, dx, dy):
    """The frames drawn onto the shared canvas."""
    out = []
    for image, ms in frames:
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        scaled = image.resize(size, Image.LANCZOS)
        canvas.paste(scaled, (round(dx), round(dy)), scaled)  # onto an empty canvas, so paste = composite
        out.append((canvas, ms))
    return out


def _small(image):
    small = image.resize((SCORE_SIZE, SCORE_SIZE), Image.BILINEAR)
    alpha = small.getchannel("A")
    black = Image.new("RGBA", small.size, (0, 0, 0, 255))
    return Image.composite(small, black, alpha).convert("RGB"), alpha


def seam(a, b):
    """How much the picture jumps from frame a to frame b: 0 the same, about 10 a clear jump, 100 nothing alike.
    Colour change where either frame has Dieter, plus how much of his outline doesn't overlap."""
    rgb_a, alpha_a = _small(a)
    rgb_b, alpha_b = _small(b)
    either = ImageChops.lighter(alpha_a, alpha_b).point(lambda v: 255 if v > ALPHA_EDGE else 0)
    area = ImageStat.Stat(either).sum[0] / 255
    if not area:
        return 0.0
    colour = sum(ImageStat.Stat(ImageChops.difference(rgb_a, rgb_b), either).mean) / 3 / 255 * 100
    outline = ImageStat.Stat(ImageChops.difference(alpha_a, alpha_b).point(
        lambda v: 255 if v > 128 else 0)).sum[0] / 255 / area * 100
    return round(0.5 * colour + 0.5 * outline, 1)


def anchor(image):
    """Bottom centre of Dieter in a frame (x, y), in canvas pixels."""
    b = box(image)
    return ((b[0] + b[2]) / 2, b[3]) if b else (CANVAS / 2, CANVAS)


def verdict(score):
    return "ok" if score < OK else "look" if score < LOOK else "jump"


def sheet(frames, columns=8):
    """All frames in one WebP, `columns` across, each CANVAS square."""
    rows = math.ceil(len(frames) / columns)
    grid = Image.new("RGBA", (CANVAS * min(columns, len(frames)), CANVAS * rows), (0, 0, 0, 0))
    for n, (image, _) in enumerate(frames):
        grid.paste(image, ((n % columns) * CANVAS, (n // columns) * CANVAS))
    out = io.BytesIO()
    grid.save(out, "WEBP", quality=88, alpha_quality=90, method=4)
    return out.getvalue()
