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
  python3 工具/make_art.py <原图> <作品名> [标题] [1×秒数] [线稿时间占比] [epsilon]

例：
  python3 工具/make_art.py /upload/xxx.jpg 海边人像 "海边人像 · 逐笔绘制回放" 90 0.22
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


def pick_background(photo):
    """取原图高频色当底色；同时判断是不是暗背景图。"""
    im = np.array(Image.open(photo).convert("RGB"))
    q = (im.reshape(-1, 3) // 8 * 8)
    cols, cnt = np.unique(q, axis=0, return_counts=True)
    bg = cols[int(np.argmax(cnt))]
    hexbg = "#%02x%02x%02x" % tuple(int(v) for v in bg)
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
    return hexbg, (14 if lum < 60 else 0), int(im.shape[1]), int(im.shape[0])


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
    if len(sys.argv) < 3:
        print(__doc__)
        return
    photo = os.path.abspath(sys.argv[1])
    name = sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else (name + " · 逐笔绘制回放")
    dur = sys.argv[4] if len(sys.argv) > 4 else "90"
    outl = sys.argv[5] if len(sys.argv) > 5 else "0.15"
    eps = sys.argv[6] if len(sys.argv) > 6 else "0.25"
    linew = sys.argv[7] if len(sys.argv) > 7 else "2.6"

    outdir = os.path.join(OUT_ROOT, name)
    os.makedirs(outdir, exist_ok=True)
    svg = os.path.join(outdir, name + ".svg")
    html = os.path.join(outdir, name + ".html")

    bg, dark_cut, W, H = pick_background(photo)
    print("原图 %dx%d   底色 %s（dark_cut %d）" % (W, H, bg, dark_cut))

    print("\n［1/3］描摹 SVG")
    run(["python3", os.path.join(TOOLS, "build_svg_art.py"), photo,
         "--out-prefix", os.path.join(outdir, name),
         "--max-width", str(W), "--colors", "512", "--passes", "0",
         "--epsilon", eps, "--min-area", "1",
         "--background", bg, "--dark-cut", str(dark_cut),
         "--order", "sketch", "--no-ghost", "--title", title])

    print("\n［2/3］SVG 补描边（消白点）")
    add_strokes(svg)

    print("\n［3/3］生成 Canvas 回放页（线稿层 + 扫描线切分 + 视觉重量时间轴）")
    run(["python3", os.path.join(TOOLS, "svg2canvas.py"), svg, photo,
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
