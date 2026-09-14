#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_upgrade_fixtures.py —— 重新生成「历史回放页升级」测试的黄金样本。

背景：tests/fixtures/*.html 是四个代表世代的真实回放页，喂给 tools/test_upgrade.js
做升级回归测试。样本从 git 历史里「复活」：逐个 checkout 当年的 svg2canvas.py，
用同一张小图 + 小 SVG 跑出当年的产物。这样样本不是手搓的假货，而是真实世代形态。

四个代表样本（文件 → 来源 commit → 分工）：
  s1-29770ea.html   初版（金色 UI、老时间轴、无缩放）
                    —— 模糊规则全量跑一遍：AXIS_OLD 时间轴替换 + 42 条布局升级
  s4-eaa6ea5.html   双栏各自独立（相册式已就位、缺后续修复）
                    —— #18 的「世代跨度」场景（中间世代也要升得动）
  s6-v1.html        第一个带戳的模板（v1，描述框固定高）—— 489f2a6 生成
                    —— 迁移链的输入（v1→v2 描述框自动变高）
  s7-v2.html        当前模板（v2，描述框自动变高）—— 工作区生成
                    —— 验证「带戳最新产物零改动直通」

（更细的世代样本——s2 缩放初版 / s3 相册式初版 / s5 描述首版——2026-09-14 精简掉了：
断言上都有冗余覆盖，要恢复从 git 历史里 checkout 对应 commit 即可。）

跑完自己验一遍：python tools/make_upgrade_fixtures.py
（只重生成缺失的；--force 全部重来。改完 fixtures 记得跑 node tools/test_upgrade.js）
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
PY = sys.executable

# (输出名, commit, 分工)；commit=None 表示用当前工作区的脚本
SAMPLES = [
    ("s1-29770ea.html", "29770ea", "初版（模糊规则全量：老时间轴 + 老布局）"),
    ("s4-eaa6ea5.html", "eaa6ea5", "中间世代（相册式已就位、缺后续修复）"),
    ("s6-v1.html", "489f2a6", "第一个带戳的模板（v1，迁移链输入）"),
    ("s7-v2.html", None, "当前模板（v2）—— 直通验证"),
]


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print((r.stdout or "")[-500:])
        print((r.stderr or "")[-800:])
        raise SystemExit("命令失败：%s" % " ".join(cmd))
    return r


def git_show(commit, rel):
    r = run(["git", "-C", ROOT, "show", "%s:%s" % (commit, rel)])
    return r.stdout


def main():
    force = "--force" in sys.argv
    todo = []
    for name, commit, note in SAMPLES:
        p = os.path.join(FIX, name)
        if force or not os.path.exists(p):
            todo.append((name, commit, note))
    if not todo:
        print("样本都在（要重来加 --force）。")
        return
    os.makedirs(FIX, exist_ok=True)
    print("要生成 %d 个：%s" % (len(todo), "、".join(n for n, _, _ in todo)))

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1) 小图：examples 例图裁一半、缩到 220 宽（样本只求世代形态，不求大） ──
        from PIL import Image
        im = Image.open(os.path.join(ROOT, "examples", "example-photo-vs-svg.jpg"))
        w, h = im.size
        small = im.crop((0, 0, w // 2, h)).convert("RGB")
        small.thumbnail((220, 2200), Image.LANCZOS)
        photo = os.path.join(tmp, "small.jpg")
        small.save(photo, quality=88)
        print("小图 %dx%d" % small.size)

        # ── 2) 小 SVG：当前 build_svg_art 生成 + 补描边（对齐 make_art 第 2 步） ──
        svg = os.path.join(tmp, "small.svg")
        run([PY, os.path.join(ROOT, "tools", "build_svg_art.py"), photo,
             "--out-prefix", os.path.join(tmp, "small"), "--max-width", "220",
             "--colors", "48", "--passes", "0", "--epsilon", "0.8", "--min-area", "1",
             "--background", "#f0f0f0", "--dark-cut", "0", "--order", "area",
             "--no-ghost", "--title", "测试样本"])
        t = open(svg, encoding="utf-8").read()
        n = t.count("<path")
        t2 = re.sub(r'<path fill="([^"]+)" fill-rule="evenodd"',
                    r'<path fill="\1" stroke="\1" fill-rule="evenodd"', t)
        if t2.count(' stroke="') != n:
            raise SystemExit("补描边异常：%d != %d" % (t2.count(' stroke="'), n))
        style = '<style>path{stroke-width:0.6;stroke-linejoin:round;stroke-linecap:round}</style>\n'
        i = t2.index("</title>") + len("</title>")
        open(svg, "w", encoding="utf-8").write(t2[:i] + "\n" + style + t2[i:])
        print("小 SVG：%d 个色块" % n)

        # ── 3) 逐世代跑 svg2canvas 出样本 ──
        #    参数与当初生成时一致：初版不传第 7/8 位（走它自己的默认）；
        #    其余世代传 st=0（不单独控制线稿时间）+ 线宽 2.6。
        for name, commit, note in todo:
            script = os.path.join(tmp, "gen_%s.py" % (commit or "head"))
            if commit:
                open(script, "w", encoding="utf-8").write(
                    git_show(commit, "tools/svg2canvas.py"))
            else:
                script = os.path.join(ROOT, "tools", "svg2canvas.py")
            out = os.path.join(tmp, name)
            args = [PY, script, svg, photo, out, "测试样本", "30", "0"]
            if commit != "29770ea":
                args += ["0", "2.6"]
            run(args)
            dst = os.path.join(FIX, name)
            open(dst, "wb").write(open(out, "rb").read())
            sz = os.path.getsize(dst) / 1024
            print("  %s  ← %s（%s）  %.0f KB" % (name, commit or "工作区",
                                                 note, sz))

    print("\n完成 → %s" % FIX)
    print("跑一遍验证：node tools/test_upgrade.js")


if __name__ == "__main__":
    main()
