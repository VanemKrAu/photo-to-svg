"""Offline similarity estimate: rasterize the emitted shape stack and compare
it with the reference. This measures the approximation itself, not how a
browser antialiases it, and is only computed when the caller asks for it."""

import math
import re

import cv2
import numpy as np

GRADIENT = re.compile(r"linear-gradient\(([-\d.]+)deg,(#[0-9a-f]{6}),(#[0-9a-f]{6})\)")


def rgb_of(hex_color):
    return np.array([int(hex_color[index:index + 2], 16) for index in (1, 3, 5)], np.float32)


def paint_layer(paint, width, height):
    """Render one CSS paint (solid or two-stop linear gradient) to HxWx3."""
    layer = np.empty((height, width, 3), np.float32)
    if paint.startswith("#"):
        layer[:] = rgb_of(paint)
        return layer
    match = GRADIENT.fullmatch(paint)
    if match is None:
        return None
    angle, start, end = float(match.group(1)), rgb_of(match.group(2)), rgb_of(match.group(3))
    radians = math.radians(angle)
    # CSS angles run clockwise from "up": the gradient direction in image
    # coordinates (x right, y down) is (sin, -cos).
    direction = np.array([math.sin(radians), -math.cos(radians)])
    length = abs(direction[0]) * width + abs(direction[1]) * height
    ys, xs = np.mgrid[0:height, 0:width]
    position = (direction[0] * (xs + .5 - width / 2) + direction[1] * (ys + .5 - height / 2)) / length
    layer[:] = start + (end - start) * np.clip(position + .5, 0, 1)[..., None]
    return layer


class Rasterizer:
    """Composites emitted shapes in paint order onto the matte background."""

    def __init__(self, shape_wh, background):
        self.composite = np.empty((shape_wh[1], shape_wh[0], 3), np.float32)
        self.composite[:] = np.array(background, np.float32)

    def fill_mask(self, mask, x, y, paint):
        """Paint a solid color where a canvas-space boolean mask is set."""
        height, width = mask.shape
        layer = paint_layer(paint, width, height)
        if layer is not None:
            region = self.composite[y:y + height, x:x + width]
            region[mask] = layer[mask]

    def fill_rings(self, points, origin, size, paint):
        """Fill even-odd rings (canvas-space float points) with a paint."""
        x0 = max(0, int(math.floor(origin[0])))
        y0 = max(0, int(math.floor(origin[1])))
        x1 = min(self.composite.shape[1], int(math.ceil(origin[0] + size[0])))
        y1 = min(self.composite.shape[0], int(math.ceil(origin[1] + size[1])))
        if x1 <= x0 or y1 <= y0:
            return
        local = np.asarray(points) - (x0, y0)
        mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.fillPoly(mask, [np.round(local).astype(np.int32)], 1)
        layer = paint_layer(paint, x1 - x0, y1 - y0)
        if layer is not None:
            region = self.composite[y0:y1, x0:x1]
            region[mask > 0] = layer[mask > 0]

    def errors(self, reference):
        """Mean absolute error at full detail and on a 64 px thumbnail."""
        detail = float(np.abs(self.composite - reference.astype(np.float32)).mean())
        scale = 64 / max(reference.shape[:2])
        # A narrow side can round to zero, and cv2.resize rejects an empty
        # dsize, so keep the thumbnail at least one pixel per side.
        width = max(1, round(reference.shape[1] * scale))
        height = max(1, round(reference.shape[0] * scale))
        small_ref = cv2.resize(reference, (width, height), interpolation=cv2.INTER_AREA)
        small_out = cv2.resize(self.composite, (width, height), interpolation=cv2.INTER_AREA)
        small = float(np.abs(small_out - small_ref.astype(np.float32)).mean())
        return round(detail, 2), round(small, 2)
