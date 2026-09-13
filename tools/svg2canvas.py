#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
svg2canvas.py —— 把 build_svg_art.py 生成的巨型 SVG 改造成 Canvas 回放页。

为什么要做这件事：
  * 原生 SVG 把 18 万个 <path> 全塞进 DOM，手机一打开就卡死；
  * 改成 Canvas：DOM 里只有 1 个元素，数据走二进制，逐帧增量绘制。
  * 顺带把顶点坐标打包成 Int16（×10 精度），比 SVG 文本小得多。

用法：
  python3 svg2canvas.py <input.svg> <原图> <输出.html> [标题]

体积参考：182640 笔 / 149 万顶点 → payload 约 12 MB。
"""
import sys, re, math, base64, struct, array, io, json, os
import numpy as np

TEMPLATE_W, TEMPLATE_H = 1440, 2162   # 仅作回退默认值；真实尺寸一律从 SVG 的 viewBox 读
from PIL import Image


def _grad_color_at(grad, x, y):
    """取渐变在画布某点上的颜色（把大渐变块拆条时用）。"""
    if not grad:
        return None
    x1, y1, x2, y2, c1, c2 = grad
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return c1
    k = ((x - x1) * dx + (y - y1) * dy) / L2
    k = 0.0 if k < 0 else (1.0 if k > 1 else k)
    r1, g1, b1 = (c1 >> 16) & 255, (c1 >> 8) & 255, c1 & 255
    r2, g2, b2 = (c2 >> 16) & 255, (c2 >> 8) & 255, c2 & 255
    return ((int(r1 + (r2 - r1) * k) << 16) |
            (int(g1 + (g2 - g1) * k) << 8) |
            int(b1 + (b2 - b1) * k))


def _split_bands(rings, area, max_area=9000):
    """把面积过大的色块按扫描线切成横条，让它像大笔刷一样一条条刷上去，
    而不是"啪"地糊上一整片。返回若干组 rings（每组一个形状）。"""
    import cv2
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    x0, y0 = int(min(xs)) - 1, int(min(ys)) - 1
    x1, y1 = int(max(xs)) + 2, int(max(ys)) + 2
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or w * h > 6000000:
        return [rings]
    mask = np.zeros((h, w), np.uint8)
    polys = [np.array([[(int(p[0]) - x0, int(p[1]) - y0) for p in r]], np.int32) for r in rings]
    cv2.fillPoly(mask, polys, 1, lineType=cv2.LINE_8)
    # 目标：每条 bbox 面积 ≈ max_area。step 由宽度反解，并夹在 [8, h] 之间
    step = max(8, min(h, int(round(max_area / max(1.0, w)))))
    out = []
    for yy in range(0, h, max(1, step)):
        # 注意 yy+step+1：让相邻两条共享一行，否则 1px 空档会露出底色形成白线
        band = mask[yy:min(h, yy + step + 1)]
        if not band.any():
            continue
        cnts, _ = cv2.findContours(band, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            if len(c) < 3:
                continue
            poly = [(float(q[0][0]) + x0, float(q[0][1]) + yy + y0) for q in c]
            out.append([poly])
    return out if out else [rings]


def parse_svg(path):
    # 画布尺寸必须从 SVG 的 viewBox 读，不能写死 —— 否则线稿层会用错的尺寸去提边缘
    with open(path, encoding='utf-8') as fh:
        head = fh.read(4096)
    mv = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
    CW = int(float(mv.group(1))) if mv else 1440
    CH = int(float(mv.group(2))) if mv else 2162

    # 坐标按 Int16 存（省一半体积）。乘一个因子保留亚像素精度，
    # 但乘完不能超过 Int16 上限 32767 —— 否则大图会 OverflowError。
    #   长边 ≤3276 → ×10（0.1px）    ≤6553 → ×5     ≤16383 → ×2     更大 → ×1
    # 溢出会一路跑到 [3/4] 输出 SVG 时才炸，所以这里必须按尺寸算准。
    VMAX = 32767
    span = max(CW, CH, 1)
    SCALE = max(1, min(10, VMAX // span))
    DEC = 1.0 / SCALE                       # 解码时用
    grads, gidx = [], {}
    vert = array.array('h')          # x,y 交替（坐标 ×10，Int16）
    areas = array.array('I')         # 每笔 bbox 面积（用于按视觉重量分配时间）
    offs = array.array('I', [0])     # 每笔的顶点起点
    cols = array.array('I', [0])     # 每笔颜色（0x01xxxxxx = 渐变索引）
    cols.pop()
    nring = array.array('I', [0])    # 每笔的环起点（在 roffs 里的下标）
    roffs = array.array('I')         # 每个环的顶点起点
    gpat = re.compile(
        r'<linearGradient id="(g\d+)" gradientUnits="userSpaceOnUse" '
        r'x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)">'
        r'<stop offset="0" stop-color="(#[0-9a-fA-F]{6})"/>'
        r'<stop offset="1" stop-color="(#[0-9a-fA-F]{6})"/>')
    # 宽松匹配：兼容补过 stroke 的 path（<path fill=.. stroke=.. fill-rule=.. d=..>）
    ppat = re.compile(r'fill="([^"]+)"[^>]*?d="([^"]*)"')
    n = 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            if '<linearGradient' in line:
                for m in gpat.finditer(line):
                    gid, x1, y1, x2, y2, c1, c2 = m.groups()
                    gidx[gid] = len(grads)
                    grads.append((float(x1), float(y1), float(x2), float(y2),
                                  int(c1[1:], 16), int(c2[1:], 16)))
            elif line.startswith('<path'):
                m = ppat.search(line)
                if not m:
                    continue
                fill, d = m.groups()
                grad = grads[gidx[fill[5:-1]]] if fill.startswith('url(') else None
                base_col = int(fill[1:], 16) if not grad else 0
                rings = []
                for seg in d.split('M'):
                    if not seg:
                        continue
                    nums = seg.rstrip('Zz ').split()
                    pts = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums) - 1, 2)]
                    if len(pts) >= 3:
                        rings.append(pts)
                if not rings:
                    continue
                xs = [p[0] for r in rings for p in r]; ys = [p[1] for r in rings for p in r]
                barea = (max(xs) - min(xs)) * (max(ys) - min(ys))
                groups = _split_bands(rings, barea) if barea > 7000 else [rings]
                for g in groups:
                    gx = [p[0] for r in g for p in r]; gy = [p[1] for r in g for p in r]
                    garea = max(1.0, (max(gx) - min(gx)) * (max(gy) - min(gy)))
                    if grad:
                        col = _grad_color_at(grad, (min(gx) + max(gx)) / 2, (min(gy) + max(gy)) / 2)
                    else:
                        col = base_col
                    for r in g:
                        roffs.append(len(vert) // 2)
                        for pt in r:
                            vert.append(int(round(pt[0] * SCALE)))
                            vert.append(int(round(pt[1] * SCALE)))
                    areas.append(int(garea))
                    offs.append(len(vert) // 2)
                    nring.append(len(roffs))
                    cols.append(col)
                    n += 1
    return dict(n=n, nv=len(vert) // 2, nr=len(roffs), ng=len(grads), areas=areas,
                w=CW, h=CH, scale=SCALE,
                vert=vert, offs=offs, cols=cols, nring=nring, roffs=roffs, grads=grads)


def pack(d):
    blob = struct.pack('<4sIIII', b'HX02', d['n'], d['nv'], d['nr'], d['ng'])
    blob += d['vert'].tobytes() + d['offs'].tobytes() + d['cols'].tobytes()
    blob += d['nring'].tobytes() + d['roffs'].tobytes()
    blob += array.array('f', [x for g in d['grads'] for x in g[:4]]).tobytes()
    blob += array.array('I', [c for g in d['grads'] for c in g[4:]]).tobytes()
    blob += d['areas'].tobytes()          # 末尾追加：每笔 bbox 面积（Uint32）
    return base64.b64encode(blob).decode()


def build_lineart(photo, W, H, dark=0.78, seg_pts=16, min_pts=8, approx=1.0):
    """从原图提真正的线稿：保边平滑 → Canny 得到 1px 边缘 → **追踪成有序折线** →
    按段切开。返回 [(点列, 颜色)]，每段是一「笔」。

    关键：绝不能拿 findContours 的外轮廓去 fill —— 边缘密集处（树叶、百叶窗、木纹）
    膨胀后会连成大块，轮廓近似成多边形再填充就是一坨几何色块，那不是线稿。
    这里改成「描边」：1px 边缘直接追踪成折线，用 stroke 画出来才是线。"""
    import cv2
    img = cv2.imread(photo)
    if img is None:
        return []
    if img.shape[1] != W or img.shape[0] != H:
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    sm = cv2.bilateralFilter(img, 9, 55, 55)
    sm = cv2.bilateralFilter(sm, 9, 55, 55)
    gray = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY)
    edge = cv2.Canny(gray, 60, 150)

    # 把边缘像素放进集合，按 8 邻域追踪成一条条链
    pts = set(zip(*np.nonzero(edge)[::-1]))          # {(x, y), ...}
    NEIGH = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))

    def walk(start):
        chain = [start]
        pts.discard(start)
        cur = start
        while True:
            x, y = cur
            nxt = None
            for dx, dy in NEIGH:
                q = (x + dx, y + dy)
                if q in pts:
                    nxt = q
                    break
            if nxt is None:
                return chain
            chain.append(nxt)
            pts.discard(nxt)
            cur = nxt

    # 先从头端点起步（度=1），保证一条线尽量完整；剩下的（闭环）再任意起头
    ends = [q for q in pts
            if sum(1 for dx, dy in NEIGH if (q[0] + dx, q[1] + dy) in pts) == 1]
    chains = []
    for p0 in ends:
        if p0 in pts:
            c = walk(p0)
            if len(c) >= min_pts:
                chains.append(c)
    while pts:
        c = walk(next(iter(pts)))
        if len(c) >= min_pts:
            chains.append(c)

    out = []
    for c in chains:
        arr = np.array(c, np.int32).reshape(-1, 1, 2)
        arr = cv2.approxPolyDP(arr, approx, False).reshape(-1, 2)
        if len(arr) < 3:
            continue
        x, y = int(arr[0][0]), int(arr[0][1])
        b, gg, r = [int(v) for v in img[max(0, min(H - 1, y)), max(0, min(W - 1, x))]]
        col = ((int(r * dark) << 16) | (int(gg * dark) << 8) | int(b * dark)) & 0xFFFFFF
        # 按段切开，让线稿是一笔一笔勾出来的，而不是一整条瞬间出现
        for k in range(0, len(arr) - 1, seg_pts):
            seg = arr[k:k + seg_pts + 1]
            if len(seg) < 3:
                continue
            out.append((seg, col))
    # 长线先落笔（起稿时先拉主轮廓）
    out.sort(key=lambda s: -cv2.arcLength(s[0].reshape(-1, 1, 2), False))
    return out


TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>
  :root{--bg:#07070a;--panel:#131320;--line:#282842;--gold:#c9a227;--text:#e9e9f2;--dim:#71718c;
    --ghostimg:url(__GHOST__)}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--text);overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    display:flex;flex-direction:column;-webkit-text-size-adjust:100%}
  header{flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:8px 12px;
    border-bottom:1px solid var(--line);background:linear-gradient(180deg,#141422,#0d0d16)}
  header h1{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1 1 auto}
  header .n{font-size:11px;color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums}
  .stage{position:relative;flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;
    padding:8px;gap:14px}
  /* .frame 是「视口」：撑满整个可用区域，图片按适配尺寸居中显示。
     不要给它 aspect-ratio —— 那样它会缩成「贴着图片的小框」，
     放大后只能在小框里裁剪，用不上整屏（手机全屏时尤其明显）。
     放大时图片在视口里平移，超出部分正好被这里的 overflow 裁掉。 */
  .frame{position:relative;flex:1 1 auto;min-width:0;align-self:stretch;overflow:hidden;touch-action:none}
  /* 跟随缩放/平移的「画面」容器：canvas 和对照层都放里面，一次 transform 全部同步 */
  .view{position:absolute;left:0;top:0;will-change:transform}
  /* 底板色 = 生成时的底色（__BG__）。绝不能写死浅色 —— 深色图里「与底色
     相近、被整簇跳过的深色块」会露出这层底板，浅色底板会让它满屏白斑 */
  canvas{display:block;width:100%;height:100%;background:__BG__}
  #ghost{position:absolute;inset:0;background-image:var(--ghostimg);background-size:100% 100%;
    background-repeat:no-repeat;clip-path:inset(0 0 0 50%);pointer-events:none}
  /* 宽屏并排栏：只显示跟当前帧同步的原图块 */
  #side{display:none;position:relative;flex:0 0 auto;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden;
    background-image:var(--ghostimg);background-size:100% 100%;background-repeat:no-repeat}
  #side .tag{position:absolute;left:9px;top:9px;font-size:11px;letter-spacing:.5px;
    color:#f2f2f8;background:rgba(0,0,0,.48);padding:3px 9px;border-radius:5px;
    -webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px)}
  #split{position:absolute;top:0;bottom:0;left:50%;width:2px;background:var(--gold);
    opacity:.85;pointer-events:none;box-shadow:0 0 8px rgba(201,162,39,.6)}
  footer{flex:0 0 auto;padding:8px 12px calc(10px + env(safe-area-inset-bottom));
    border-top:1px solid var(--line);background:linear-gradient(0deg,#141422,#0d0d16);
    display:flex;flex-direction:column;gap:8px}
  /* 伪全屏（点「全屏」或按 F）：藏起页头/页脚，画面区占满整屏。
     不用 Fullscreen API —— Android WebView（VIA 等）的宿主会把它当
     「视频全屏」处理并强制横屏，竖图也被转；CSS 方案完全绕开它。 */
  body.fs header, body.fs footer{display:none}
  body.fs .stage{padding:0}
  body.fs .zoombar{bottom:14px}
  #fsExit{display:none;position:fixed;right:10px;top:10px;z-index:30;
    font:inherit;font-size:12.5px;color:var(--text);background:rgba(10,10,16,.74);
    border:1px solid var(--line);border-radius:9px;padding:7px 12px;cursor:pointer;
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  body.fs #fsExit{display:block}
  /* 控制栏：窄屏下必须能横滑，否则右侧的倍速按钮会被挤出屏幕点不到 */
  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
  .row.scroll{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;
    -webkit-overflow-scrolling:touch;scrollbar-width:none;padding-bottom:2px}
  .row.scroll::-webkit-scrollbar{display:none}
  @media (max-width:520px){
    .row{gap:6px}
    button{padding:6px 10px;font-size:12px}
    .sp{flex:0 0 auto}
    .keys{display:none !important}
  }
  .bar{position:relative;height:22px;flex:1 1 auto;display:flex;align-items:center;cursor:pointer;touch-action:none}
  .bar .track{position:absolute;left:0;right:0;height:6px;border-radius:3px;background:#22223a}
  .bar .fill{position:absolute;left:0;height:6px;border-radius:3px;width:0;
    background:linear-gradient(90deg,#7a6420,var(--gold))}
  .bar .knob{position:absolute;width:12px;height:12px;border-radius:50%;background:#fff;
    transform:translateX(-6px);box-shadow:0 1px 4px rgba(0,0,0,.6)}
  button{font:inherit;font-size:13px;color:var(--text);background:var(--panel);
    border:1px solid var(--line);border-radius:7px;padding:7px 12px;cursor:pointer;
    white-space:nowrap;flex:0 0 auto}
  button:active{background:#1b1b2c}
  button.on{background:var(--gold);border-color:var(--gold);color:#1a1200;font-weight:600}
  .sp{display:flex;gap:4px}
  .sp button{padding:5px 8px;font-size:11px}
  .meta{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
  #toast{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:rgba(10,10,16,.86);
    border:1px solid var(--line);border-radius:8px;padding:10px 16px;font-size:13px;display:none;pointer-events:none}

  /* 缩放控件：毛玻璃浮框，钉在「界面」右下角（画面区底部，紧挨底部工具栏上方）。
     定位基准是 .stage（整个观感区域）而不是 .frame（画面）——
     相对画面定位时它会跟着图走，图小的时候正好压在图上。 */
  .zoombar{position:absolute;right:10px;bottom:10px;z-index:10;
    display:flex;gap:3px;align-items:center;
    background:rgba(10,10,16,.74);border:1px solid var(--line);border-radius:9px;
    padding:4px;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  .zoombar button{font:inherit;font-size:13px;line-height:1;color:var(--text);
    background:transparent;border:0;border-radius:6px;padding:6px 8px;cursor:pointer;
    min-width:30px;text-align:center;flex:0 0 auto}
  .zoombar .lvl{font-size:11px;color:var(--dim);padding:6px 3px;min-width:44px;
    text-align:center;font-variant-numeric:tabular-nums}
  @media (hover:hover) and (pointer:fine){
    .zoombar button:hover{background:#232338}
  }

  /* ---------- 桌面端 / 宽屏适配 ---------- */
  /* 窄屏：倍速组单独一行，永远点得到 */
  @media (max-width:640px){
    footer{padding:8px 10px calc(10px + env(safe-area-inset-bottom))}
    .sp button{padding:5px 9px}
  }
  @media (min-width:900px){
    header{padding:11px 22px;gap:14px}
    header h1{font-size:15px}
    header .n{font-size:12px}
    .stage{padding:18px}
    .frame{border-radius:7px;box-shadow:0 24px 70px rgba(0,0,0,.85),0 0 0 1px var(--line)}
    footer{padding:12px 22px 16px;gap:10px}
    button{font-size:13px;padding:8px 15px;border-radius:8px}
    .sp button{font-size:12px;padding:6px 10px}
    .bar{height:26px}
    .bar .track,.bar .fill{height:7px}
    .meta{font-size:12px}
  }
  @media (min-width:1440px){
    header h1{font-size:16px}
    .stage{padding:22px}
    footer{padding:14px 30px 20px}
  }
  /* 只有真鼠标设备才给 hover 反馈，触屏不受影响 */
  @media (hover:hover) and (pointer:fine){
    button{transition:background .12s ease,border-color .12s ease,color .12s ease}
    button:hover{background:#1f1f34;border-color:#3f3f66}
    button.on:hover{background:#d9b23c;border-color:#d9b23c}
    .bar:hover .track{background:#2c2c4a}
    .bar:hover .knob{transform:translateX(-6px) scale(1.18)}
    .bar .knob{transition:transform .1s ease}
    #ghostBtn:hover{background:#1f1f34}
  }
  /* 键盘快捷键提示：只在桌面端显示 */
  .keys{display:none;font-size:11px;color:#5a5a78}
  @media (min-width:900px){ .keys{display:inline} }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="n">__NSTR__ 笔 · Canvas 回放</div>
</header>

<div class="stage">
  <div class="frame" id="frame">
    <div class="view" id="view">
      <canvas id="art" width="__W__" height="__H__"></canvas>
      <div id="ghost"></div>
      <div id="split"></div>
    </div>
    <div id="toast">绘制中…</div>
  </div>
  <div id="side"><div class="tag">原图</div></div>
  <div class="zoombar" id="zoombar">
    <button id="zOut" title="缩小（滚轮 / 双指捏合）">−</button>
    <span class="lvl" id="zLvl">100%</span>
    <button id="zIn" title="放大（滚轮 / 双指捏合）">＋</button>
    <button id="zFit" title="适应窗口（按 0）">⤢</button>
    <button id="zRst" title="实际大小（按 1）">1:1</button>
  </div>
</div>

<footer>
  <div class="row">
    <div class="bar" id="bar">
      <div class="track"></div><div class="fill" id="fill"></div><div class="knob" id="knob"></div>
    </div>
    <span class="meta" id="pct">0%</span>
  </div>
  <div class="row scroll">
    <button id="play">▶ 播放</button>
    <button id="reset" title="回到开头">↺</button>
    <button id="ghostBtn" class="on" title="显示/隐藏原图对照">对照</button>
    <button id="full" title="全屏（F）">⛶ 全屏</button>
    <span class="keys">空格 播放 · ←→ 快进退 · 滚轮缩放 · 0 适应 · F 全屏</span>
    <div style="flex:1 1 auto;min-width:8px"></div>
    <div class="sp">
      <button data-s="0.5">0.5×</button>
      <button data-s="1" class="on">1×</button>
      <button data-s="2">2×</button>
      <button data-s="4">4×</button>
    </div>
  </div>
</footer>
<button id="fsExit" type="button">✕ 退出全屏</button>

<script id="payload" type="text/plain">__PAYLOAD__</script>
<script>
(function(){
"use strict";
var CV=document.getElementById("art"), W=__W__, H=__H__;
var fitW=0, fitH=0;                     /* 图片的「适配尺寸」：显示在视口中央的基准大小 */
/* 缩放状态。必须在这里就定义：下面的 layout() 会立即执行并读它。 */
var fitScale = 1;                       /* 适应窗口时 1 画布像素 = fitScale 屏幕像素 */
var zoom = { s: 1, tx: 0, ty: 0 };

var DEC=__DEC__;                       /* 顶点解码因子：顶点值 × DEC = 画布坐标 */
var ctx=CV.getContext("2d",{alpha:false});
var b64=document.getElementById("payload").textContent.trim().replace(/\s+/g,"");
var raw=atob(b64), L=raw.length, bin=new Uint8Array(L);
for(var i=0;i<L;i++) bin[i]=raw.charCodeAt(i);
var dv=new DataView(bin.buffer);
var N=dv.getUint32(4,true), NV=dv.getUint32(8,true), NR=dv.getUint32(12,true), NG=dv.getUint32(16,true);
var p=20;
var V=new Int16Array(bin.buffer,p,NV*2); p+=NV*4;
var OFF=new Uint32Array(bin.buffer,p,N+1); p+=(N+1)*4;
var COL=new Uint32Array(bin.buffer,p,N);   p+=N*4;
var NRI=new Uint32Array(bin.buffer,p,N+1); p+=(N+1)*4;
var ROFF=new Uint32Array(bin.buffer,p,NR); p+=NR*4;
var GP=new Float32Array(bin.buffer,p,NG*4);p+=NG*16;
var GC=new Uint32Array(bin.buffer,p,NG*2);

var gradCache=new Array(NG);
function paintRange(from,to){
  for(var i=from;i<to;i++){
    var v0=OFF[i], v1=OFF[i+1];
    if(v1<=v0) continue;
    var r0=NRI[i], r1=NRI[i+1];
    ctx.beginPath();
    for(var r=r0;r<r1;r++){
      var s=ROFF[r];
      var e=(r+1<NR && ROFF[r+1]<v1)?ROFF[r+1]:v1;
      if(e<=s) continue;
      ctx.moveTo(V[s*2]*DEC, V[s*2+1]*DEC);
      for(var v=s+1;v<e;v++) ctx.lineTo(V[v*2]*DEC, V[v*2+1]*DEC);
      ctx.closePath();
    }
    var c=COL[i];
    if(c&0x02000000){                      /* 线稿笔：描边，不填充 */
      ctx.strokeStyle="rgb("+((c>>16)&255)+","+((c>>8)&255)+","+(c&255)+")";
      ctx.lineWidth=LINEW;
      ctx.lineCap="round"; ctx.lineJoin="round";
      ctx.beginPath();
      for(var rr=r0;rr<r1;rr++){
        var ss=ROFF[rr];
        var ee=(rr+1<NR && ROFF[rr+1]<v1)?ROFF[rr+1]:v1;
        if(ee<=ss) continue;
        ctx.moveTo(V[ss*2]*DEC, V[ss*2+1]*DEC);
        for(var vv=ss+1;vv<ee;vv++) ctx.lineTo(V[vv*2]*DEC, V[vv*2+1]*DEC);
      }
      ctx.stroke();
      continue;
    }
    if(c&0x01000000){
      var gi=c&0xFFFFFF, g=gradCache[gi];
      if(!g){
        var o=gi*4;
        g=ctx.createLinearGradient(GP[o],GP[o+1],GP[o+2],GP[o+3]);
        g.addColorStop(0,"#"+("000000"+(GC[gi*2]|0).toString(16)).slice(-6));
        g.addColorStop(1,"#"+("000000"+(GC[gi*2+1]|0).toString(16)).slice(-6));
        gradCache[gi]=g;
      }
      ctx.fillStyle=g;
    }else{
      ctx.fillStyle="rgb("+((c>>16)&255)+","+((c>>8)&255)+","+(c&255)+")";
    }
    ctx.fill("evenodd");
  }
}
function clearAll(){ ctx.fillStyle="__BG__"; ctx.fillRect(0,0,W,H); }   /* 底板色必须与 SVG 里的一致 */

var cur=0, playing=false, speed=1, raf=null, last=0;
var DURATION=__DUR__;  // 1× 总时长（秒）
/* ---- 按「视觉重量」分配时间 ----
   色块：时间 ∝ bbox 面积（大笔慢慢铺开，小碎片快过）。
   线稿：时间 ∝ √bbox面积（≈ 线的长度）—— 勾线本就是「一笔划过」，
   不刻意给它留时间，也不按面积吃时间。
   只有显式指定 SKETCH_T > 0 时，才把线稿反解成固定占比的起稿阶段。   */
var SKETCH_P=__SP__, SKETCH_T=__ST__;
var LINEW=__LINEW__;                 /* 线稿线宽（画布原生像素） */
var NL=Math.max(1,Math.round(SKETCH_P*N));
var AREA=null;
try{ AREA=new Uint32Array(bin.buffer, bin.byteLength-N*4, N); }catch(e){ AREA=null; }
var CUM=new Float64Array(N+1), TOTW=1;
(function(){
  var i, w, acc=0, sumW=0, W=new Float64Array(N);
  if(!AREA){ for(i=0;i<N;i++) CUM[i+1]=i+1; TOTW=N; return; }
  if(SKETCH_T>0){
    /* 指定了起稿时长：色块先按视觉重量算好，线稿反解成固定占比 */
    for(i=NL;i<N;i++){ w=0.15+AREA[i]*0.002; W[i]=w; sumW+=w; }
    var LW=(SKETCH_T*sumW)/((1-SKETCH_T)*NL);
    for(i=0;i<NL;i++) W[i]=LW;
  }else{
    /* 默认：线稿不单独占时间，但用「线的长度」而不是「bbox 面积」来估它的视觉重量——
       线是细长的，bbox 会把整条线的跨度都圈进去、明显超重（实测能把起稿拖到 13 秒）。
       √bbox面积 ≈ 线的特征长度，和色块那边的「面积」在几何上对齐。 */
    for(i=0;i<NL;i++) W[i]=0.15+Math.sqrt(AREA[i])*0.05;
    for(i=NL;i<N;i++) W[i]=0.15+AREA[i]*0.002;
  }
  for(i=0;i<N;i++){ acc+=W[i]; CUM[i+1]=acc; }
  TOTW=acc||1;
})();
function strokeAtTime(p){          // 时间进度 0..1 → 笔序号
  if(p<=0) return 0; if(p>=1) return N;
  var target=p*TOTW, lo=0, hi=N;
  while(lo<hi){ var mid=(lo+hi)>>1; if(CUM[mid]<target) lo=mid+1; else hi=mid; }
  return lo;
}
function timeOfStroke(n){          // 笔序号 → 时间进度
  n=Math.max(0,Math.min(N,n)); return CUM[n]/TOTW;
}
var timePos=0;

var fillEl=document.getElementById("fill"), knobEl=document.getElementById("knob"),
    pctEl=document.getElementById("pct"), playBtn=document.getElementById("play"),
    bar=document.getElementById("bar"), splitEl=document.getElementById("split");

var TOTAL_STR="__NSTR__";
function syncUI(){
  var f=cur/N;
  fillEl.style.width=(f*100)+"%";
  knobEl.style.left=(f*100)+"%";
  pctEl.textContent=Math.round(f*100)+"% \u00b7 "+Math.round(cur).toLocaleString()+" 笔";
}
function step(ts){
  if(!playing) return;
  if(!last) last=ts;
  var dt=(ts-last)/1000; last=ts;
  if(dt>0.25) dt=0.25;
  timePos=Math.min(1, timePos + dt*speed/DURATION);
  var target=strokeAtTime(timePos);
  if(target-cur>=1){
    var a=Math.floor(cur), b=Math.floor(target);
    paintRange(a,b); cur=target; syncUI();
  }
  if(timePos>=1){ playing=false; playBtn.textContent="↺ 重播"; playBtn.classList.remove("on"); syncUI(); return; }
  raf=requestAnimationFrame(step);
}
function play(){
  if(playing) return;
  if(cur>=N-0.5){ clearAll(); cur=0; timePos=0; syncUI(); }
  playing=true; last=0; playBtn.textContent="❚❚ 暂停"; playBtn.classList.add("on");
  raf=requestAnimationFrame(step);
}
function pause(){ playing=false; if(raf) cancelAnimationFrame(raf); playBtn.textContent="▶ 播放"; playBtn.classList.remove("on"); }
playBtn.addEventListener("click",function(){ playing?pause():play(); });

document.getElementById("reset").addEventListener("click",function(){
  pause(); clearAll(); cur=0; timePos=0; syncUI(); playBtn.textContent="▶ 播放";
});

var toastEl=document.getElementById("toast");
function seekTo(n){
  n=Math.max(0,Math.min(N,Math.round(n)));
  if(n>=cur){ paintRange(Math.floor(cur),n); }
  else { toastEl.style.display="block"; clearAll(); paintRange(0,n); toastEl.style.display="none"; }
  cur=n; timePos=timeOfStroke(n); syncUI();
  playBtn.textContent=(cur>=N-0.5)?"↺ 重播":"▶ 播放"; playBtn.classList.remove("on");
}
function posFromEvent(e){
  var r=bar.getBoundingClientRect();
  var x=(e.touches?e.touches[0].clientX:e.clientX)-r.left;
  return Math.max(0,Math.min(1,x/r.width))*N;
}
var dragging=false, seekRaf=0, seekTarget=0;
function down(e){ dragging=true; pause(); seekTarget=posFromEvent(e); seekNow(); e.preventDefault(); }
function move(e){ if(!dragging) return; seekTarget=posFromEvent(e);
  if(!seekRaf) seekRaf=requestAnimationFrame(function(){ seekRaf=0; seekNow(); });
  e.preventDefault(); }
function seekNow(){ seekTo(seekTarget); }
function up(){ dragging=false; if(seekRaf){cancelAnimationFrame(seekRaf);seekRaf=0;} }
bar.addEventListener("mousedown",down); window.addEventListener("mousemove",move); window.addEventListener("mouseup",up);
bar.addEventListener("touchstart",down,{passive:false}); window.addEventListener("touchmove",move,{passive:false}); window.addEventListener("touchend",up);

Array.prototype.forEach.call(document.querySelectorAll(".sp button"),function(b){
  b.addEventListener("click",function(){
    Array.prototype.forEach.call(document.querySelectorAll(".sp button"),function(x){x.classList.remove("on")});
    b.classList.add("on"); speed=parseFloat(b.getAttribute("data-s"))||1;
  });
});

/* ---- 对照原图 ---- */
var ghostOn=true, ghostEl=document.getElementById("ghost");
document.getElementById("ghostBtn").addEventListener("click",function(){
  ghostOn=!ghostOn; this.classList.toggle("on",ghostOn);
  if(sideMode()){ layout(); }
  else{
    ghostEl.style.display=ghostOn?"block":"none";
    splitEl.style.display=ghostOn?"block":"none";
  }
  layout();
});
var frame=document.getElementById("frame"), VIEW=document.getElementById("view");
function setSplit(clientX){
  var r=VIEW.getBoundingClientRect();          /* 分界按「画面」（不是视口）的比例算 */
  var x=Math.max(0,Math.min(100,(clientX-r.left)/r.width*100));
  ghostEl.style.clipPath="inset(0 0 0 "+x+"%)";
  splitEl.style.left=x+"%";
}
frame.addEventListener("pointerdown",function(e){ if(ghostOn){ pause(); setSplit(e.clientX); } });
frame.addEventListener("pointermove",function(e){
  if(!ghostOn) return;
  if(e.pointerType==="mouse" || e.buttons) setSplit(e.clientX);
});

/* ---- 全屏：CSS 伪全屏（藏起页头/页脚，画面区占满）----
   不用 Fullscreen API：Android WebView（VIA 等调用系统内核的浏览器）会把
   requestFullscreen 交给宿主处理，宿主常按「视频全屏」对待并强制横屏 ——
   竖图也会被转成横屏。CSS 方案不碰系统接口，所有浏览器行为一致，
   嵌在 iframe 里也能用（不需要 allowfullscreen）。 */
function toggleFull(){
  var on = document.body.classList.toggle("fs");
  var b = document.getElementById("full");
  if(b) b.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
  setTimeout(layout, 60);        /* 可用区域变了，重算画面适配 */
}
document.getElementById("full").addEventListener("click",toggleFull);
document.getElementById("fsExit").addEventListener("click",toggleFull);
/* 双击留给「放大/还原」（绑在 frame 上，黑边区域双击也有效）；全屏只用按钮或 F 键 */

/* ---- 键盘：空格播放/暂停，←→ 以 2% 步进快退快进，Home/End 首尾，F 全屏 ---- */
document.addEventListener("keydown",function(e){
  var tag=(e.target.tagName||"").toLowerCase();
  if(tag==="input"||tag==="textarea") return;
  if(e.code==="Space"||e.key===" "){ e.preventDefault(); playing?pause():play(); }
  else if(e.key==="ArrowRight"){ e.preventDefault(); pause(); seekTo(cur+N*0.02); }
  else if(e.key==="ArrowLeft"){ e.preventDefault(); pause(); seekTo(cur-N*0.02); }
  else if(e.key==="Home"){ e.preventDefault(); pause(); seekTo(0); }
  else if(e.key==="End"){ e.preventDefault(); pause(); seekTo(N); }
  else if(e.key==="f"||e.key==="F"){ e.preventDefault(); toggleFull(); }
  else if(e.key==="0"){ e.preventDefault(); pause(); resetView(); }
  else if(e.key==="1"){ e.preventDefault(); pause(); setZoomAt(1/fitScale); }
  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); setZoomAt(zoom.s*1.4); }
  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); setZoomAt(zoom.s/1.4); }
});

/* ---- 精确布局：按可用空间等比缩放画框（不依赖 aspect-ratio 的浏览器实现） ---- */
var stageEl=document.querySelector(".stage"), sideEl=document.getElementById("side");
function sideMode(){
  return ghostOn && window.innerWidth>=1000 && (window.innerWidth/window.innerHeight)>=1.0;
}
function layout(){
  var cs=getComputedStyle(stageEl);
  var aw=stageEl.clientWidth -parseFloat(cs.paddingLeft)-parseFloat(cs.paddingRight);
  var ah=stageEl.clientHeight-parseFloat(cs.paddingTop) -parseFloat(cs.paddingBottom);
  if(aw<=0||ah<=0) return;
  var dual=sideMode(), GAP=14, vw;
  if(dual){
    sideEl.style.display="block";
    vw=(aw-GAP)/2;                          /* 左栏（视口）可用宽度 */
  }else{
    sideEl.style.display="none";
    vw=aw;
  }
  /* 视口尺寸由 CSS 撑满，这里只算「图片的适配尺寸」并把它居中 */
  var s=Math.min(vw/W, ah/H);
  fitScale=s;                               /* 1:1 按钮与快捷键要用它换算 */
  fitW=Math.max(1,Math.round(W*s)); fitH=Math.max(1,Math.round(H*s));
  VIEW.style.width =fitW+"px";
  VIEW.style.height=fitH+"px";
  if(dual){ sideEl.style.width=fitW+"px"; sideEl.style.height=fitH+"px"; }
  clampPan();
  applyView();
}
window.addEventListener("resize",layout);
window.addEventListener("orientationchange",function(){ setTimeout(layout,120); });
if(window.ResizeObserver) new ResizeObserver(layout).observe(stageEl);
if(document.fullscreenElement!==undefined) document.addEventListener("fullscreenchange",layout);
layout();

/* ==================== 缩放与平移 ====================
   思路：画布本身不重绘，只给 <canvas> 套 CSS transform。
   所以放到 2400% 也只是把已有像素放大，不掉帧。
   变量 zoom.s 是「相对适应窗口」的倍数，1 = 刚好填满画框。 */
function applyView(){
  if(zoom.s <= 1.0001){ zoom.tx = 0; zoom.ty = 0; }
  var fw=frame.clientWidth, fh=frame.clientHeight;
  /* 图片视觉左上角 = 「居中基准」+ 用户平移（tx/ty 是相对居中位置的偏移） */
  var bx=(fw-fitW*zoom.s)/2+zoom.tx, by=(fh-fitH*zoom.s)/2+zoom.ty;
  VIEW.style.transformOrigin = "0 0";
  VIEW.style.transform = "translate(" + bx + "px," + by + "px) scale(" + zoom.s + ")";
  var zl = document.getElementById("zLvl");
  if(zl) zl.textContent = Math.round(zoom.s * 100) + "%";
}
/* 平移范围：图片比视口大时不许把边缘拖进视口；没占满的方向锁在居中 */
function clampPan(){
  var fw=frame.clientWidth, fh=frame.clientHeight;
  var vw=fitW*zoom.s, vh=fitH*zoom.s;
  var mx=Math.max(0,(vw-fw)/2), my=Math.max(0,(vh-fh)/2);
  zoom.tx = Math.max(-mx, Math.min(mx, zoom.tx));
  zoom.ty = Math.max(-my, Math.min(my, zoom.ty));
}
/* 以视口内的点 (cx, cy) 为锚点缩放（不传就锚定视口中心）——
   缩放后该点对应的屏幕位置保持不变，和相册/地图的手感一致。 */
function setZoomAt(ns, cx, cy){
  ns = Math.min(24, Math.max(1, ns));
  var fw=frame.clientWidth, fh=frame.clientHeight;
  var k = ns / zoom.s;
  var L = (fw-fitW*zoom.s)/2+zoom.tx, T = (fh-fitH*zoom.s)/2+zoom.ty;   /* 图片视觉左上角 */
  if(cx === undefined){ cx = fw/2; cy = fh/2; }
  var a = cx - L, b = cy - T;                    /* 锚点相对图片左上角 */
  zoom.s = ns;
  zoom.tx = cx - a*k - (fw-fitW*ns)/2;
  zoom.ty = cy - b*k - (fh-fitH*ns)/2;
  clampPan();
  applyView();
}
function resetView(){ zoom.s = 1; zoom.tx = 0; zoom.ty = 0; applyView(); }

/* 滚轮：以指针位置为锚点 */
frame.addEventListener("wheel", function(e){
  e.preventDefault();
  pause();
  var r = frame.getBoundingClientRect();
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });

/* 触屏：双指捏合缩放、放大后单指拖动平移 */
var ptrs = {}, pinch = null, pan = null;
frame.addEventListener("pointerdown", function(e){
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };
  var ids = Object.keys(ptrs);
  if(ids.length === 2){
    var a = ptrs[ids[0]], b = ptrs[ids[1]];
    var r = frame.getBoundingClientRect();
    pinch = { d: Math.hypot(a.x-b.x, a.y-b.y), s: zoom.s,
              cx:(a.x+b.x)/2 - r.left, cy:(a.y+b.y)/2 - r.top };
    pan = null;
  } else if(zoom.s > 1.0001){
    pan = { x:e.clientX, y:e.clientY, tx:zoom.tx, ty:zoom.ty };
  }
});
frame.addEventListener("pointermove", function(e){
  if(!ptrs[e.pointerId]) return;
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };
  var ids = Object.keys(ptrs);
  if(pinch && ids.length >= 2){
    e.preventDefault();
    var a = ptrs[ids[0]], b = ptrs[ids[1]];
    var d = Math.hypot(a.x-b.x, a.y-b.y);
    if(d > 0) setZoomAt(pinch.s * (d / pinch.d), pinch.cx, pinch.cy);
  } else if(pan){
    e.preventDefault();
    zoom.tx = pan.tx + (e.clientX - pan.x);
    zoom.ty = pan.ty + (e.clientY - pan.y);
    clampPan(); applyView();
  }
}, { passive:false });
function endPtr(e){
  delete ptrs[e.pointerId];
  if(Object.keys(ptrs).length < 2) pinch = null;
  if(Object.keys(ptrs).length === 0) pan = null;
}
frame.addEventListener("pointerup", endPtr);
frame.addEventListener("pointercancel", endPtr);

/* 双击画面：1× ⇄ 2.5× 切换（绑在视口上，黑边区域双击也有效） */
frame.addEventListener("dblclick", function(e){
  var r = frame.getBoundingClientRect();
  if(zoom.s > 1.01) resetView();
  else setZoomAt(2.5, e.clientX - r.left, e.clientY - r.top);
});

/* 控件按钮 */
document.getElementById("zIn").onclick  = function(){ pause(); setZoomAt(zoom.s * 1.4); };
document.getElementById("zOut").onclick = function(){ pause(); setZoomAt(zoom.s / 1.4); };
document.getElementById("zFit").onclick = function(){ pause(); resetView(); };
document.getElementById("zRst").onclick = function(){ pause(); setZoomAt(1 / fitScale); };

clearAll(); syncUI();
setTimeout(function(){ layout(); play(); },600);
})();
</script>
</body>
</html>
'''


def ghost_data_uri(img_path, w=1440):
    im = Image.open(img_path).convert("RGB")
    if im.width > w:
        im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=78, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode(), buf.tell()


def main():
    if len(sys.argv) < 4 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("""位置参数（按顺序，注意第 6 位是占位符）：
  1  in.svg            build_svg_art.py 产出的 SVG
  2  原图              用于提取线稿层和对照层
  3  out.html          输出文件
  4  标题              页面顶部显示（默认「逐笔绘制回放（Canvas 版）」）
  5  秒数              1× 速度下播完整幅画的时间（默认 80）
  6  线稿笔占比        必须填 0 —— 占位参数，实际笔数由脚本从 SVG 里读
  7  线稿时间占比      0 = 不单独控制（默认）——线稿跟色块一起按视觉重量播，
                      实测占 3~4%（90 秒里 3 秒出头）；给个 0~1 的小数则固定起稿阶段占比
  8  线宽              线稿线条宽度（画布原生像素，默认 2.6）

例：
  python3 svg2canvas.py 作品.svg 原图.jpg 作品.html "作品 · 逐笔绘制回放" 90 0 0 2.6
""")
        return
    if len(sys.argv) < 6:
        raise SystemExit("参数不足，用 --help 看用法")
    src, photo, out = sys.argv[1], sys.argv[2], sys.argv[3]
    title = sys.argv[4] if len(sys.argv) > 4 else "逐笔绘制回放（Canvas 版）"
    duration = float(sys.argv[5]) if len(sys.argv) > 5 else 80.0
    sp = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0    # 0 = 按实际线稿笔数自动算
    linew = float(sys.argv[8]) if len(sys.argv) > 8 else 2.6  # 线稿线宽
    st = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0    # 线稿阶段时间占比；0 = 不单独控制（跟色块一起按视觉重量）
    print("解析 SVG…")
    d = parse_svg(src)
    # 回放页的底板色必须和 SVG 里的底板一致：深色图里「与底色相近、被整簇
    # 跳过的块」根本不画，露出来的就是这层底板 —— 两边不一致时（比如底板
    # 写死浅色）深色图会满屏白斑（2026-09-14 用户报）。
    with open(src, encoding="utf-8") as fh:
        m_bg = re.search(r'<rect width="[\d.]+" height="[\d.]+" fill="(#[0-9a-fA-F]{6})"', fh.read())
    bgcolor = m_bg.group(1) if m_bg else "#f0f0f0"
    print("  底板色 %s（取自 SVG）" % bgcolor)
    print("  画布 %d×%d   顶点精度 1/%d px" % (d['w'], d['h'], d.get('scale', 10)))
    print("  色块笔 %d  环 %d  顶点 %d  渐变 %d" % (d['n'], d['nr'], d['nv'], d['ng']))
    if os.environ.get("NO_LINEART"):
        nline = 0
    else:
        la = build_lineart(photo, d['w'], d['h'])
        nline = len(la)
        print("  线稿笔 %d（从原图 Canny 边缘提取）" % nline)
        if nline:
            lv = array.array('h'); loffs = array.array('I', [0])
            lcols = array.array('I'); lnr = array.array('I', [0]); lro = array.array('I')
            larea = array.array('I')
            for seg, col in la:
                lro.append(len(lv) // 2)
                sx = [int(x) for x, y in seg]; sy = [int(y) for x, y in seg]
                for x, y in seg:
                    lv.append(int(round(float(x) * d['scale'])))
                    lv.append(int(round(float(y) * d['scale'])))
                larea.append(max(1, (max(sx) - min(sx) + 2) * (max(sy) - min(sy) + 2)))
                loffs.append(len(lv) // 2); lnr.append(len(lro))
                lcols.append(0x02000000 | col)          # 0x02 = 这一笔用描边画，不填充
            NV0 = len(lv) // 2; NR0 = len(lro)
            d['vert'] = lv + d['vert']
            d['offs'] = loffs + array.array('I', [o + NV0 for o in list(d['offs'])[1:]])
            d['cols'] = lcols + d['cols']
            d['nring'] = lnr + array.array('I', [r + NR0 for r in list(d['nring'])[1:]])
            d['roffs'] = lro + array.array('I', [r + NV0 for r in d['roffs']])
            d['areas'] = larea + d['areas']
            d['n'] += nline; d['nv'] = len(d['vert']) // 2; d['nr'] = len(d['roffs'])
    payload = pack(d)
    print("  打包 %.2f MB" % (len(payload) / 1e6))
    uri, gsz = ghost_data_uri(photo)
    print("  对照图 %.0f KB" % (gsz / 1024))
    html = (TEMPLATE
            .replace("__PAYLOAD__", payload)
            .replace("__GHOST__", uri)
            .replace("__TITLE__", title)
            .replace("__NSTR__", "{:,}".format(d['n']))
            .replace("__W__", str(d['w'])).replace("__H__", str(d['h']))
            .replace("__DEC__", repr(1.0 / d.get('scale', 10)))
            .replace("__DUR__", str(duration))
            .replace("__SP__", "%.4f" % (sp if sp > 0 else (nline / max(1, d['n']))))
            .replace("__ST__", str(st))
            .replace("__LINEW__", str(linew))
            .replace("__BG__", bgcolor))
    open(out, "w", encoding="utf-8").write(html)
    real_sp = sp if sp > 0 else (nline / max(1, d['n']))
    stxt = ("%.0f%% 时间" % (st * 100)) if st > 0 else "按视觉重量自然分配"
    print("写出 %s  %.2f MB  (线稿 %d 笔 = %.1f%% 笔 / %s)" % (
        out, os.path.getsize(out) / 1e6, nline, real_sp * 100, stxt))


if __name__ == "__main__":
    main()
