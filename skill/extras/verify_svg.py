#!/usr/bin/env python3
"""把导出的形状重新栅格化，量化还原度，并生成对比图。

支持纯色与线性渐变两种填充（与 build_svg_art.py 产出的形状格式一致）。
参数需与生成时保持一致，否则比的是另一份结果。
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _find_scripts():
    import os
    here = Path(__file__).resolve()
    candidates = []
    if os.environ.get('IMAGE_TO_CSS_ART_DIR'):
        candidates.append(Path(os.environ['IMAGE_TO_CSS_ART_DIR']) / 'scripts')
    for parent in here.parents:
        candidates.append(parent / 'scripts')
    candidates += [Path.home() / '.agents/skills/image-to-css-art/scripts',
                   Path('/skills/image-to-css-art/scripts')]
    for candidate in candidates:
        if (candidate / 'css_art' / 'regions.py').exists():
            return candidate
    raise SystemExit('找不到 image-to-css-art 的 scripts/css_art 模块；'
                     '请把 extras/ 放在 skill 目录内，或用 IMAGE_TO_CSS_ART_DIR 指定')


SKILL_SCRIPTS = _find_scripts()
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from css_art.geometry import bridge_rings          # noqa: E402
import build_svg_art as B                          # noqa: E402


def _rgb(hex_color):
    return np.array([int(hex_color[1:3], 16), int(hex_color[3:5], 16),
                     int(hex_color[5:7], 16)], np.float32)


def fill_solid(composite, rings, fill):
    pts = bridge_rings(rings)
    x0 = max(0, int(np.floor(pts[:, 0].min())))
    y0 = max(0, int(np.floor(pts[:, 1].min())))
    x1 = min(composite.shape[1], int(np.ceil(pts[:, 0].max())))
    y1 = min(composite.shape[0], int(np.ceil(pts[:, 1].max())))
    if x1 <= x0 or y1 <= y0:
        return
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(mask, [np.round(pts - (x0, y0)).astype(np.int32)], 1)
    composite[y0:y1, x0:x1][mask > 0] = _rgb(fill)


def fill_gradient(composite, rings, grad):
    """按 (x1,y1,x2,y2,起色,终色) 在形状内填线性渐变。"""
    gx1, gy1, gx2, gy2, c1, c2 = grad
    dx, dy = gx2 - gx1, gy2 - gy1
    denom = dx * dx + dy * dy
    if denom <= 0:
        return
    pts = bridge_rings(rings)
    x0 = max(0, int(np.floor(pts[:, 0].min())))
    y0 = max(0, int(np.floor(pts[:, 1].min())))
    x1 = min(composite.shape[1], int(np.ceil(pts[:, 0].max())))
    y1 = min(composite.shape[0], int(np.ceil(pts[:, 1].max())))
    if x1 <= x0 or y1 <= y0:
        return
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(mask, [np.round(pts - (x0, y0)).astype(np.int32)], 1)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    t = np.clip(((xs + .5 - gx1) * dx + (ys + .5 - gy1) * dy) / denom, 0, 1)
    layer = _rgb(c1) + (_rgb(c2) - _rgb(c1)) * t[..., None]
    region = composite[y0:y1, x0:x1]
    sel = mask > 0
    region[sel] = layer[sel]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input', type=Path)
    ap.add_argument('--out', default='/tmp/verify')
    # 底板默认纸白：必须与生成时一致，否则比对结果会失真
    ap.add_argument('--bg', default='#f0f0f0')
    ap.add_argument('--dark-cut', type=int, default=14)
    ap.add_argument('--max-width', type=int, default=760)
    ap.add_argument('--colors', type=int, default=96)
    ap.add_argument('--epsilon', type=float, default=0.32)
    ap.add_argument('--min-area', type=int, default=6)
    ap.add_argument('--passes', type=int, default=3)
    ap.add_argument('--order', default='sketch')
    ap.add_argument('--sketch-ratio', type=float, default=0.02)
    ap.add_argument('--no-gradients', action='store_true')
    ap.add_argument('--gradient-min-area', type=int, default=24)
    args = ap.parse_args()

    background = tuple(int(args.bg[i:i + 2], 16) for i in (1, 3, 5))
    reference, _ = B.load_reference(args.input, args.max_width, background, args.dark_cut)
    H, W = reference.shape[:2]

    shapes = B.trace(reference, args.colors, args.passes, args.epsilon,
                     args.min_area, background, args.order, args.sketch_ratio,
                     not args.no_gradients, args.gradient_min_area,
                     progress=lambda *_: None)

    graded = sum(1 for s in shapes if len(s) > 5 and s[5])
    print(f'shapes: {len(shapes)}  （其中渐变 {graded} 处）')

    composite = np.empty((H, W, 3), np.float32)
    composite[:] = np.array(background, np.float32)
    for shape in shapes:
        rings, fill, grad = shape[0], shape[1], (shape[5] if len(shape) > 5 else None)
        if grad:
            fill_gradient(composite, rings, grad)
        else:
            fill_solid(composite, rings, fill)

    ref = reference.astype(np.float32)
    mae_all = float(np.abs(composite - ref).mean())
    scale = 64 / max(H, W)
    tw, th = max(1, round(W * scale)), max(1, round(H * scale))
    mae_thumb = float(np.abs(
        cv2.resize(composite.astype(np.uint8), (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)
        - cv2.resize(reference, (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)).mean())
    print(f'MAE all      : {mae_all:.2f}')
    print(f'MAE thumbnail: {mae_thumb:.2f}')

    diff_bg = np.abs(reference.astype(np.int16) - np.array(background, np.int16)).max(axis=2)
    fg = diff_bg > 20
    if fg.any():
        print(f'MAE foreground: {float(np.abs(composite[fg] - ref[fg]).mean()):.2f}   '
              f'foreground share {fg.mean()*100:.1f}%')

    areas = np.array([s[2] for s in shapes])
    print(f'area median {np.median(areas):.0f}px  max {areas.max()}px  '
          f'small(<20px) {int((areas < 20).sum())} ({(areas < 20).mean()*100:.0f}%)')

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(composite.astype(np.uint8)).save(f'{args.out}_redraw.png')
    panel = np.concatenate([reference, composite.astype(np.uint8),
                            np.clip(np.abs(composite - ref) * 3, 0, 255).astype(np.uint8)], axis=1)
    Image.fromarray(panel).save(f'{args.out}_compare.png')

    ref_lum = reference @ [0.2126, 0.7152, 0.0722]
    out_lum = composite @ [0.2126, 0.7152, 0.0722]
    print(f'luma ref {ref_lum.mean():.1f} -> redraw {out_lum.mean():.1f}')
    if fg.any():
        print(f'luma fg  ref {ref_lum[fg].mean():.1f} -> redraw {out_lum[fg].mean():.1f}')


if __name__ == '__main__':
    main()
