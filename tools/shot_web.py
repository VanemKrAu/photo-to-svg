#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shot_web.py —— 给 web/ 界面拍一张 README 用的截图（新版取景器 UI）。

拍出来的状态，等价于「第二次打开这个网站、刚拖入一张照片」：
  - 运行时已就绪（替身 worker 回报「已缓存」，不发真实的 21 MB 下载）
  - 输入区已载入一张参考照片（从 examples/example-photo-vs-svg.jpg 左半裁出的
    「走廊人像」原照片，经页面内 canvas 转成 JPEG——不额外依赖 Pillow）
  - 生成历史里带 3 条示例记录（缩略图由同一张照片做色调变体，数据取自
    /workspace/图片转SVG 里的真实作品参数）
  - 舞台显示刚选中的照片，HUD / 引擎徽标 / 缓存文案全部落在就绪态

用法：
  python3 tools/shot_web.py web/index.html examples/screenshot-web-pc-v2.png
  python3 tools/shot_web.py                                # 全部用默认值（1440×1080）
  python3 tools/shot_web.py web/index.html /tmp/x.png --no-history

为什么要单独写个脚本：
  README 顶部那张界面截图要能反映真实布局，手动截图容易漏状态（图选没选、
  按钮是不是可用色、历史区空不空）。这个脚本把状态固定下来，重跑一次得到一样的图。

依赖（不在 requirements.txt 里，只有更新截图时才需要）：
  pip install playwright && playwright install chromium

实现要点：
  - 用本地 http server 而不是 file://：页面要 fetch('worker.js') 探测运行时，
    file:// 下这条路走不通，会退化到「演示模式」，拍出来就不是真实状态。
  - worker.js 换成替身：真的跑起来会下载 21 MB Pyodide，截图不需要；
    替身只回报 ready + 已缓存，页面就落到「第二次打开」的真实分支上。
"""

import argparse
import asyncio
import base64
import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("需要 playwright：pip install playwright && playwright install chromium")


# ---------------------------------------------------------------- 数据

# examples/example-photo-vs-svg.jpg 是一张「左：原照片 / 右：SVG 描摹」的对比图，
# 截图里当「待描摹的照片」用的就是左半边那半张——先按亮度找边界，再裁。
# （边界测量：竖缝在 x≈576，四角暗边约 8px，照片区 x∈[12,572] y∈[42,824]。）
REF_CROP = (12, 42, 572, 824)

# 生成历史的三条示例。strokes / dims / size 都取自 /workspace/图片转SVG 里
# 那几件真实作品的参数（走廊人像 172,471 笔、海边人像 182,640 笔……），
# 免得截图上出现一看就编的数字。tint 是缩略图的调色，让三张缩略图一眼可分。
HISTORY = [
    {"id": "shot-demo-1", "title": "走廊人像", "name": "走廊人像",
     "dims": "1440 × 2012", "strokes": 172471, "size": 45953778,
     "ago": 2 * 3600 * 1000, "tint": None},
    {"id": "shot-demo-2", "title": "夜景人像", "name": "夜景人像",
     "dims": "1440 × 1912", "strokes": 13255, "size": 9263700,
     "ago": 26 * 3600 * 1000, "tint": "brightness(0.62) saturate(1.25) hue-rotate(-15deg)"},
    {"id": "shot-demo-3", "title": "海边人像", "name": "海边人像",
     "dims": "1440 × 2162", "strokes": 182640, "size": 61579465,
     "ago": 3 * 24 * 3600 * 1000, "tint": "brightness(1.18) saturate(1.05) sepia(.08)"},
]

# 替身 worker：真 worker.js 一跑就要下载 21 MB Pyodide。截图只需要界面落到
# 「运行时已缓存 + 就绪」这个真实分支上，所以替身只回两条消息：
#   ready —— 页面收到后：引擎徽标转绿、按钮解锁、状态「就绪」
#   cacheinfo —— 页面问 cachesummary 时答「已缓存」，缓存文案落到「直接读取…」
FAKE_WORKER = """\
/* 截图专用替身（由 tools/shot_web.py 生成，不要提交这个文件）。 */
self.postMessage({ type: 'ready' });
self.addEventListener('message', (e) => {
  const m = e.data || {};
  if (m.type === 'cachesummary') {
    self.postMessage({ type: 'cacheinfo', count: 9, bytes: 22439526, supported: true });
  }
});
"""


# ---------------------------------------------------------------- 浏览器里跑的小脚本

# ① 从参考对比图里裁出照片，顺带做三张缩略图（色调变体）。
#    图片和页面同源（都在临时站点目录里），canvas 不会被跨域污染。
MAKE_PHOTO = """async ([ref, crop, tints]) => {
  const img = new Image();
  img.src = ref;
  await img.decode();
  const [x1, y1, x2, y2] = crop;
  const t = document.createElement('canvas');
  t.width = x2 - x1; t.height = y2 - y1;
  t.getContext('2d').drawImage(img, x1, y1, t.width, t.height, 0, 0, t.width, t.height);
  const photo = t.toDataURL('image/jpeg', 0.92);
  const thumbs = tints.map((f) => {
    const s = Math.min(128 / t.width, 128 / t.height, 1);   // 和页面里 thumbOf() 同一规则
    const c = document.createElement('canvas');
    c.width = Math.round(t.width * s); c.height = Math.round(t.height * s);
    const g = c.getContext('2d');
    g.filter = f || 'none';
    g.drawImage(t, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.72);
  });
  return { photo, thumbs };
}"""

# ② 把三条示例记录写进 IndexedDB（字段与页面 saveHistory() 存的完全一致）。
WRITE_HISTORY = """async (recs) => {
  const open = () => new Promise((res, rej) => {
    const r = indexedDB.open('photo-to-svg', 1);
    r.onupgradeneeded = () => {
      const d = r.result;
      if (!d.objectStoreNames.contains('records')) {
        d.createObjectStore('records', { keyPath: 'id' }).createIndex('at', 'at');
      }
    };
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  const db = await open();
  await new Promise((res, rej) => {
    const t = db.transaction('records', 'readwrite');
    const store = t.objectStore('records');
    for (const rec of recs) {
      store.put(Object.assign({}, rec, {
        at: Date.now() - rec.ago, duration: 90,
        svg: new Blob(['<svg xmlns="http://www.w3.org/2000/svg"/>'], { type: 'image/svg+xml' }),
        html: new Blob(['<!doctype html><title>demo</title>'], { type: 'text/html' }),
      }));
    }
    t.oncomplete = res; t.onerror = () => rej(t.error);
  });
  db.close();
}"""


# ---------------------------------------------------------------- 工具

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静态服务器，但不往控制台刷访问日志。"""
    def log_message(self, *args):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


async def run(args):
    html = os.path.abspath(args.html)
    if not os.path.isfile(html):
        sys.exit("找不到界面文件：" + html)
    root = os.path.dirname(os.path.dirname(html))          # web/ 的上一级 = 仓库根
    ref = os.path.abspath(args.ref) if args.ref else os.path.join(root, "examples", "example-photo-vs-svg.jpg")
    if not os.path.isfile(ref):
        sys.exit("找不到参考图：" + ref)

    # 拼一个和 GitHub Pages 同构的最小站点：index.html + worker.js（替身）+ 参考图
    site = tempfile.mkdtemp(prefix="shot-web-")
    shutil.copy(html, os.path.join(site, "index.html"))
    shutil.copy(ref, os.path.join(site, "src-ref.jpg"))
    with open(os.path.join(site, "worker.js"), "w", encoding="utf-8") as f:
        f.write(FAKE_WORKER)
    for icon in ("favicon.ico", "apple-touch-icon.png"):
        p = os.path.join(os.path.dirname(html), icon)
        if os.path.isfile(p):
            shutil.copy(p, os.path.join(site, icon))

    photo_path = os.path.join(site, "pick.jpg")            # 待「选入」的照片，落在临时目录
    httpd, port = serve(site)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = await browser.new_context(viewport={"width": args.width, "height": args.height},
                                            device_scale_factor=1)
            page = await ctx.new_page()
            await page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
            await page.wait_for_timeout(300)

            # ① 参考图 → 照片 JPEG + 缩略图
            made = await page.evaluate(MAKE_PHOTO, ["src-ref.jpg", list(REF_CROP),
                                                    [h["tint"] for h in HISTORY]])
            with open(photo_path, "wb") as f:
                f.write(base64.b64decode(made["photo"].split(",", 1)[1]))

            # ② 写历史记录 → 刷新页面，让页面的 renderHistory() 自然读出这些记录
            if not args.no_history:
                recs = [dict(h, thumb=made["thumbs"][i]) for i, h in enumerate(HISTORY)]
                await page.evaluate(WRITE_HISTORY, recs)
                await page.reload(wait_until="load")
                await page.wait_for_selector(".hitem")

            # ③ 等运行时落在「已就绪」：引擎徽标转绿 + 缓存文案出现
            await page.wait_for_function(
                "document.getElementById('engineText').textContent.indexOf('就绪') >= 0",
                timeout=15000)
            await page.wait_for_function(
                "document.getElementById('rtText').textContent.indexOf('缓存') >= 0",
                timeout=15000)

            # ④ 真实地「选」一张图：从文件输入框塞进去，走页面的 show(f) 全流程
            with open(photo_path, "rb") as f:
                data = f.read()
            await page.set_input_files("#pick", {"name": "走廊人像.jpg",
                                                 "mimeType": "image/jpeg", "buffer": data})
            await page.wait_for_function(
                "document.getElementById('srcDims').textContent.indexOf('×') >= 0",
                timeout=10000)
            await page.wait_for_timeout(500)               # 等布局稳定（图片 onload 后才算尺寸）

            await page.screenshot(path=args.out, full_page=True)
            print(f"已保存 {args.out}（视口 {args.width}×{args.height}）")
            await ctx.close()
            await browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(site, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="给 web/ 界面拍 README 截图")
    ap.add_argument("html", nargs="?", default="web/index.html", help="界面文件")
    ap.add_argument("out", nargs="?", default="examples/screenshot-web-pc-v2.png", help="输出 PNG")
    ap.add_argument("--width", type=int, default=1440, help="视口宽（默认 1440，即 PC 两栏布局）")
    ap.add_argument("--height", type=int, default=1080, help="视口高（900 时左栏要滚动，底部「运行时已缓存」那行会被藏住，所以取 1080）")
    ap.add_argument("--ref", default="", help="参考照片（默认 examples/example-photo-vs-svg.jpg）")
    ap.add_argument("--no-history", action="store_true", help="历史区留空，不塞示例记录")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
