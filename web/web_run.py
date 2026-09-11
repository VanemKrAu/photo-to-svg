#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_run.py —— 浏览器端（Pyodide）的流水线驱动。

跟 tools/make_art.py 做同一件事，但**不走 subprocess**（Pyodide 里没有子进程），
而是直接把 build_svg_art 和 svg2canvas 当模块 import 进来调用。

浏览器里的目录布局（由 GitHub Actions 在发布时拼好）：

    /app/web_run.py
    /app/tools/build_svg_art.py
    /app/tools/svg2canvas.py
    /app/scripts/css_art/*.py      <- 来自 skill/scripts，build_svg_art 靠上溯父目录找到它

用法（Pyodide 里）：
    import web_run
    web_run.run(json_string, emit_callback)
"""
import io
import json
import os
import re
import sys

APP = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(APP, "tools")
WORK = "/work"                      # 临时目录（Pyodide 的内存文件系统）


class _Tee(io.TextIOBase):
    """把 print 的每一行转发给 JS，同时留一份纯文本日志。

    注意 emit 期间要临时把 sys.stdout 换回真实 stdout：
    否则回调里只要出现一次 print（比如网页端调试、或 emit 内部报错打印），
    就会又走进本对象的 write()，造成无限嵌套。
    """

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""
        self.all = []
        self._inner = None      # 正在 emit 时，这里是真实 stdout

    def _forward(self, line):
        self.all.append(line)
        prev = sys.stdout
        sys.stdout = self._inner or sys.stdout
        try:
            self.emit(line)
        except Exception:
            pass
        finally:
            sys.stdout = prev

    def write(self, s):
        if self._inner is not None:      # emit 期间的输出直接放过，不再嵌套
            return self._inner.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._forward(line)
        return len(s)

    def flush(self):
        if self._inner is not None:
            return
        if self.buf and self.buf.strip():
            line, self.buf = self.buf.rstrip(), ""
            self._forward(line)


def _sub_progress(line):
    """从子脚本的输出里挤出细粒度进度（描摹到第几色）。"""
    m = re.search(r"描摹\s+(\d+)/(\d+)\s+色", line)
    if m:
        return int(m.group(1)) / max(1, int(m.group(2)))
    return None


def _add_strokes(svg_path):
    """给所有 <path> 补同色描边，消掉纯 SVG 版的白点。"""
    t = open(svg_path, encoding="utf-8").read()
    n = t.count("<path")
    t2 = re.sub(r'<path fill="([^"]+)" fill-rule="evenodd"',
                r'<path fill="\1" stroke="\1" fill-rule="evenodd"', t)
    if t2.count(' stroke="') != n:
        raise RuntimeError("描边补写异常：%d 个 path 只补上 %d 个"
                           % (n, t2.count(' stroke="')))
    style = ('<style>path{stroke-width:0.6;stroke-linejoin:round;'
             'stroke-linecap:round}</style>\n')
    i = t2.index("</title>") + len("</title>")
    open(svg_path, "w", encoding="utf-8").write(t2[:i] + "\n" + style + t2[i:])
    return n


def pick_background(photo):
    """选底色。占比 <5% 说明没有统一背景（插画），改用画面里不存在的中性色。"""
    import numpy as np
    from PIL import Image
    im = np.array(Image.open(photo).convert("RGB"))
    flat = im.reshape(-1, 3)
    q = flat // 8 * 8
    cols, cnt = np.unique(q, axis=0, return_counts=True)
    top = int(np.argmax(cnt))
    bg = cols[top]
    ratio = cnt[top] / len(flat)
    lum = 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]
    if ratio >= 0.05:
        return "#%02x%02x%02x" % tuple(int(v) for v in bg), (14 if lum < 60 else 0), ratio
    mean_lum = float(0.2126 * flat[:, 0].mean() + 0.7152 * flat[:, 1].mean()
                     + 0.0722 * flat[:, 2].mean())
    return ("#f0f0f0" if mean_lum >= 96 else "#101010"), 0, ratio


def preprocess(photo, mode, outdir):
    """可选预处理：小图 2x 放大、插画额外降噪。"""
    import cv2
    if mode == "none":
        return photo
    img = cv2.imread(photo, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError("读不了这张图——浏览器里的 OpenCV 对 WebP/HEIC 支持有限，请先转成 JPG 或 PNG")
    if img.shape[2] == 4:
        a = (img[:, :, 3].astype("float32") / 255)[..., None]
        img = (img[:, :, :3].astype("float32") * a + 255.0 * (1 - a)).astype("uint8")
    h, w = img.shape[:2]
    scale = (mode == "illust") or (mode == "auto" and w < 1200)
    if not scale:
        return photo
    img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    if mode == "illust":
        img = cv2.fastNlMeansDenoisingColored(img, None, 6, 6, 7, 21)
    out = os.path.join(outdir, "_prep.png")
    cv2.imwrite(out, img)
    return out


def run(cfg_json, emit):
    """cfg_json: JSON 字符串。emit: 收进度文本的回调。"""
    cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else cfg_json
    photo = cfg["input"]
    name = cfg.get("name") or "artwork"
    title = cfg.get("title") or (name + " · 逐笔绘制回放")
    duration = str(cfg.get("duration", 90))
    outline = str(cfg.get("outline", 0.15))
    eps = str(cfg.get("epsilon", 0.25))
    linew = str(cfg.get("linewidth", 2.6))
    maxw = int(cfg.get("max_width", 0))            # 0 = 不限制
    pre = cfg.get("preprocess", "auto")

    outdir = os.path.join(WORK, name)
    os.makedirs(outdir, exist_ok=True)
    svg = os.path.join(outdir, name + ".svg")
    html = os.path.join(outdir, name + ".html")

    def say(msg):
        try:
            emit(msg)
        except Exception:
            pass

    def prog(frac):
        try:
            emit(json.dumps({"_progress": max(0.0, min(1.0, float(frac)))}))
        except Exception:
            pass

    real_out = sys.stdout
    tee = _Tee(say)
    tee._inner = real_out          # emit 期间用它，避免回调里的 print 再次嵌套
    sys.stdout = tee
    try:
        prog(0.02)
        say("准备中…")

        src = photo
        if pre != "none":
            src = preprocess(photo, pre, outdir)
            if src != photo:
                say("预处理完成：%s" % os.path.basename(src))
        prog(0.05)

        bg, dark_cut, ratio = pick_background(src)
        say("底色 %s（高频色占比 %.1f%%）" % (bg, ratio * 100))

        from PIL import Image
        with Image.open(src) as im:
            w0, h0 = im.size
        if maxw and w0 > maxw:
            say("按上限缩到 %d 宽（原 %d）" % (maxw, w0))
        else:
            maxw = w0
        prog(0.08)

        say("［1/3］描摹 SVG —— 这一步最耗时")
        sys.path.insert(0, TOOLS)
        import build_svg_art
        sys.argv = [
            "build_svg_art.py", src,
            "--out-prefix", os.path.join(outdir, name),
            "--max-width", str(maxw), "--colors", "512", "--passes", "0",
            "--epsilon", eps, "--min-area", "1",
            "--background", bg, "--dark-cut", str(dark_cut),
            "--order", "sketch", "--no-ghost", "--title", title,
        ]
        build_svg_art.main()
        prog(0.62)

        say("［2/3］SVG 补描边（消白点）")
        n = _add_strokes(svg)
        say("已为 %d 个色块补描边" % n)
        prog(0.66)

        say("［3/3］生成 Canvas 回放页")
        import svg2canvas
        sys.argv = ["svg2canvas.py", svg, src, html, title,
                    duration, "0", outline, linew]
        svg2canvas.main()
        prog(0.97)

        sz_svg = os.path.getsize(svg)
        sz_html = os.path.getsize(html)
        head = open(svg, encoding="utf-8").read(1024)
        mv = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
        size = [int(float(mv.group(1))), int(float(mv.group(2)))] if mv else [w0, h0]
        strokes = sum(1 for _ in open(svg, encoding="utf-8") if _.startswith("<path"))
        prog(1.0)
        say("完成：%d 个色块，画布 %d×%d" % (strokes, size[0], size[1]))
        return json.dumps({
            "ok": True,
            "name": name,
            "svg": svg,
            "html": html,
            "svg_bytes": sz_svg,
            "html_bytes": sz_html,
            "strokes": strokes,
            "size": size,
            "log": "\n".join(tee.all[-40:]),
        })
    finally:
        sys.stdout = real_out
