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

    # 线稿筛选：既看长度，也看「这条线画在哪」——
    # 皮肤/高光上的短碎线是照片纹理（噪点、眼妆过渡），不是结构轮廓，
    # 画出来就是脸上一道道脏线（用户 2026-09-14 报「脸上有黑色斑纹」）。
    #
    # 判据用「链的平均亮度」而不是「亮点占比」：真结构线总有一侧是暗的
    # （头发边、轮廓线，平均亮度低），而纯皮肤上的纹理线两侧都亮。
    # 实测（用户原图）：阈值 110 时链 530→329（保留 62% 结构线），
    # 画在亮区的比例 25%→9%；用「亮点占比」判据则会把亮皮肤上的
    # 轮廓线一起误伤（用户反馈「线稿变淡了」）。
    luma = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(int)   # int：防 uint8 累加溢出
    def keep(c):
        if len(c) < min_pts:
            return False
        return sum(luma[y, x] for (x, y) in c) / len(c) <= 110

    chains = []
    for p0 in ends:
        if p0 in pts:
            c = walk(p0)
            if keep(c):
                chains.append(c)
    while pts:
        c = walk(next(iter(pts)))
        if keep(c):
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
<!-- 图标内联成 data URI（64×64 PNG）：回放页是独立文件，下载到哪儿、发给谁，
     都得自带图标 —— 不要换成外部文件引用，那样一挪位置就没了。 -->
<link rel="icon" href="data:image/png;base64,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">
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
  /* 宽屏并排栏：右栏显示原图。里面放一个和左栏 .view 同款的可变换层（#sideView），
     但它有**自己的一套缩放/平移** —— 滚轮 / 拖动落在哪一栏上就只动哪一栏，
     方便把原图单独放大看细节，不会把左边正在绘制的那张也一起带走。
     touch-action:none 是给触屏用的：否则单指拖动会被浏览器当成滚动手势吃掉。 */
  #side{display:none;position:relative;flex:0 0 auto;touch-action:none;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden}
  #sideView{position:absolute;left:0;top:0;will-change:transform;
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
  <div id="side"><div id="sideView"></div><div class="tag">原图</div></div>
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
/* 右栏（原图）**自己的一套**缩放/平移 —— 两栏互不影响：滚轮 / 拖动落在哪一栏上
   就只动哪一栏。想单独把原图放大看细节时，不会把左边正在绘制的那张一起带走。
   右栏坐标系比左栏简单：它没有「居中基准」，#side 的尺寸就是图片的适配尺寸，
   所以图片左上角就是 (0,0)，直接和 zoom2.tx/ty 对应。
   （声明必须在这——下面的 layout() 会立即执行并读到它。） */
var zoom2 = { s: 1, tx: 0, ty: 0 };
/* 最后一次交互的是哪一栏：底部 ± 按钮、键盘 +/- 作用于它，百分比也显示它 */
var lastPane = "left";

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
    /* 补一层同色描边：色块之间会有发丝缝，不补的话缝里露出底板 ——
       深色图上就是密集的黑线/黑斑（脸这种细节密集处最明显）。
       SVG 版当年同样补过（stroke-width 0.6），Canvas 版这里一直漏着。 */
    ctx.strokeStyle=ctx.fillStyle;
    ctx.lineWidth=0.7; ctx.lineJoin="round";
    ctx.stroke();
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
var frame=document.getElementById("frame"), VIEW=document.getElementById("view"),
    SIDEVIEW=document.getElementById("sideView");
function setSplit(clientX){
  var r=VIEW.getBoundingClientRect();          /* 分界按「画面」（不是视口）的比例算 */
  var x=Math.max(0,Math.min(100,(clientX-r.left)/r.width*100));
  ghostEl.style.clipPath="inset(0 0 0 "+x+"%)";
  splitEl.style.left=x+"%";
}
frame.addEventListener("pointerdown",function(e){ if(ghostOn){ pause(); setSplit(e.clientX); } });
frame.addEventListener("pointermove",function(e){
  if(!ghostOn) return;
  if(e.buttons) setSplit(e.clientX);   /* 只在按住拖动时移动分界线；鼠标悬停不再带着它跑 */
});

/* ---- 全屏：CSS 伪全屏（藏起页头/页脚，画面区占满）----
   不用 Fullscreen API：Android WebView（VIA 等调用系统内核的浏览器）会把
   requestFullscreen 交给宿主处理，宿主常按「视频全屏」对待并强制横屏 ——
   竖图也会被转成横屏。CSS 方案不碰系统接口，所有浏览器行为一致，
   嵌在 iframe 里也能用（不需要 allowfullscreen）。 */
function toggleFull(){
  /* 嵌在别人的 iframe 里（网页端的预览框）：把「全屏」请求交给外层 ——
     自己这点「伪全屏」只能在框内变大，铺不满屏幕；外层收到后会打开
     真正的全屏预览层（再点一次则收回）。 */
  if(window.parent && window.parent !== window){
    try{ window.parent.postMessage({ type: "art:fullscreen" }, "*"); return; }catch(_){}
  }
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
  else if(e.key==="0"){ e.preventDefault(); pause(); resetView(); resetSide(); }
  else if(e.key==="1"){ e.preventDefault(); pause(); setZoomAt(1/fitScale); setZoomAt2(1/fitScale); }
  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); zoomBy(1.4); }
  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); zoomBy(1/1.4); }
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
    /* 双栏模式：右栏已经是完整的原图 —— 左栏就别再叠「半张原图 + 分界线」了，
       否则进来先看到「一半绘制一半原图」，还得手动拖分界线把它拉走 */
    ghostEl.style.display="none";
    splitEl.style.display="none";
  }else{
    sideEl.style.display="none";
    vw=aw;
    /* 单栏：恢复对照层（开着的话）*/
    ghostEl.style.display=ghostOn?"block":"none";
    splitEl.style.display=ghostOn?"block":"none";
  }
  /* 视口尺寸由 CSS 撑满，这里只算「图片的适配尺寸」并把它居中 */
  var s=Math.min(vw/W, ah/H);
  fitScale=s;                               /* 1:1 按钮与快捷键要用它换算 */
  fitW=Math.max(1,Math.round(W*s)); fitH=Math.max(1,Math.round(H*s));
  VIEW.style.width =fitW+"px";
  VIEW.style.height=fitH+"px";
  if(dual){
    sideEl.style.width=fitW+"px"; sideEl.style.height=fitH+"px";
    SIDEVIEW.style.width=fitW+"px"; SIDEVIEW.style.height=fitH+"px";
  }
  clampPan();
  applyView();
  /* 右栏尺寸跟着一起变了：它的平移范围要重算、transform 也要重新贴上去
     （两栏状态各自独立，这里必须分开处理） */
  clampPan2();
  applySide();
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
  syncZoomLabel();
}
/* 底部那个百分比：跟随「最后一次操作的那一栏」，不再固定绑在左栏上 */
function syncZoomLabel(){
  var zl = document.getElementById("zLvl");
  if(zl) zl.textContent = Math.round((lastPane==="right"?zoom2.s:zoom.s) * 100) + "%";
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

/* 滚轮：以指针位置为锚点。落点在哪一栏就动哪一栏（lastPane 供底部 ± 按钮与键盘用） */
frame.addEventListener("wheel", function(e){
  e.preventDefault();
  pause(); lastPane="left";
  var r = frame.getBoundingClientRect();
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });

/* ---------------- 右栏（原图）：自己的一套缩放 / 平移 ----------------
   滚轮落在右栏就只缩右栏，拖动也只拖右栏，跟左栏各走各的。
   原先两栏共用同一个 zoom —— 在哪儿滚都是两张一起放大；而且右栏压根没绑拖动，
   于是「拖原图拖不动、拖左栏时原图却跟着跑」。 */
function applySide(){
  if(!SIDEVIEW) return;
  SIDEVIEW.style.transformOrigin = "0 0";
  SIDEVIEW.style.transform = "translate(" + zoom2.tx + "px," + zoom2.ty + "px) scale(" + zoom2.s + ")";
  syncZoomLabel();
}
/* 右栏的平移范围：图片比它的框大时不许把边缘拖进框里（和左栏同一套规矩） */
function clampPan2(){
  if(!SIDEVIEW) return;
  var sw=sideEl.clientWidth, sh=sideEl.clientHeight;
  var mx=Math.max(0,(fitW*zoom2.s-sw)/2), my=Math.max(0,(fitH*zoom2.s-sh)/2);
  zoom2.tx = Math.max(-mx, Math.min(mx, zoom2.tx));
  zoom2.ty = Math.max(-my, Math.min(my, zoom2.ty));
}
/* 锚点 (cx,cy) 是「相对 #side 左上角」的坐标；不传就锚定右栏中心 */
function setZoomAt2(ns, cx, cy){
  ns = Math.min(24, Math.max(1, ns));
  if(cx === undefined){ cx = sideEl.clientWidth/2; cy = sideEl.clientHeight/2; }
  var k = ns / zoom2.s;
  zoom2.tx = cx - (cx - zoom2.tx)*k;
  zoom2.ty = cy - (cy - zoom2.ty)*k;
  zoom2.s = ns;
  clampPan2(); applySide();
}
function resetSide(){ zoom2.s=1; zoom2.tx=0; zoom2.ty=0; applySide(); }

sideEl.addEventListener("wheel", function(e){
  e.preventDefault();
  pause(); lastPane="right";
  var r = sideEl.getBoundingClientRect();
  setZoomAt2(zoom2.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });

/* 右栏的双指捏合 / 放大后单指拖动 —— 只动右栏 */
var ptrs2 = {}, pinch2 = null, pan2 = null;
sideEl.addEventListener("pointerdown", function(e){
  lastPane="right";
  ptrs2[e.pointerId] = { x:e.clientX, y:e.clientY };
  /* 和左栏一样捕获指针：拖出右栏范围也能收到 pointerup，松手不会粘住 */
  try{ sideEl.setPointerCapture(e.pointerId); }catch(_){}
  var ids = Object.keys(ptrs2);
  if(ids.length === 2){
    var a = ptrs2[ids[0]], b = ptrs2[ids[1]];
    var r = sideEl.getBoundingClientRect();
    pinch2 = { d:Math.hypot(a.x-b.x, a.y-b.y), s:zoom2.s,
               cx:(a.x+b.x)/2 - r.left, cy:(a.y+b.y)/2 - r.top };
    pan2 = null;
  } else if(zoom2.s > 1.0001){
    pan2 = { x:e.clientX, y:e.clientY, tx:zoom2.tx, ty:zoom2.ty };
  }
});
sideEl.addEventListener("pointermove", function(e){
  if(!ptrs2[e.pointerId]) return;
  ptrs2[e.pointerId] = { x:e.clientX, y:e.clientY };
  var ids = Object.keys(ptrs2);
  if(pinch2 && ids.length >= 2){
    e.preventDefault();
    var a = ptrs2[ids[0]], b = ptrs2[ids[1]];
    var d = Math.hypot(a.x-b.x, a.y-b.y);
    if(d > 0) setZoomAt2(pinch2.s * (d/pinch2.d), pinch2.cx, pinch2.cy);
  } else if(pan2){
    e.preventDefault();
    zoom2.tx = pan2.tx + (e.clientX - pan2.x);
    zoom2.ty = pan2.ty + (e.clientY - pan2.y);
    clampPan2(); applySide();
  }
}, { passive:false });
function endPtr2(e){
  delete ptrs2[e.pointerId];
  if(Object.keys(ptrs2).length < 2) pinch2 = null;
  if(Object.keys(ptrs2).length === 0) pan2 = null;
}
sideEl.addEventListener("pointerup", endPtr2);
sideEl.addEventListener("pointercancel", endPtr2);

/* 触屏：双指捏合缩放、放大后单指拖动平移 */
var ptrs = {}, pinch = null, pan = null;
frame.addEventListener("pointerdown", function(e){
  lastPane="left";
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };
  try{ frame.setPointerCapture(e.pointerId); }catch(_){}
  /* 捕获指针：拖出画面范围也能收到 pointerup，避免「松手后画面还跟着鼠标跑」 */
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
/* ± 作用于「最后操作的那一栏」（鼠标在哪边滚过/拖过就是哪边）；
   ⤢ 适应窗口 与 1:1 是全局动作，两栏一起归位 —— 免得一边调好了另一边还是旧的。 */
function zoomBy(k){
  if(lastPane==="right") setZoomAt2(zoom2.s * k);
  else setZoomAt(zoom.s * k);
}
document.getElementById("zIn").onclick  = function(){ pause(); zoomBy(1.4); };
document.getElementById("zOut").onclick = function(){ pause(); zoomBy(1 / 1.4); };
document.getElementById("zFit").onclick = function(){ pause(); resetView(); resetSide(); };
document.getElementById("zRst").onclick = function(){ pause(); setZoomAt(1 / fitScale); setZoomAt2(1 / fitScale); };

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
