#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_test.py —— 一条命令确认「这套东西装好了、能跑」。

用法：
  python3 tools/smoke_test.py            # 快速检查（几秒）
  python3 tools/smoke_test.py --render   # 再用一张小图真跑一遍（约 1 分钟）

检查项：
  1. 依赖是否齐全（numpy / Pillow / opencv）
  2. 所有脚本语法
  3. tools/ 与 skill/extras/ 的同名脚本是否一致（防止两边分叉）
  4. skill/ 是否完整、能否被 agent 识别、上游版权声明是否保留
  5. 文档相对链接、硬编码本机路径、残留缓存
  6. --render：真跑一遍完整流程并核对产物
"""
import os
import re
import sys
import glob
import ast
import hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK, FAIL, WARN = [], [], []


def ok(msg):
    OK.append(msg); print("  \033[1;32m✓\033[0m %s" % msg)


def bad(msg):
    FAIL.append(msg); print("  \033[1;31m✗\033[0m %s" % msg)


def warn(msg):
    WARN.append(msg); print("  \033[1;33m!\033[0m %s" % msg)


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def check_deps():
    print("\n[1/6] 依赖")
    for mod, pkg, attr in (("numpy", "numpy", "__version__"),
                           ("PIL", "Pillow", "__version__"),
                           ("cv2", "opencv-python-headless", "__version__")):
        try:
            m = __import__(mod)
            ok("%s %s" % (pkg, getattr(m, attr, "?")))
        except ImportError:
            bad("缺 %s —— 先跑 bash install.sh" % pkg)


def check_syntax():
    print("\n[2/6] 脚本语法")
    files = sorted(glob.glob(os.path.join(ROOT, "tools", "*.py")) +
                   glob.glob(os.path.join(ROOT, "skill", "extras", "*.py")) +
                   glob.glob(os.path.join(ROOT, "skill", "scripts", "*.py")) +
                   glob.glob(os.path.join(ROOT, "skill", "scripts", "css_art", "*.py")))
    n = 0
    for f in files:
        rel = os.path.relpath(f, ROOT)
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
            n += 1
        except SyntaxError as e:
            bad("%s 语法错误：%s" % (rel, e))
            continue
        # 顶层函数/类重复定义：语法合法，但后定义的会静默覆盖前面的。
        # 曾因此让 make_art.py 里两份 pick_background 互相覆盖、命令行直接跑不了（踩坑 #16）。
        seen = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen.setdefault(node.name, []).append(node.lineno)
        for name, lines in seen.items():
            if len(lines) > 1:
                bad("%s 里 %s() 定义了 %d 次（行 %s）—— 后一份会覆盖前面的"
                    % (rel, name, len(lines), "、".join(map(str, lines))))
    if n == len(files) and not FAIL:
        ok("%d 个脚本语法正常（含重复定义检查）" % n)


def check_no_drift():
    """tools/ 与 skill/extras/ 是同名同内容的两份，改一处必须同步另一处。"""
    print("\n[3/6] tools/ 与 skill/extras/ 是否同步")
    pairs = ["build_svg_art.py", "svg2canvas.py", "verify_svg.py"]
    drift = []
    for name in pairs:
        a = os.path.join(ROOT, "tools", name)
        b = os.path.join(ROOT, "skill", "extras", name)
        if not os.path.exists(a) or not os.path.exists(b):
            drift.append("%s（有一边不存在）" % name)
        elif digest(a) != digest(b):
            drift.append(name)
    if drift:
        for d in drift:
            bad("已分叉：%s —— 用 cp 同步到另一边" % d)
    else:
        ok("三个同名脚本内容一致（%s）" % "、".join(pairs))


def check_installed_skill():
    """如果本机装了 skill，检查它有没有落后于仓库里的 skill/。
    这一步专治「改了仓库忘了同步已安装副本」。"""
    print("\n[4b/6] 已安装的 skill 是否落后")
    installed = [os.path.expanduser("~/.claude/skills/image-to-css-art"),
                 os.path.expanduser("~/.agents/skills/image-to-css-art"),
                 "/skills/image-to-css-art"]
    found = [p for p in installed if os.path.isdir(p)]
    if not found:
        print("  （本机没装 skill，跳过）")
        return
    repo = os.path.join(ROOT, "skill")
    for inst in found:
        stale = []
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), repo)
                a = os.path.join(repo, rel)
                b = os.path.join(inst, rel)
                if not os.path.exists(b) or digest(a) != digest(b):
                    stale.append(rel)
        if stale:
            bad("已安装的 skill 落后 %d 个文件：%s" % (len(stale), inst))
            print("       同步：cp -r %s/. %s/" % (repo, inst))
        else:
            ok("已安装的 skill 与仓库一致（%s）" % inst)


def check_skill():
    print("\n[4/6] skill/ 完整性")
    skill = os.path.join(ROOT, "skill")
    need = ["SKILL.md", "LICENSE", "requirements.txt",
            "scripts/image_to_css.py", "scripts/css_art/regions.py",
            "scripts/css_art/render.py", "extras/build_svg_art.py",
            "extras/svg2canvas.py", "extras/verify_svg.py"]
    missing = [f for f in need if not os.path.exists(os.path.join(skill, f))]
    if missing:
        for m in missing:
            bad("skill/ 缺 %s" % m)
    else:
        ok("必要文件齐全（%d 个）" % len(need))

    md = open(os.path.join(skill, "SKILL.md"), encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---", md, re.S)
    if not m or "name:" not in m.group(1) or "description:" not in m.group(1):
        bad("SKILL.md 缺 frontmatter（name/description），agent 认不出来")
    else:
        ok("SKILL.md frontmatter 可被 agent 识别")

    lic = open(os.path.join(skill, "LICENSE"), encoding="utf-8").read()
    if "MIT" in lic and "AvroraCL" in lic:
        ok("上游 MIT 许可与版权声明保留完整")
    else:
        bad("skill/LICENSE 丢了上游版权声明（MIT 要求保留）")


def check_web():
    """网站（GitHub Pages）：文件齐不齐、worker 声明的清单与站点内容是否一致。"""
    print("\n[5b/6] 网站（web/）")
    web = os.path.join(ROOT, "web")
    need = ["index.html", "worker.js", "web_run.py"]
    missing = [f for f in need if not os.path.exists(os.path.join(web, f))]
    if missing:
        for m in missing:
            bad("web/ 缺 %s" % m)
        return

    try:
        ast.parse(open(os.path.join(web, "web_run.py"), encoding="utf-8").read())
    except SyntaxError as e:
        bad("web/web_run.py 语法错误：%s" % e)
        return

    # worker.js 里声明的 APP_FILES，必须都能在发布目录里找到
    src = open(os.path.join(web, "worker.js"), encoding="utf-8").read()
    m = re.search(r"const APP_FILES = \[(.*?)\];", src, re.S)
    if not m:
        bad("worker.js 里找不到 APP_FILES 清单")
        return
    files = re.findall(r"'([^']+)'", m.group(1))
    absent = []
    for rel in files:
        # 站点布局：web_run.py 在根，tools/ 与 scripts/ 由 workflow 从仓库拷
        if rel.startswith("tools/"):
            src_path = os.path.join(ROOT, rel)
        elif rel.startswith("scripts/"):
            src_path = os.path.join(ROOT, "skill", rel)
        else:
            src_path = os.path.join(web, rel)
        if not os.path.exists(src_path):
            absent.append("%s（找的是 %s）" % (rel, os.path.relpath(src_path, ROOT)))
    if absent:
        for a in absent:
            bad("worker.js 要的 %s 不存在 —— 发布会 404" % a)
    else:
        ok("worker.js 声明的 %d 个文件都在（含 tools/ 与 skill/scripts/）" % len(files))

    # 检查网页里引用的文件名
    idx = open(os.path.join(web, "index.html"), encoding="utf-8").read()
    if "worker.js" not in idx:
        bad("index.html 里没有引用 worker.js")
    else:
        ok("index.html 引用了 worker.js")
    if "pyodide" in src.lower():
        ok("worker.js 用 Pyodide 在浏览器里跑 Python")


def check_docs():
    print("\n[5/6] 文档链接与路径")
    bad_links = []
    for md in glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True):
        if "/.venv/" in md:
            continue
        base = os.path.dirname(md)
        text = open(md, encoding="utf-8").read()
        for m in re.finditer(r"\[([^\]]+)\]\(([^)#]+?)\)", text):
            link = m.group(2).strip()
            if link.startswith(("http", "mailto:", "#")):
                continue
            if not os.path.exists(os.path.normpath(os.path.join(base, link))):
                bad_links.append("%s → %s" % (os.path.relpath(md, ROOT), link))
    if bad_links:
        for b in bad_links:
            bad("失效链接：%s" % b)
    else:
        ok("所有相对链接有效")

    hits = []
    for f in glob.glob(os.path.join(ROOT, "tools", "*.py")) + \
             glob.glob(os.path.join(ROOT, "skill", "extras", "*.py")):
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            if "/workspace/" in line and "candidates" not in line:
                hits.append("%s:%d" % (os.path.relpath(f, ROOT), i))
    if hits:
        for h in hits:
            warn("可能硬编码了本机路径：%s" % h)
    else:
        ok("脚本里没有硬编码本机路径")

    caches = [c for c in glob.glob(os.path.join(ROOT, "**", "__pycache__"), recursive=True)
              if "/.venv/" not in c]
    if caches:
        warn("残留 __pycache__ %d 处（提交前清一下）" % len(caches))
    else:
        ok("无 __pycache__ 残留")

    if os.access(os.path.join(ROOT, "install.sh"), os.X_OK):
        ok("install.sh 可执行")
    else:
        warn("install.sh 没有可执行权限（chmod +x 一下）")


def check_render():
    print("\n[6/6] 真跑一遍完整流程")
    import subprocess, tempfile
    src = None
    for c in glob.glob(os.path.join(ROOT, "examples", "*.jpg")):
        if "photo-vs-svg" in c:
            src = c; break
    if not src:
        warn("examples/ 里没有可用作输入的照片，跳过实跑")
        return
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, SVG_ART_OUT=tmp)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_art.py"),
                            src, "smoke", "SMOKE", "10", "0", "0.5", "2.6"],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            bad("实跑失败：%s" % (r.stderr or r.stdout)[-300:]); return
        if not any("自检通过" in line for line in (r.stdout or "").splitlines()):
            bad("实跑没走到「画布尺寸自检」那一步"); return
        produced = glob.glob(os.path.join(tmp, "smoke", "*"))
        if len(produced) >= 3:
            ok("实跑成功，产出 %d 个文件" % len(produced))
        else:
            bad("实跑产物不全（只有 %d 个）" % len(produced))


def main():
    print("photo-to-svg 自检 —— %s" % ROOT)
    check_deps(); check_syntax(); check_no_drift(); check_skill()
    check_installed_skill(); check_web(); check_docs()
    if "--render" in sys.argv:
        check_render()
    else:
        print("\n[6/6] 真跑一遍完整流程 —— 跳过（加 --render 启用）")
    print("\n" + "-" * 56)
    if FAIL:
        print("\033[1;31m%d 项失败\033[0m，%d 项警告" % (len(FAIL), len(WARN)))
        for f in FAIL:
            print("  ✗ %s" % f)
        return 1
    print("\033[1;32m全部通过\033[0m（%d 项）%s"
          % (len(OK), "，%d 项警告" % len(WARN) if WARN else ""))
    for w in WARN:
        print("  ! %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
