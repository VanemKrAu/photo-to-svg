#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shot_web.py —— 给 web/ 界面拍一张「已选好图、等待生成」状态的截图，用来更新 README。

用法：
  python3 tools/shot_web.py web/index.html examples/screenshot-web.png
  python3 tools/shot_web.py web/index.html /tmp/shot.png --width 1440

为什么要单独写个脚本：
  README 顶部那张界面截图要能反映真实布局，手动截图容易漏掉状态（拖放区收没收起、
  按钮是不是可用色）。这个脚本把状态固定下来，重跑一次就能得到一样的图。

依赖（不在 requirements.txt 里，只有更新截图时才需要）：
  pip install playwright && playwright install chromium

注意：脚本会拦掉 worker.js，避免真的去下载 20 MB 的 Pyodide 运行时。
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import quote

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("需要 playwright：pip install playwright && playwright install chromium")


# 缩略图用内联 SVG 现画一张（竖构图，像张人像照片），不依赖仓库外的素材、
# 也不需要 Pillow —— 只有 playwright 一个依赖。
def fake_thumb() -> str:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="300" height="430">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">
    <stop offset="0" stop-color="#33405c"/><stop offset="0.62" stop-color="#6b6470"/>
    <stop offset="1" stop-color="#c99a63"/></linearGradient>
  <radialGradient id="glow">
    <stop offset="0" stop-color="#ffd9a0" stop-opacity="0.5"/>
    <stop offset="1" stop-color="#ffd9a0" stop-opacity="0"/></radialGradient>
</defs>
<rect width="300" height="430" fill="url(#bg)"/>
<circle cx="212" cy="92" r="78" fill="url(#glow)"/>
<circle cx="150" cy="170" r="71" fill="#e8cba6"/>
<path d="M30 430C46 320 96 286 150 286s104 34 120 144z" fill="#242938"/>
</svg>"""
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


# 把界面设成「已选好图 + 运行时就绪」：与真实交互后的 DOM 状态保持一致
READY = """(t) => {
  const warn = document.getElementById('bootWarn');
  if (warn) warn.style.display = 'none';                 // 运行时已就绪，提示条收起
  const th = document.getElementById('thumb');
  th.src = t; th.style.display = 'block';                // 缩略图
  const drop = document.getElementById('drop');
  if (drop) {
    drop.classList.add('hasfile');
    const big = drop.querySelector('.big');
    if (big) big.textContent = '点击或拖入可更换图片';
  }
  const go = document.getElementById('go');
  go.disabled = false;                                   // 按钮变成可用色
  const title = document.getElementById('title');
  if (title) title.value = '走廊人像';
}"""


async def run(page_path: str, out_path: str, width: int, height: int, full: bool):
    url = "file://" + os.path.abspath(page_path)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(viewport={"width": width, "height": height},
                                        device_scale_factor=1)
        page = await ctx.new_page()
        # 拦掉 Worker：截图不需要真的加载 Pyodide
        await page.route("**/worker.js", lambda r: asyncio.ensure_future(
            r.fulfill(status=200, content_type="application/javascript", body="/* blocked */")))
        await page.goto(url)
        await page.wait_for_timeout(400)
        await page.evaluate(READY, fake_thumb())
        await page.wait_for_timeout(400)
        await page.screenshot(path=out_path, full_page=full)
        print(f"已保存 {out_path}（视口 {width}×{height}，{'整页' if full else '首屏'}）")
        await ctx.close()
        await browser.close()


def main():
    ap = argparse.ArgumentParser(description="给 web/ 界面拍 README 截图")
    ap.add_argument("html", nargs="?", default="web/index.html", help="界面文件")
    ap.add_argument("out", nargs="?", default="examples/screenshot-web.png", help="输出 PNG")
    ap.add_argument("--width", type=int, default=1440, help="视口宽（默认 1440，即 PC 两栏布局）")
    ap.add_argument("--height", type=int, default=900, help="视口高")
    ap.add_argument("--viewport", action="store_true", help="只截首屏，默认截整页")
    args = ap.parse_args()
    asyncio.run(run(args.html, args.out, args.width, args.height, not args.viewport))


if __name__ == "__main__":
    main()
