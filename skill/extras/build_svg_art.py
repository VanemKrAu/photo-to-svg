#!/usr/bin/env python3
"""把参考图描摹成 SVG，并生成可回放每一笔绘制过程的 HTML。

复用 image-to-css-art 的算法管线（Oklab 量化 → 邻接合并 → 亚像素轮廓 → 偶奇多边形），
但把输出从 CSS clip-path 换成真正的 SVG <path>，并额外产出逐笔动画页。

用法:
    python3 build_svg_art.py <参考图> [--out-prefix 前缀] [参数...]
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

def _find_scripts():
    """定位 css_art 模块，顺序：环境变量 → 逐级上溯 → 常见安装位置。

    上溯这条让脚本放到 skill 目录内的任何层级都能用，不依赖本机绝对路径。
    """
    here = Path(__file__).resolve()
    candidates = []
    if os.environ.get('IMAGE_TO_CSS_ART_DIR'):
        candidates.append(Path(os.environ['IMAGE_TO_CSS_ART_DIR']) / 'scripts')
    for parent in here.parents:                      # extras/ → skill/ → ...
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
from css_art.geometry import component_rings, hex_color  # noqa: E402
from css_art.regions import label_components, merge_regions, quantize  # noqa: E402


# ---------------------------------------------------------------- 图像准备

def load_reference(path, max_width, background, dark_cut):
    """EXIF 转正 → 合成到底色 → 限宽 → 降噪 → 暗部压平。

    暗部压平是本脚本相对原工具的额外一步：夜景照片的纯黑背景里全是 JPEG
    压缩噪点，不平掉会在背景里长出成百上千个毫无意义的碎片形状。
    """
    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) > 1:
            raise ValueError("动图/多页图不支持，请先导出单帧")
        image = ImageOps.exif_transpose(image).convert("RGBA")
        matte = Image.new("RGBA", image.size, (*background, 255))
        image = Image.alpha_composite(matte, image).convert("RGB")
        original = image.size
        scale = min(1.0, max_width / image.width, 2 * max_width / max(image.size))
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        reference = np.array(image)

    if min(reference.shape[:2]) >= 3:
        reference = cv2.bilateralFilter(reference, 7, 22, 4)
        reference = cv2.bilateralFilter(reference, 9, 24, 5)

    if dark_cut > 0:
        diff = np.abs(reference.astype(np.int16) - np.array(background, np.int16)).max(axis=2)
        reference[diff <= dark_cut] = background

    return reference, original


# ---------------------------------------------------------------- 描摹

def _perimeter(rings):
    """多边形周长（像素）。用于判断一个形状是不是细长的线。"""
    total = 0.0
    for ring in rings:
        closed = np.vstack([ring, ring[:1]])
        delta = np.diff(closed, axis=0)
        total += float(np.hypot(delta[:, 0], delta[:, 1]).sum())
    return total


def _lum_of(fill):
    return (int(fill[1:3], 16) * 0.2126 + int(fill[3:5], 16) * 0.7152
            + int(fill[5:7], 16) * 0.0722)


def fit_gradient(reference, mask, x, y, width, height, min_area):
    """在区域内部拟合线性渐变，返回 (x1,y1,x2,y2,起色,终色) 或 None。

    做法与原版工具一致：对区域内像素做最小二乘平面拟合，用 SVD 取主方向，
    端点颜色按 3/97 分位裁剪，免得离群点把渐变拉爆。
    返回的是画布绝对坐标（SVG 里用 gradientUnits="userSpaceOnUse"），
    这样渐变方向不会被形状自身的长宽比拉伸变形。
    """
    if min(width, height) < 5:
        return None
    ys, xs = np.nonzero(mask)
    if len(xs) < 12 or len(xs) < min_area:
        return None                      # 样本太少时拟合不可靠，退回纯色
    samples = reference[y + ys, x + xs].astype(float)
    if len(xs) > 6000:                       # 采样上限，免得大区域拟合太慢
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
    if float(np.max(np.abs(end - start))) < 3:
        return None                          # 渐变幅度太小，纯色更划算
    cx, cy = x + width / 2, y + height / 2
    hx, hy = direction[0] * length / 2, direction[1] * length / 2
    return (cx - hx, cy - hy, cx + hx, cy + hy, hex_color(start), hex_color(end))


def order_shapes(shapes, order, sketch_ratio):
    """决定画笔顺序。元素是 (rings, fill, area, perimeter, luma)。

    人画画不是按亮度来的，而是：**先起线稿定轮廓 → 再铺大色块 → 最后抠细节**。

    - `luma`   亮→暗（原版工具继承下来的顺序，只为兼容保留）
    - `area`   大到小，纯粹铺色块
    - `sketch` 线稿优先，随后大到小（默认，最接近人的画法）
    """
    if order == "luma":
        return sorted(shapes, key=lambda s: (-s[4], s[2]))
    if order == "area":
        return sorted(shapes, key=lambda s: -s[2])

    total_px = sum(s[2] for s in shapes) or 1
    thin_cap = max(24.0, total_px * 0.0002)      # 线稿得是细碎的小形状，大区域不算
    lines = []
    for index, shape in enumerate(shapes):
        area, perimeter = shape[2], shape[3]
        if area > thin_cap:
            continue
        # 圆 ≈ 1，正方形 ≈ 1.13，10:1 长条 ≈ 2.2，100:1 细线 ≈ 6.4
        if perimeter / (2 * math.sqrt(math.pi * max(area, 1))) >= 2.0:
            lines.append(index)
    lines.sort(key=lambda i: -shapes[i][3])      # 长线先落笔，像起稿时先拉主轮廓
    budget = max(1, int(len(shapes) * sketch_ratio))
    chosen = set(lines[:budget])
    head = [shapes[i] for i in lines[:budget]]
    tail = sorted((s for i, s in enumerate(shapes) if i not in chosen), key=lambda s: -s[2])
    return head + tail


def trace(reference, colors, passes, epsilon, min_area, background,
          order="sketch", sketch_ratio=0.02, use_gradients=True, grad_min_area=24,
          progress=print):
    """返回 [(rings, fill_hex, area, perimeter, luma, 渐变), ...]，顺序由 order 决定。

    渐变项为 None（纯色）或 (x1,y1,x2,y2,起色,终色)（画布绝对坐标）。
    """
    labels, palette = quantize(reference, colors)
    labels = merge_regions(labels, palette, passes, progress)

    ids, comp_colors, areas, boxes, _ = label_components(labels)
    groups = {}
    for component in range(1, len(comp_colors)):
        groups.setdefault(int(comp_colors[component]), []).append(component)

    luminance = palette.astype(np.float64) @ np.array([0.2126, 0.7152, 0.0722])
    color_order = sorted(np.unique(labels), key=lambda i: (-float(luminance[i]), int(i)))

    # 这里原本有一道省笔优化：与底板色差 ≤3 的调色板色「整簇跳过不画」。
    # 2026-09-14 用户要求取消 —— 每个地方都必须实打实画出来，底板只当纸用。
    # 原因：跳过的块露出的是底板色，而该处的真实颜色只是「接近」底板
    # （填充色取的是区域均值，不是调色板色本身），浅色图上会看出一块块没画到的方斑。
    # background 参数仍保留在签名里：底板 rect 由 write_svg 写，调用方接口不变。
    raw = []
    for position, color in enumerate(color_order):
        for component in groups.get(int(color), ()):
            x, y, width, height = map(int, boxes[component])
            area = int(areas[component])
            if area < min_area:
                continue
            mask = (ids[y:y + height, x:x + width] == component).astype(np.uint8)
            ys, xs = np.nonzero(mask)
            fill = hex_color(reference[y + ys, x + xs].mean(axis=0))
            grad = (fit_gradient(reference, mask, x, y, width, height, grad_min_area)
                    if use_gradients and area >= grad_min_area else None)
            for rings in component_rings(mask, x, y, area, epsilon):
                raw.append((rings, fill, area, _perimeter(rings), _lum_of(fill), grad))
        if position and position % 48 == 0:
            progress(f"  描摹 {position + 1}/{len(color_order)} 色：{len(raw)} 个形状")

    return order_shapes(raw, order, sketch_ratio)


def rings_to_path(rings):
    """把一组环（外轮廓 + 孔洞）写成单个 <path d>，多子路径靠 evenodd 挖洞。"""
    return "".join(
        "M" + " ".join(f"{p[0]:.1f} {p[1]:.1f}" for p in ring) + "Z" for ring in rings
    )


# ---------------------------------------------------------------- 输出：纯 SVG

def gradient_def(gid, grad):
    """一个渐变的 <linearGradient> 定义。用 userSpaceOnUse 绝对坐标，
    免得被形状自身的长宽比拉伸成错误的倾斜方向。"""
    x1, y1, x2, y2, c1, c2 = grad
    return (f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}">'
            f'<stop offset="0" stop-color="{c1}"/>'
            f'<stop offset="1" stop-color="{c2}"/></linearGradient>')


def write_svg(shapes, width, height, background, target):
    matte = hex_color(np.array(background))
    defs, body = [], []
    for index, shape in enumerate(shapes):
        rings, fill = shape[0], shape[1]
        grad = shape[5] if len(shape) > 5 else None
        path = rings_to_path(rings)
        if grad:
            gid = f"g{index}"
            defs.append(gradient_def(gid, grad))
            body.append(f'<path fill="url(#{gid})" fill-rule="evenodd" d="{path}"/>')
        else:
            body.append(f'<path fill="{fill}" fill-rule="evenodd" d="{path}"/>')
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" shape-rendering="geometricPrecision">',
        f'<title>纯 SVG 描摹插画 · {len(shapes)} 笔 · {len(defs)} 处渐变</title>',
    ]
    if defs:
        lines.append('<defs>' + "".join(defs) + '</defs>')
    lines.append(f'<rect width="{width}" height="{height}" fill="{matte}"/>')
    lines.extend(body)
    lines.append('</svg>')
    target.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- 输出：逐笔动画 HTML

PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__ · 逐笔绘制回放</title>
<style>
  :root{
    --bg:#07070a; --panel:#131320; --line:#282842;
    --gold:#f0b429; --text:#e8e8f0; --dim:#8b8ba8;
    --step:__STEP__s; --dur:__DUR__s; --seek:0s; --play:running;
  }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    overscroll-behavior:none}
  body{display:flex;flex-direction:column;min-height:100vh}

  header{padding:14px 16px 10px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
  header h1{margin:0;font-size:15px;font-weight:600;letter-spacing:.02em}
  header .meta{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}

  #stage{flex:1;display:flex;align-items:center;justify-content:center;padding:0 12px;min-height:0}
  .frame{position:relative;max-height:100%;max-width:100%;aspect-ratio:__W__/__H__;
    box-shadow:0 18px 60px rgba(0,0,0,.75), 0 0 0 1px var(--line);border-radius:6px;overflow:hidden}
  svg{display:block;width:100%;height:100%}

  /* 每一笔：先沿轮廓描线，再填充 */
  #art path{
    stroke-width:1.8;
    stroke-linejoin:round;
    stroke-linecap:round;
    stroke-dasharray:1;
    stroke-dashoffset:1;
    fill-opacity:0;
    animation:draw var(--dur) linear both;
    animation-delay:calc(var(--i) * var(--step) - var(--seek));
    animation-play-state:var(--play);
  }
  @keyframes draw{
    0%  {stroke-dashoffset:1; fill-opacity:0}
    62% {stroke-dashoffset:0; fill-opacity:0}
    100%{stroke-dashoffset:0; fill-opacity:1}
  }
  /* 原图对照层：滑动对比，左半原图 / 右半产物，原图 100% 不透明显示 */
  #ghost{position:absolute;inset:0;background-size:100% 100%;opacity:0;
    transition:opacity .2s;pointer-events:none;
    clip-path:inset(0 calc(100% - var(--wipe,50%)) 0 0)}
  body.ghost #ghost{opacity:1}
  #wipe{position:absolute;top:0;bottom:0;left:var(--wipe,50%);width:2px;margin-left:-1px;
    background:var(--gold);opacity:0;transition:opacity .2s;pointer-events:none;
    box-shadow:0 0 10px rgba(0,0,0,.9);z-index:3}
  body.ghost #wipe{opacity:1}
  #wipe::after{content:'';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
    width:28px;height:28px;border-radius:50%;background:var(--gold);
    box-shadow:0 2px 10px rgba(0,0,0,.7);
    background-image:linear-gradient(90deg,#1a1200 0 38%,transparent 38% 62%,#1a1200 62% 100%);
    background-size:11px 12px;background-position:center;background-repeat:no-repeat}
  body.ghost .frame{cursor:ew-resize}

  footer{padding:10px 16px 18px;display:flex;flex-direction:column;gap:10px}
  .bar{height:6px;border-radius:3px;background:#20203a;position:relative;cursor:pointer;
    touch-action:none;overflow:hidden}
  .bar i{position:absolute;inset:0 auto 0 0;width:0;background:linear-gradient(90deg,#8a6d1f,var(--gold));
    border-radius:3px}
  .bar b{position:absolute;top:-3px;width:2px;height:12px;background:#fff;border-radius:1px;
    opacity:0;transition:opacity .15s}
  .bar:hover b{opacity:.7}

  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  button{font:inherit;font-size:13px;color:var(--text);background:var(--panel);
    border:1px solid var(--line);border-radius:8px;padding:7px 13px;cursor:pointer;
    transition:.15s;white-space:nowrap}
  button:hover{border-color:#3d3d63;background:#1b1b2c}
  button:active{transform:translateY(1px)}
  button.on{background:var(--gold);border-color:var(--gold);color:#1a1200;font-weight:600}
  .spacer{flex:1}
  .count{font-variant-numeric:tabular-nums;color:var(--dim);font-size:12.5px}
  .count b{color:var(--gold);font-weight:600}
  .hint{color:#5c5c78;font-size:11.5px;line-height:1.5}
  @media (max-width:520px){ header h1{font-size:14px} button{padding:7px 10px} }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="meta">__N__ 笔 · __SIZE__ · __KBSIZE__</span>
</header>

<div id="stage">
  <div class="frame">
    <div id="ghost"></div>
    <div id="wipe" aria-hidden="true"></div>
    <svg viewBox="0 0 __W__ __H__" preserveAspectRatio="xMidYMid meet" aria-label="__TITLE__">
      <defs>__DEFS__</defs>
      <g id="art">__PATHS__</g>
    </svg>
  </div>
</div>

<footer>
  <div class="bar" id="bar"><i id="fill"></i><b id="knob"></b></div>
  <div class="row">
    <button id="btnPlay" class="on">⏸ 暂停</button>
    <button id="btnReplay">↺ 重播</button>
    <span class="spacer"></span>
    <button id="btnGhost">◧ 对照原图</button>
  </div>
  <div class="row">
    <span class="count">第 <b id="n">0</b> / __N__ 笔</span>
    <span class="spacer"></span>
    <button data-speed="4">4×</button>
    <button data-speed="2">2×</button>
    <button data-speed="1" class="on">1×</button>
    <button data-speed="0.5">0.5×</button>
  </div>
  <div class="hint">进度条可拖动定位。每一笔先沿轮廓描线，再落色填充。<br>
    开启「对照原图」后，<b>左右拖动分界线</b>：左边是原图，右边是产物。</div>
</footer>

<script>
(function(){
  let art      = document.getElementById('art');
  let N        = art.children.length;
  const root   = document.documentElement;
  const bar    = document.getElementById('bar');
  const fill   = document.getElementById('fill');
  const knob   = document.getElementById('knob');
  const nOut   = document.getElementById('n');
  const btnPlay= document.getElementById('btnPlay');
  const BASE   = __BASE__;          // 1× 下每笔间隔（秒）
  const DUR    = __DUR__;           // 单笔动画时长（秒）

  let speed = 1, playing = true, seek = 0, t0 = performance.now();

  // 总时长：最后一笔的延迟 + 单笔时长
  const total = () => (N - 1) * BASE + DUR;

  function applyStep(){
    root.style.setProperty('--step', (BASE / speed) + 's');
  }
  function setPlay(state){
    playing = state;
    root.style.setProperty('--play', state ? 'running' : 'paused');
    btnPlay.textContent = state ? '⏸ 暂停' : '▶ 播放';
    btnPlay.classList.toggle('on', state);
    if (state) t0 = performance.now() - seek * 1000;
  }
  function setSeek(seconds){
    seek = Math.max(0, Math.min(total(), seconds));
    root.style.setProperty('--seek', seek + 's');
    paint();
  }
  function paint(){
    const p = total() > 0 ? seek / total() : 0;
    fill.style.width = (p * 100) + '%';
    knob.style.left  = (p * 100) + '%';
    const drawn = Math.max(0, Math.min(N, Math.ceil(seek / (BASE / speed))));
    nOut.textContent = drawn;
  }

  // 时钟：播放时按 rAF 推进进度条
  function tick(){
    if (playing){
      seek = Math.min(total(), (performance.now() - t0) / 1000);
      paint();
      if (seek >= total()) setPlay(false);             // 画完停在结束帧
    }
    requestAnimationFrame(tick);
  }

  function replay(){
    const fresh = art.cloneNode(true);                // 重建 DOM，动画干净地回到第 0 笔
    art.replaceWith(fresh);
    art = fresh;
    N = art.children.length;
    seek = 0; applyStep(); setSeek(0); setPlay(true);
  }
  btnPlay.onclick = () => {
    if (!playing && seek >= total() - 0.02) replay();  // 停在结尾时点播放 = 重播
    else setPlay(!playing);
  };
  document.getElementById('btnReplay').onclick = replay;
  document.getElementById('btnGhost').onclick = (e) => {
    document.body.classList.toggle('ghost');
    e.target.classList.toggle('on');
  };

  // 滑动对比：拖动分界线，左半看原图、右半看产物
  const frame = document.querySelector('.frame');
  let wipePct = 50, wiping = false;
  function setWipe(clientX){
    const r = frame.getBoundingClientRect();
    if (!r.width) return;
    wipePct = Math.max(0, Math.min(100, (clientX - r.left) / r.width * 100));
    frame.style.setProperty('--wipe', wipePct.toFixed(2) + '%');
  }
  function wipeStart(e){
    if (!document.body.classList.contains('ghost')) return;   // 只在对照模式生效
    wiping = true;
    setWipe(e.touches ? e.touches[0].clientX : e.clientX);
    e.preventDefault();
  }
  function wipeMove(e){
    if (!wiping) return;
    setWipe(e.touches ? e.touches[0].clientX : e.clientX);
    e.preventDefault();
  }
  frame.addEventListener('mousedown', wipeStart);
  window.addEventListener('mousemove', wipeMove);
  window.addEventListener('mouseup', () => { wiping = false; });
  frame.addEventListener('touchstart', wipeStart, {passive:false});
  window.addEventListener('touchmove', wipeMove, {passive:false});
  window.addEventListener('touchend', () => { wiping = false; });
  document.querySelectorAll('[data-speed]').forEach(b => {
    b.onclick = () => {
      speed = parseFloat(b.dataset.speed);
      document.querySelectorAll('[data-speed]').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      t0 = performance.now() - seek * 1000;
      applyStep(); paint();
    };
  });

  // 进度条拖动定位
  let dragging = false;
  const posToTime = (clientX) => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * total();
  };
  const start = (e) => { dragging = true; setPlay(false); setSeek(posToTime(e.touches ? e.touches[0].clientX : e.clientX)); };
  const move  = (e) => { if (dragging) { setSeek(posToTime(e.touches ? e.touches[0].clientX : e.clientX)); e.preventDefault(); } };
  const end   = () => { dragging = false; };
  bar.addEventListener('mousedown', start);
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  bar.addEventListener('touchstart', start, {passive:true});
  window.addEventListener('touchmove', move, {passive:false});
  window.addEventListener('touchend', end);

  applyStep(); paint(); tick();
})();
</script>
</body>
</html>
'''


def write_html(shapes, width, height, title, target, ghost_data_uri, base=0.028, dur=0.5):
    defs, paths = [], []
    for index, shape in enumerate(shapes):
        rings, fill = shape[0], shape[1]
        grad = shape[5] if len(shape) > 5 else None
        if grad:
            gid = f"g{index}"
            defs.append(gradient_def(gid, grad))
            paint = f"url(#{gid})"
        else:
            paint = fill
        paths.append(f'<path style="--i:{index}" pathLength="1" fill="{paint}" '
                     f'stroke="{paint}" fill-rule="evenodd" d="{rings_to_path(rings)}"/>')

    ghost_div = (f'<div id="ghost" style="background-image:url({ghost_data_uri})"></div>'
                 if ghost_data_uri else '<div id="ghost"></div>')

    page = (PAGE
            .replace("__DEFS__", "".join(defs))
            .replace("__PATHS__", "\n".join(paths))
            .replace("__TITLE__", title)
            .replace("__W__", str(width))
            .replace("__H__", str(height))
            .replace("__N__", str(len(shapes)))
            .replace("__STEP__", f"{base:g}")
            .replace("__DUR__", f"{dur:g}")
            .replace("__BASE__", f"{base:g}")
            .replace("__SIZE__", f"{width}×{height}")
            .replace("__KBSIZE__", f"{len(''.join(paths))/1024:.0f} KB"))
    page = page.replace('<div id="ghost"></div>', ghost_div)
    target.write_text(page, encoding="utf-8")


def ghost_uri(path, width, height, background, fmt="webp", quality=90, scale=2.0):
    """内嵌对照层。三个曾经的坑，这里都避开：

    ① 透明通道：不能直接 `.convert("RGB")`，那会把透明像素变成它底下的原始
       RGB（通常是黑或白），圆角图标会显示成黑角。要先合成到底色。
    ② 尺寸：对照层必须对齐描摹尺寸，否则与产物对不上，缩放还会再糊一层。
       默认按 2× 描摹尺寸取（兼顾高清屏），但绝不超过原图本身的分辨率。
    ③ 编码：JPEG 有损压缩会糊掉形状边缘的柔光和过渡（实测 q72 时边缘区
       色差可达 70+）。默认改用 WebP q90，同为有损但保真度高得多。
    """
    import base64
    import io
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGBA")
        matte = Image.new("RGBA", image.size, (*background, 255))
        rgb = Image.alpha_composite(matte, image).convert("RGB")
        target = (min(round(width * scale), rgb.width), min(round(height * scale), rgb.height))
        if rgb.size != target:
            rgb = rgb.resize(target, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if fmt == "png":
            rgb.save(buffer, "PNG", optimize=True)
            mime = "image/png"
        elif fmt == "jpeg":
            rgb.save(buffer, "JPEG", quality=quality, optimize=True, subsampling=0)
            mime = "image/jpeg"
        else:
            rgb.save(buffer, "WEBP", quality=quality, method=6)
            mime = "image/webp"
    return f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode()


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="描摹成 SVG + 生成逐笔绘制回放页")
    parser.add_argument("input", type=Path)
    parser.add_argument("--out-prefix", default="./svg-art",
                        help="输出前缀；建议指向作品专属子目录，别直接写到工作区根目录")
    parser.add_argument("--max-width", type=int, default=760)
    parser.add_argument("--colors", type=int, default=96)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--epsilon", type=float, default=0.32)
    parser.add_argument("--min-area", type=int, default=6)
    # 底板默认纸白：与 make_art / web_run 的底板保持同一套约定（白纸作画）
    parser.add_argument("--background", default="#f0f0f0")
    parser.add_argument("--dark-cut", type=int, default=14, help="与底色色差≤此值的像素压平为底色")
    parser.add_argument("--title", default="纯 SVG 描摹插画")
    parser.add_argument("--base", type=float, default=0.028, help="1× 下每笔间隔（秒）")
    parser.add_argument("--dur", type=float, default=0.5, help="单笔动画时长（秒）")
    parser.add_argument("--no-ghost", action="store_true", help="不内嵌原图对照层")
    parser.add_argument("--ghost-format", choices=["webp", "png", "jpeg"], default="webp",
                        help="对照层编码格式；webp 兼顾体积与保真（默认），png 无损但大")
    parser.add_argument("--ghost-quality", type=int, default=90, help="对照层有损质量，默认 90")
    parser.add_argument("--ghost-scale", type=float, default=2.0,
                        help="对照层相对描摹尺寸的倍数，默认 2.0（高清屏用），不会超过原图分辨率")
    parser.add_argument("--order", choices=["sketch", "area", "luma"], default="sketch",
                        help="画笔顺序。sketch=先起线稿再铺大色块后抠细节（默认，接近人的画法）；"
                             "area=纯粹大到小；luma=亮到暗（原版工具的旧行为）")
    parser.add_argument("--sketch-ratio", type=float, default=0.02,
                        help="线稿阶段占全部笔数的比例，默认 0.02（2%%）")
    parser.add_argument("--no-gradients", action="store_true",
                        help="关闭局部渐变，所有形状都用纯色平涂")
    parser.add_argument("--gradient-min-area", type=int, default=24,
                        help="面积不小于此值的形状才拟合渐变，默认 24")
    args = parser.parse_args()

    background = tuple(int(args.background[i:i + 2], 16) for i in (1, 3, 5))
    started = time.perf_counter()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)   # 目录不存在时自动建，别指望调用方先 mkdir

    print(f"[1/4] 载入与预处理 {args.input.name}")
    reference, original = load_reference(args.input, args.max_width, background, args.dark_cut)
    height, width = reference.shape[:2]
    print(f"      原图 {original[0]}×{original[1]} → 描摹 {width}×{height}")

    print("[2/4] 量化与邻接合并")
    shapes = trace(reference, args.colors, args.passes, args.epsilon,
                   args.min_area, background, args.order, args.sketch_ratio,
                   not args.no_gradients, args.gradient_min_area, progress=print)

    vertices = sum(len(r) for shape in shapes for r in shape[0])
    print(f"[3/4] 输出 SVG：{len(shapes)} 笔 / {vertices} 顶点")
    svg_path = Path(f"{args.out_prefix}.svg")
    write_svg(shapes, width, height, background, svg_path)

    print("[4/4] 输出逐笔回放 HTML")
    uri = "" if args.no_ghost else ghost_uri(args.input, width, height, background,
                                             args.ghost_format, args.ghost_quality,
                                             args.ghost_scale)
    html_path = Path(f"{args.out_prefix}.html")
    write_html(shapes, width, height, args.title, html_path, uri, args.base, args.dur)

    report = {
        "input": str(args.input), "original_size": list(original),
        "trace_size": [width, height], "strokes": len(shapes), "vertices": vertices,
        "gradients": sum(1 for s in shapes if len(s) > 5 and s[5]),
        "svg_bytes": svg_path.stat().st_size, "html_bytes": html_path.stat().st_size,
        "settings": {"max_width": args.max_width, "colors": args.colors, "passes": args.passes,
                     "epsilon": args.epsilon, "min_area": args.min_area,
                     "background": args.background, "dark_cut": args.dark_cut,
                     "order": args.order, "sketch_ratio": args.sketch_ratio},
        "playback_seconds_1x": round((len(shapes) - 1) * args.base + args.dur, 1),
        "seconds": round(time.perf_counter() - started, 2),
    }
    Path(f"{args.out_prefix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
