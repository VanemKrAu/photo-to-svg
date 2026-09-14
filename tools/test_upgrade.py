#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_upgrade.py —— 历史回放页升级测试（Python 入口），跑两段：

  ① 逻辑断言（必须）：node tools/test_upgrade.js —— 对六个黄金样本断言
     版本戳 / 幂等 / 防重复哨兵 / 逐样本特性 / 迁移链机制。
  ② 浏览器验证（可选，需 playwright）：把样本升级后的页面在 Chromium 里逐个打开，
     断言零报错、canvas 真的画出了内容 —— 升级是「文本手术」，最终得在真浏览器里
     站得住（改坏一处 JS，逻辑断言看不出来）。

用法：python3 tools/test_upgrade.py [--no-browser]
退出码：0 = 通过（含「明确跳过」，跳过原因会打印）；1 = 有失败。
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def run_node_tests():
    print("=" * 60)
    print("① 逻辑断言：node tools/test_upgrade.js")
    print("=" * 60)
    if not NODE:
        print("跳过：本机没有 node（装一个才能跑升级测试）。")
        return None
    r = sh(NODE, os.path.join(ROOT, "tools", "test_upgrade.js"), cwd=ROOT)
    print(r.stdout, end="")
    if r.stderr.strip():
        print("[stderr]", r.stderr[-500:])
    return r.returncode == 0


UPGRADE_ALL_JS = r"""
const U = require(process.env.PROJ + '/web/upgrade.js');
const fs = require('fs');
const path = require('path');
const fix = path.join(process.env.PROJ, 'tests', 'fixtures');
for (const f of fs.readdirSync(fix)) {
  if (!f.endsWith('.html')) continue;
  const t = fs.readFileSync(path.join(fix, f), 'utf8');
  fs.writeFileSync(path.join(process.env.UP_OUT, f), U.upgradeReplay(t, null).text);
}
console.log('upgraded');
"""

CHECK_JS = """() => {
  const cv = document.getElementById('art');
  if (!cv) return { canvas: false };
  const ctx = cv.getContext('2d');
  const d = ctx.getImageData(0, 0, Math.min(cv.width, 96), Math.min(cv.height, 96)).data;
  let opaque = 0, varied = 0;
  const r0 = d[0], g0 = d[1], b0 = d[2];
  for (let i = 0; i < d.length; i += 4) {
    if (d[i + 3] > 0) opaque++;
    if (Math.abs(d[i] - r0) > 10 || Math.abs(d[i + 1] - g0) > 10 || Math.abs(d[i + 2] - b0) > 10) varied++;
  }
  return { canvas: true, opaque: opaque, varied: varied };
}"""


def run_browser_checks():
    print()
    print("=" * 60)
    print("② 浏览器验证：升级后的页面在 Chromium 里打开")
    print("=" * 60)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("跳过：本机没有 playwright（pip install playwright && playwright install chromium）。")
        return None
    if not NODE:
        print("跳过：生成升级样本需要 node。")
        return None

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PROJ=ROOT, UP_OUT=tmp)
        r = sh(NODE, "-e", UPGRADE_ALL_JS, cwd=ROOT, env=env)
        if r.returncode != 0 or "upgraded" not in r.stdout:
            print("生成升级样本失败：")
            print((r.stderr or r.stdout)[-600:])
            return False

        files = sorted(f for f in os.listdir(tmp) if f.endswith(".html"))
        fails = 0
        with sync_playwright() as p:
            b = p.chromium.launch()
            for f in files:
                pg = b.new_page(viewport={"width": 900, "height": 700})
                errs = []
                pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
                pg.on("pageerror", lambda e: errs.append("PAGEERROR: " + str(e)))
                pg.goto("file://" + os.path.join(tmp, f))
                pg.wait_for_timeout(1200)
                pg.keyboard.press("Space")      # 播放，逼出绘制路径上的错误
                pg.wait_for_timeout(1600)
                info = pg.evaluate(CHECK_JS)
                pg.close()
                okk = (not errs) and info.get("canvas") and info.get("varied", 0) > 100
                mark = "✓" if okk else "✗"
                print("  %s %-22s 报错=%d 绘制内容像素=%s" % (
                    mark, f, len(errs), info.get("varied")))
                for e in errs[:3]:
                    print("      ERR:", e[:160])
                if not okk:
                    fails += 1
            b.close()
        return fails == 0


def main():
    print("历史回放页升级测试 —— %s" % ROOT)
    r1 = run_node_tests()
    r2 = None if "--no-browser" in sys.argv else run_browser_checks()

    print()
    print("-" * 60)
    bad = (r1 is False) or (r2 is False)
    if bad:
        print("失败：" + "、".join(
            n for n, v in (("逻辑断言", r1), ("浏览器验证", r2)) if v is False))
        return 1
    print("通过" + ("（浏览器部分跳过）" if r2 is None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
