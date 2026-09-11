"""Build a static document with only inline CSS and HTML contour elements."""

import html
import math

import cv2
import numpy as np

from .geometry import bridge_rings, component_rings, hex_color, mask_polygon, number, polygon_css
from .quality import Rasterizer
from .regions import label_components, quantize


def paint_for_region(reference, mask, x, y, width, height, gradients):
    ys, xs = np.nonzero(mask)
    samples = reference[y + ys, x + xs].astype(float)
    solid = hex_color(samples.mean(axis=0))
    if not gradients or len(xs) < 45 or min(width, height) < 5:
        return solid, False
    if len(xs) > 6000:
        step = math.ceil(len(xs) / 6000)
        xs, ys, samples = xs[::step], ys[::step], samples[::step]
    design = np.column_stack((xs + .5 - width / 2, ys + .5 - height / 2, np.ones(len(xs))))
    coeff, _, _, _ = np.linalg.lstsq(design, samples, rcond=None)
    axes, _, _ = np.linalg.svd(coeff[:2], full_matrices=False)
    direction = axes[:, 0]
    slope = direction @ coeff[:2]
    length = abs(direction[0]) * width + abs(direction[1]) * height
    low, high = np.percentile(samples, (3, 97), axis=0)
    start = np.clip(coeff[2] - slope * length / 2, low, high)
    end = np.clip(coeff[2] + slope * length / 2, low, high)
    if np.max(np.abs(end - start)) < 3:
        return solid, False
    angle = math.degrees(math.atan2(direction[0], -direction[1])) % 360
    return f"linear-gradient({number(angle, 1)}deg,{hex_color(start)},{hex_color(end)})", True


def color_order(labels, palette):
    return sorted(np.unique(labels), key=lambda i: (-float(palette[i] @ np.array([.2126, .7152, .0722])), int(i)))


def same_as_matte(rgb, background):
    return int(np.abs(rgb.astype(np.int16) - background).max()) <= 3


class BudgetExceeded(ValueError):
    """Raised while rendering when the document passes its byte budget."""

    def __init__(self, byte_count):
        super().__init__("HTML exceeds --max-output-mb; reduce --max-width/--colors, raise the limit, or use --fit.")
        self.byte_count = byte_count


class ContourRenderer:
    def __init__(self, reference, background, epsilon, gradients, max_bytes, score=False):
        self.reference = reference
        self.height, self.width = reference.shape[:2]
        self.background = np.array(background)
        self.epsilon = epsilon
        self.gradients = gradients
        self.max_bytes = max_bytes
        self.byte_count = 0
        self.paint_classes = {}
        self.raster = Rasterizer((self.width, self.height), background) if score else None
        self.stats = {"shapes": 0, "gradient_fills": 0, "polygon_vertices": 0, "interior_holes": 0, "underpainting_shapes": 0}

    def account(self, text):
        self.byte_count += len(text.encode("utf-8"))
        if self.byte_count > self.max_bytes:
            raise BudgetExceeded(self.byte_count)
        return text

    def solid_class(self, paint):
        """Share one CSS class between every shape painted the same solid color."""
        if paint not in self.paint_classes:
            self.paint_classes[paint] = f"p{len(self.paint_classes)}"
        return self.paint_classes[paint]

    def shape(self, rings, paint):
        all_points = np.concatenate(rings)
        origin = all_points.min(axis=0)
        size = all_points.max(axis=0) - origin
        if np.any(size <= 0):
            return None
        points = bridge_rings(rings)
        # Two decimals already bound the error to size/10000 px; only vast
        # shapes earn a third decimal. That stays under ~0.1 px, far below
        # the tracing grid and visible antialiasing.
        digits = max(2, math.ceil(math.log10(max(size) / 10)))
        clip = polygon_css(points, origin, size, len(rings) > 1, digits)
        cls = "shape"
        background = ""
        if paint.startswith("#"):
            cls = f"shape {self.solid_class(paint)}"
        else:
            background = f"background:{paint};"
        style = (
            f"left:{number(origin[0] / self.width * 100, 3)}%;top:{number(origin[1] / self.height * 100, 3)}%;"
            f"width:{number(size[0] / self.width * 100, 3)}%;height:{number(size[1] / self.height * 100, 3)}%;"
            f"{background}clip-path:{clip}"
        )
        self.stats["shapes"] += 1
        self.stats["polygon_vertices"] += len(points)
        if self.raster is not None:
            self.raster.fill_rings(points, origin, size, paint)
        return self.account(f'<div class="{cls}" style="{style}"></div>')

    def foreground(self, labels, palette, progress):
        parts = []
        # One crop-based labeling pass replaces per-color full-image labeling.
        ids, colors_by_component, areas, boxes, centers = label_components(labels)
        components_of = {}
        for component in range(1, len(colors_by_component)):
            components_of.setdefault(int(colors_by_component[component]), []).append(component)
        order = color_order(labels, palette)
        for position, color in enumerate(order):
            rgb = palette[color]
            if same_as_matte(rgb, self.background):
                continue
            small_groups = {}
            for component in components_of.get(int(color), ()):
                x, y, width, height = map(int, boxes[component])
                area = int(areas[component])
                mask = (ids[y:y + height, x:x + width] == component).astype(np.uint8)
                paint, gradient = paint_for_region(self.reference, mask, x, y, width, height, self.gradients) if area >= 24 else (hex_color(rgb), False)
                for rings in component_rings(mask, x, y, area, self.epsilon):
                    self.stats["interior_holes"] += len(rings) - 1
                    if area < 24:
                        key = tuple((centers[component] // 160).astype(int))
                        small_groups.setdefault(key, []).extend(rings)
                    else:
                        shape = self.shape(rings, paint)
                        if shape:
                            parts.append(shape)
                            self.stats["gradient_fills"] += int(gradient)
            for rings in small_groups.values():
                shape = self.shape(rings, hex_color(rgb))
                if shape:
                    parts.append(shape)
            if position % 32 == 0:
                progress(f"Trace {position + 1}/{len(order)} colors: {self.stats['shapes']} shapes")
        return "\n".join(parts)

    def underpainting(self):
        if min(self.width, self.height) < 8:
            return ""
        difference = np.abs(self.reference.astype(np.int16) - self.background).max(axis=2)
        silhouette = cv2.medianBlur((difference > 9).astype(np.uint8), 3)
        silhouette = cv2.erode(silhouette, np.ones((5, 5), np.uint8))
        clip, vertices = mask_polygon(silhouette, .5, 8)
        if not clip:
            return ""
        self.stats["polygon_vertices"] += vertices
        scale = min(1, 516 / self.width, 1 / 3)
        small = cv2.resize(self.reference, (max(2, round(self.width * scale)), max(2, round(self.height * scale))), interpolation=cv2.INTER_AREA)
        small = cv2.medianBlur(small, 3)
        labels, palette = quantize(small, 48)
        parts = []
        for color in color_order(labels, palette):
            rgb = palette[color]
            if same_as_matte(rgb, self.background):
                continue
            mask = cv2.dilate((labels == color).astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
            polygon, vertices = mask_polygon(mask, .26, 1)
            if polygon:
                parts.append(self.account(f'<div class="shape" style="inset:0;background:{hex_color(rgb)};clip-path:{polygon}"></div>'))
                if self.raster is not None:
                    full = cv2.resize(mask, (self.width, self.height), interpolation=cv2.INTER_NEAREST)
                    self.raster.fill_mask(full > 0, 0, 0, hex_color(rgb))
                self.stats["polygon_vertices"] += vertices
                self.stats["shapes"] += 1
                self.stats["underpainting_shapes"] += 1
        return self.account(f'<div class="underpainting" aria-hidden="true" style="clip-path:{clip}">') + "\n" + "\n".join(parts) + "\n</div>"


def render_document(reference, labels, palette, original_size, *, background, title,
                    epsilon, gradients, underpainting, max_bytes, progress, score=False):
    renderer = ContourRenderer(reference, background, epsilon, gradients, max_bytes, score)
    foundation = renderer.underpainting() if underpainting else ""
    shapes = renderer.foreground(labels, palette, progress)
    width, height = original_size
    matte = hex_color(background)
    label = html.escape(title, quote=True)
    paints = "".join(f".{name}{{background:{paint}}}" for paint, name in renderer.paint_classes.items())
    document = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; script-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>{label}</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%;background:{matte}}}
.illustration{{position:relative;isolation:isolate;overflow:hidden;width:min(100%,{renderer.width}px);aspect-ratio:{width}/{height};margin:0 auto;background:{matte};contain:layout paint}}
.shape{{position:absolute;pointer-events:none}}
.underpainting{{position:absolute;inset:0;pointer-events:none}}
{paints}
@media print{{@page{{margin:0}}.illustration{{width:100%;print-color-adjust:exact;-webkit-print-color-adjust:exact}}}}
</style>
</head>
<body>
<!-- Offline contour tracing. Every visible mark is an HTML/CSS layer. -->
<main class="illustration" role="img" aria-label="{label}">
{foundation}
{shapes}
</main>
</body>
</html>
'''
    if len(document.encode("utf-8")) > max_bytes:
        raise BudgetExceeded(len(document.encode("utf-8")))
    if renderer.raster is not None:
        detail, thumbnail = renderer.raster.errors(reference)
        renderer.stats["similarity"] = {"mae": detail, "mae_thumbnail": thumbnail}
    return document, renderer.stats
