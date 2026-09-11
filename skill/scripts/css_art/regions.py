"""Image preparation and conservative, adjacency-constrained region merging."""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


def load_reference(path: Path, max_width: int, background: tuple[int, int, int]):
    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) > 1:
            raise ValueError("Animated/multipage inputs are unsupported; export one frame first.")
        image = ImageOps.exif_transpose(image)
        original_size = image.size
        # Preserve alpha until it has been composited onto the explicit CSS matte.
        rgba = image.convert("RGBA")
        matte = Image.new("RGBA", rgba.size, (*background, 255))
        image = Image.alpha_composite(matte, rgba).convert("RGB")
        # A longest-edge bound prevents very tall references from bypassing the limit.
        scale = min(1.0, max_width / image.width, 2 * max_width / max(image.size))
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        reference = np.array(image)
    if min(reference.shape[:2]) >= 3:
        reference = cv2.bilateralFilter(reference, 7, 22, 4)
        reference = cv2.bilateralFilter(reference, 9, 24, 5)
    return reference, original_size


OKLAB_FROM_LINEAR = np.array([
    [0.4122214708, 0.5363325363, 0.0514459929],
    [0.2119034982, 0.6806995451, 0.1073969566],
    [0.0883024619, 0.2817188376, 0.6299787005],
], np.float32)
OKLAB_FROM_LMS_PRIME = np.array([
    [0.2104542553, 0.7936177850, -0.0040720468],
    [1.9779984951, -2.4285922050, 0.4505937099],
    [0.0259040371, 0.7827717662, -0.8086757660],
], np.float32)


def oklab_of(rgb: np.ndarray) -> np.ndarray:
    """Map an (n, 3) sRGB uint8 array to perceptual Oklab floats."""
    linear = rgb.astype(np.float32) / 255
    linear = np.where(linear <= 0.04045, linear / 12.92, ((linear + 0.055) / 1.055) ** 2.4)
    lms = linear @ OKLAB_FROM_LINEAR.T
    return np.cbrt(lms, out=lms) @ OKLAB_FROM_LMS_PRIME.T


def quantize(reference: np.ndarray, colors: int):
    """Median-cut color quantization without dithering.

    Splits happen in Oklab so bucket boundaries follow perceived color
    differences; each palette entry is the sRGB mean of its bucket, and
    pixels keep the bucket they were split into. Bucket bounds are tracked
    incrementally, so a split only gathers one coordinate column.
    """
    height, width = reference.shape[:2]
    flat = reference.reshape(-1, 3)
    coordinates = oklab_of(flat)
    labels = np.zeros(flat.shape[0], np.int32)
    lo = coordinates.min(axis=0)
    hi = coordinates.max(axis=0)
    buckets = [[np.arange(flat.shape[0]), lo, hi]]
    while True:
        best = None
        best_size = 1
        for bucket in buckets:
            members, low, high = bucket
            if members.size > best_size and bool(((high - low) > 0).any()):
                best, best_size = bucket, members.size
        if best is None or len(buckets) >= colors:
            break
        members, low, high = best
        axis = int(np.argmax(high - low))
        column = coordinates[members, axis]
        midpoint = np.float32((float(low[axis]) + float(high[axis])) / 2)
        if not low[axis] < midpoint < high[axis]:
            # The interval is exhausted at float32 precision: no representable
            # cut remains, so retire the bucket instead of spinning.
            high[axis] = low[axis]
            continue
        upper = members[column > midpoint]
        if upper.size == 0:
            # Bounds overshoot the real pixels; tighten and retry this bucket.
            high[axis] = midpoint
            continue
        lower = members[column <= midpoint]
        if lower.size == 0:
            low[axis] = midpoint
            continue
        best[0] = lower
        best[2] = high.copy()
        best[2][axis] = midpoint
        buckets.append([upper, low.copy(), high])
        buckets[-1][1][axis] = midpoint
        labels[upper] = len(buckets) - 1
    palette = np.zeros((len(buckets), 3), np.float32)
    for index, bucket in enumerate(buckets):
        palette[index] = flat[bucket[0]].mean(axis=0)
    return labels.reshape(height, width).astype(np.uint8), np.clip(np.rint(palette), 0, 255).astype(np.uint8)


def label_components(labels):
    """Label 8-connected same-color components in one pass over the image.

    Components are numbered by ascending color value, then cv2's crop-scan
    order — deterministic for a given dependency environment, like the rest
    of the pipeline. Only the per-color bounding-box crops make the pass
    cheaper than one full-image mask per palette color. Returns the int32
    component image (0 outside components) plus parallel color, area,
    bounding-box (x, y, w, h) and centroid arrays indexed by component id,
    each with a placeholder at index 0.
    """
    width = labels.shape[1]
    ids = np.zeros(labels.shape, np.int32)
    flat = labels.ravel()
    order = np.argsort(flat, kind="stable")
    grouped = flat[order]
    starts = np.flatnonzero(np.concatenate(([True], grouped[1:] != grouped[:-1])))
    colors = grouped[starts]
    ends = np.concatenate((starts[1:], [grouped.size]))
    component_colors, component_areas = [0], [0]
    component_boxes, component_centers = [(0, 0, 0, 0)], [(0.0, 0.0)]
    offset = 0
    for color, start, end in zip(colors, starts, ends):
        span = order[start:end]
        ys = span // width
        xs = span - ys * width
        y0, y1 = int(ys.min()), int(ys.max())
        x0, x1 = int(xs.min()), int(xs.max())
        crop = (labels[y0:y1 + 1, x0:x1 + 1] == color).astype(np.uint8)
        count, local, stats, centers = cv2.connectedComponentsWithStats(crop, connectivity=8)
        if count == 1:
            continue
        region = ids[y0:y1 + 1, x0:x1 + 1]
        mask = local > 0
        region[mask] = local[mask] + offset
        component_colors.extend([int(color)] * (count - 1))
        component_areas.extend(stats[1:, cv2.CC_STAT_AREA].tolist())
        component_boxes.extend(zip(
            stats[1:, cv2.CC_STAT_LEFT] + x0,
            stats[1:, cv2.CC_STAT_TOP] + y0,
            stats[1:, cv2.CC_STAT_WIDTH],
            stats[1:, cv2.CC_STAT_HEIGHT],
        ))
        component_centers.extend(zip(
            centers[1:, 0] + x0, centers[1:, 1] + y0,
        ))
        offset += count - 1
    return (ids, np.asarray(component_colors), np.asarray(component_areas),
            np.asarray(component_boxes, dtype=np.int32),
            np.asarray(component_centers, dtype=np.float64))


def merge_regions(labels, palette, passes=4, progress=lambda _: None):
    """Merge tiny components into adjacent, larger, similar-colored components.

    The original palette label bounds cumulative drift across passes. Small dark
    eye/hair lines cannot be replaced by a spatially close but unrelated fill.
    """
    labels = labels.copy()
    original = labels.copy()
    for iteration in range(passes):
        ids, component_colors, component_areas, _, _ = label_components(labels)
        component_colors = np.asarray(component_colors)
        component_areas = np.asarray(component_areas)
        proposals = []
        for dy, dx in [(0, 1), (1, 0), (1, 1), (1, -1)]:
            ya, yb = (slice(0, -1), slice(1, None)) if dy else (slice(None), slice(None))
            if dx == 1:
                xa, xb = slice(0, -1), slice(1, None)
            elif dx == -1:
                xa, xb = slice(1, None), slice(0, -1)
            else:
                xa, xb = slice(None), slice(None)
            one, two = ids[ya, xa].ravel(), ids[yb, xb].ravel()
            boundary = one != two
            one, two = one[boundary], two[boundary]
            for src, dst in [(one, two), (two, one)]:
                candidate = (component_areas[src] < 16) & (
                    (component_areas[dst] > component_areas[src])
                    | ((component_areas[dst] == component_areas[src]) & (dst > src))
                )
                src, dst = src[candidate], dst[candidate]
                if not len(src):
                    continue
                diff = palette[component_colors[src]].astype(np.float32) - palette[
                    component_colors[dst]
                ].astype(np.float32)
                accepted = np.max(np.abs(diff), axis=1) <= 18
                src, dst, diff = src[accepted], dst[accepted], diff[accepted]
                if len(src):
                    proposals.append(np.column_stack((src, dst, np.sum(diff * diff, axis=1))))
        if not proposals:
            break
        candidates = np.concatenate(proposals)
        # Explicit tie-breaking keeps the result repeatable in one dependency environment.
        ordered = candidates[np.lexsort((candidates[:, 1], candidates[:, 2], candidates[:, 0]))]
        _, first = np.unique(ordered[:, 0], return_index=True)
        best = ordered[first]
        new_colors = component_colors.copy()
        new_colors[best[:, 0].astype(int)] = component_colors[best[:, 1].astype(int)]
        updated = new_colors[ids].astype(np.uint8)
        drift = np.abs(palette[updated].astype(np.int16) - palette[original].astype(np.int16)).max(axis=2)
        updated[drift > 23] = labels[drift > 23]
        changes = int(np.count_nonzero(updated != labels))
        labels = updated
        progress(f"Merge {iteration + 1}/{passes}: {changes} pixels")
        if not changes:
            break
    return labels
