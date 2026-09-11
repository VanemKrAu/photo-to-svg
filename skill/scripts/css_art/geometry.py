"""Subpixel contours and even-odd CSS polygons, including holes and islands."""

import cv2
import numpy as np


def number(value, digits=3):
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    # A leading zero is legal to drop in CSS numbers and saves a byte per value.
    if text.startswith("0."):
        text = text[1:]
    elif text.startswith("-0."):
        text = "-" + text[2:]
    return "0" if text in ("", "-0", "-.") else text


def hex_color(rgb):
    return "#" + "".join(f"{int(value):02x}" for value in np.clip(np.rint(rgb), 0, 255))


def bridge_rings(rings):
    """Retrace every bridge exactly; its zero area preserves even-odd topology."""
    points = list(rings[0])
    if len(rings) > 1:
        anchor = rings[0][0]
        points.append(anchor)
        for ring in rings[1:]:
            points.extend(ring)
            points.extend((ring[0], anchor))
    return np.asarray(points)


def polygon_css(points, origin, size, multiple=False, digits=3):
    normalized = (points - origin) / size * 100
    coords = ",".join(f"{number(x, digits)}% {number(y, digits)}%" for x, y in normalized)
    return f"polygon({'evenodd,' if multiple else ''}{coords})"


def smooth_ring(contour, x, y, epsilon):
    points = contour[:, 0, :].astype(np.float32) / 4
    points += np.array([x + .125, y + .125], np.float32)
    if len(points) > 8:
        points = (np.roll(points, 1, axis=0) + 2 * points + np.roll(points, -1, axis=0)) / 4
    return cv2.approxPolyDP(points[:, None, :], epsilon, True)[:, 0, :]


def component_rings(mask, x, y, area, epsilon):
    padded = np.pad(mask, 2)
    expanded = cv2.resize(padded, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    kind, size = (cv2.MORPH_ELLIPSE, 5) if area >= 24 else (cv2.MORPH_CROSS, 3)
    expanded = cv2.dilate(expanded, cv2.getStructuringElement(kind, (size, size)))
    contours, hierarchy = cv2.findContours(expanded, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        return
    for index, contour in enumerate(contours):
        if hierarchy[0, index, 3] != -1:
            continue
        outer = smooth_ring(contour, x - 2, y - 2, epsilon)
        if len(outer) < 3:
            continue
        rings = [outer]
        child = int(hierarchy[0, index, 2])
        while child != -1:
            hole = smooth_ring(contours[child], x - 2, y - 2, epsilon)
            if len(hole) >= 3 and abs(cv2.contourArea(hole)) >= .25:
                rings.append(hole)
            child = int(hierarchy[0, child, 0])
        yield rings


def mask_polygon(mask, epsilon, min_area):
    # Padding makes a solid mask (including a 1x1 input) traceable.
    contours, _ = cv2.findContours(np.pad(mask, 1), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rings = []
    for contour in contours:
        if abs(cv2.contourArea(contour)) < min_area:
            continue
        points = cv2.approxPolyDP(contour, epsilon, True)[:, 0, :].astype(float) - 1
        if len(points) >= 3:
            rings.append(points)
    if not rings:
        return None, 0
    points = bridge_rings(rings)
    return polygon_css(points, np.zeros(2), np.array(mask.shape[::-1]), True, 4), len(points)
