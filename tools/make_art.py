def pick_background(photo):
    """选底色。

    底色的作用是「哪些颜色可以整簇跳过不画」。选错的后果很严重：
      * 选到画面里存在的颜色   -> 那片区域被挖空，露出底色（在深色区就是白点）
      * 选到画面里不存在的颜色 -> 安全，底板只在缝隙里露一点点

    所以策略是：高频色必须**明显**高频（>12%）才敢用；否则一律用画面里没有的中性色。
    实测反例：一张海边照最高频色 #686868 占 5.94%，恰好越过旧的 5% 阈值，
    而 #686868 正是画面里的阴影灰 -> 人物和沙滩的阴影被整簇挖掉，满图白点。
    """
    im = np.array(Image.open(photo).convert("RGB"))
    flat = im.reshape(-1, 3)
    q = flat // 8 * 8
    cols, cnt = np.unique(q, axis=0, return_counts=True)
    top = int(np.argmax(cnt))
    bg = cols[top]
    ratio = cnt[top] / len(flat)
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]

    mean_lum = float(0.2126 * flat[:, 0].mean() + 0.7152 * flat[:, 1].mean()
                     + 0.0722 * flat[:, 2].mean())
    neutral = "#f0f0f0" if mean_lum >= 96 else "#101010"

    if ratio >= 0.12:
        hexbg = "#%02x%02x%02x" % tuple(int(v) for v in bg)
        return hexbg, (14 if lum < 60 else 0), ratio

    if ratio >= 0.05:
        pass      # 旧阈值下会误用画面里的颜色，这里降级为中性色（见上面的反例）
    return neutral, 0, ratio


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_art.py —— 一条命令跑完「照片 → 描摹 SVG → Canvas 逐笔回放页」。

把这一路踩过的坑全部固化在流程里，不需要再记住任何参数：

  1. 描摹：零妥协参数（colors 512 / passes 0 / min_area 1），背景色自动取原图高频色
  2. 去掉纯 SVG 版的「白点」：给所有 <path> 补同色 stroke + stroke-width 0.6
  3. 生成 Canvas 回放页：真线稿层（Canny 轮廓）+ 大色块扫描线切分 + 按视觉重量分配时间
  4. 写一份.json 参数报告

用法：
  python3 tools/make_art.py <原图> <作品名> [标题] [秒数] [线稿时间占比] [epsilon] [线宽]
                            [--preprocess=none|auto|illust]

  秒数          1× 速度下播完整幅画的时间，默认 90
  线稿时间占比  起稿阶段吃掉多少播出时间，默认 0.15
  epsilon       轮廓简化容差，只影响顶点数不影响形状数，默认 0.25
  线宽          线稿线条宽度（画布原生像素），默认 2.6

  --preprocess=illust   插画/动漫图：2x 放大 + NL-Means 降噪（平坦区更干净）
  --preprocess=auto     宽度 < 1200 时自动 2x 放大（小图边缘更顺）
  --preprocess=none     原样描摹（默认）

例：
  python3 tools/make_art.py 照片.jpg 海边人像 "海边人像 · 逐笔绘制回放" 90 0.15 0.25 2.6
  python3 tools/make_art.py 插画.png 浴室少女 "浴室少女" 90 0.15 0.25 2.2 --preprocess=illust
"""
import os
import re
import sys
import json
import subprocess
import numpy as np
from PIL import Image

# 路径全部从脚本自身位置推导，换台设备/换目录都不用改代码：
#   本文件在 <项目>/tools/make_art.py  →  TOOLS=<项目>/tools, OUT_ROOT=<项目>/output
TOOLS = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.environ.get("SVG_ART_OUT") or os.path.join(os.path.dirname(TOOLS), "output")


def preprocess(photo, mode, workdir):
    """可选预处理。返回要描摹的图路径。

    * none  —— 原样描摹
    * auto  —— 宽度 < 1200 时 2x LANCZOS 放大（小图边缘容易毛，放大后轮廓更顺）
    * illust —— 2x LANCZOS 放大 + NL-Means 降噪（插画/动漫图：压掉 JPEG 噪点，
                平坦区明显变干净；实测某插画平坦区 48.8% → 65.8%）
    """
    import cv2
    img = cv2.imread(photo, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit("读不了这张图：%s" % photo)
    if img.shape[2] == 4:                     # 带 alpha：先合成到白底
        a = (img[:, :, 3].astype(np.float32) / 255)[..., None]
        img = (img[:, :, :3].astype(np.float32) * a + 255.0 * (1 - a)).astype(np.uint8)
    h, w = img.shape[:2]

    do_scale = (mode == "illust") or (mode == "auto" and w < 1200)
    if not do_scale and mode != "illust":
        return photo, None

    if do_scale:
        img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    if mode == "illust":
        img = cv2.fastNlMeansDenoisingColored(img, None, 6, 6, 7, 21)

    out = os.path.join(workdir, "_prep.png")
    cv2.imwrite(out, img)
    return out, "%s → %dx%d" % (mode, img.shape[1], img.shape[0])


def pick_background(photo):
    """选底色。这个值会决定「哪些颜色整簇被跳过」，选错会让一片区域直接消失。

    * 照片（有统一背景，如天空/沙滩）：底色 = 最高频色，占比通常 >5%，安全。
    * 插画（没有统一背景）：最高频色可能只占 1%，用它作底会挖掉画面内容。
      实测某插画：底取高频色 → SSIM 0.9060；换用画面中不存在的浅色 → 0.9276。
      所以占比过低时改用「画面中不存在的中性色」，靠同色描边把缝隙填住。
    """
    im = np.array(Image.open(photo).convert("RGB"))
    flat = im.reshape(-1, 3)
    q = (flat // 8 * 8)
    cols, cnt = np.unique(q, axis=0, return_counts=True)
    top = int(np.argmax(cnt))
    bg = cols[top]
    ratio = cnt[top] / len(flat)
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]

    if ratio >= 0.05:                       # 有统一背景，照常用
        hexbg = "#%02x%02x%02x" % tuple(int(v) for v in bg)
        return hexbg, (14 if lum < 60 else 0), int(im.shape[1]), int(im.shape[0])

    # 没有统一背景：选一个与画面最不接近的中性色
    mean_lum = float(0.2126 * flat[:, 0].mean() + 0.7152 * flat[:, 1].mean()
                     + 0.0722 * flat[:, 2].mean())
    cand = "#f0f0f0" if mean_lum >= 96 else "#101010"
    print("底色：最高频色只占 %.1f%%（无统一背景）→ 改用 %s 作底，避免挖掉画面内容"
          % (ratio * 100, cand))
    return cand, 0, int(im.shape[1]), int(im.shape[0])


def run(cmd, **kw):
    print("» " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    tail = (r.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("   " + line)
    if r.returncode != 0:
        print((r.stderr or "")[-800:])
        raise SystemExit("命令失败：%s" % cmd[1])


def add_strokes(svg_path):
    """纯 SVG 版的色块没有描边，相邻块之间的发丝缝会露出底色形成白点。
    给每个 <path> 补同色 stroke，并在 <svg> 里统一给 stroke-width。"""
    t = open(svg_path, encoding="utf-8").read()
    n = t.count("<path")
    t2 = re.sub(r'<path fill="([^"]+)" fill-rule="evenodd"',
                r'<path fill="\1" stroke="\1" fill-rule="evenodd"', t)
    if t2.count(' stroke="') == n:
        style = '<style>path{stroke-width:0.6;stroke-linejoin:round;stroke-linecap:round}</style>\n'
        i = t2.index("</title>") + len("</title>")
        t2 = t2[:i] + "\n" + style + t2[i:]
        open(svg_path, "w", encoding="utf-8").write(t2)
        print("   已为 %d 个色块补描边（消白点），%.1f MB → %.1f MB"
              % (n, len(t) / 1048576, len(t2) / 1048576))
    else:
        print("   ！描边补写异常，跳过")


def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    pre = "none"
    for f in flags:
        if f.startswith("--preprocess="):
            pre = f.split("=", 1)[1]
    if pre not in ("none", "auto", "illust"):
        raise SystemExit("--preprocess 只能是 none / auto / illust")
    if len(argv) < 2:
        print(__doc__)
        return
    photo = os.path.abspath(argv[0])
    name = argv[1]
    title = argv[2] if len(argv) > 2 else (name + " · 逐笔绘制回放")
    dur = argv[3] if len(argv) > 3 else "90"
    outl = argv[4] if len(argv) > 4 else "0.15"
    eps = argv[5] if len(argv) > 5 else "0.25"
    linew = argv[6] if len(argv) > 6 else "2.6"

    outdir = os.path.join(OUT_ROOT, name)
    os.makedirs(outdir, exist_ok=True)
    svg = os.path.join(outdir, name + ".svg")
    html = os.path.join(outdir, name + ".html")

    if pre != "none":
        photo, note = preprocess(photo, pre, outdir)
        print("预处理：%s" % note)

    bg, dark_cut, ratio = pick_background(photo)
    from PIL import Image as _I
    with _I.open(photo) as _im:
        W, H = _im.size
    print("描摹尺寸 %dx%d   底色 %s（高频色占比 %.1f%%，dark_cut %d）"
          % (W, H, bg, ratio * 100, dark_cut))

    print("\n［1/3］描摹 SVG")
    run([sys.executable, os.path.join(TOOLS, "build_svg_art.py"), photo,
         "--out-prefix", os.path.join(outdir, name),
         "--max-width", str(W), "--colors", "512", "--passes", "0",
         "--epsilon", eps, "--min-area", "1",
         "--background", bg, "--dark-cut", str(dark_cut),
         "--order", "sketch", "--no-ghost", "--title", title])

    print("\n［2/3］SVG 补描边（消白点）")
    add_strokes(svg)

    print("\n［3/3］生成 Canvas 回放页（线稿层 + 扫描线切分 + 视觉重量时间轴）")
    run([sys.executable, os.path.join(TOOLS, "svg2canvas.py"), svg, photo,
         html, title, dur, "0", outl, linew])

    # 自检：回放页的画布尺寸必须和 SVG 的 viewBox 一致，
    # 否则线稿层会按错误尺寸提边缘（整体错位），页面显示也会被拉伸。
    def scan(path, pattern, limit=4 << 20):
        """流式查找：回放页里 --ghostimg 的原图 base64 有两三百 KB，
        直接 read(N) 很容易读不到后面的 <canvas>。"""
        buf = ""
        with open(path, encoding="utf-8") as fh:
            while len(buf) < limit:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                m = re.search(pattern, buf)
                if m:
                    return m
        return None

    mv = scan(svg, r'viewBox="0 0 ([\d.]+) ([\d.]+)"', 1 << 20)
    mh = scan(html, r'<canvas id="art" width="(\d+)" height="(\d+)"')
    if mv and mh:
        sw, sh = int(float(mv.group(1))), int(float(mv.group(2)))
        hw, hh = int(mh.group(1)), int(mh.group(2))
        if (sw, sh) != (hw, hh):
            raise SystemExit("自检失败：SVG 画布 %dx%d 与回放页 %dx%d 不一致" % (sw, sh, hw, hh))
        print("   自检通过：画布尺寸 %dx%d 三方一致" % (sw, sh))
    else:
        raise SystemExit("自检失败：没能从 SVG / HTML 里读到画布尺寸")

    report = {
        "input": photo, "size": [W, H],
        "svg": {"file": os.path.basename(svg), "bytes": os.path.getsize(svg),
                "bytes_mb": round(os.path.getsize(svg) / 1048576, 2)},
        "html": {"file": os.path.basename(html), "bytes": os.path.getsize(html),
                 "bytes_mb": round(os.path.getsize(html) / 1048576, 2)},
        "settings": {"colors": 512, "passes": 0, "epsilon": float(eps), "min_area": 1,
                     "background": bg, "dark_cut": dark_cut,
                     "outline_time_share": float(outl), "duration_1x": float(dur),
                     "outline_width": float(linew)},
    }
    json.dump(report, open(os.path.join(outdir, name + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("\n完成 → %s" % outdir)
    print("  %s.svg   %.1f MB  （矢量原图，电脑上看）" % (name, report["svg"]["bytes_mb"]))
    print("  %s.html  %.1f MB  （Canvas 回放，手机/电脑都能开）" % (name, report["html"]["bytes_mb"]))


if __name__ == "__main__":
    main()
