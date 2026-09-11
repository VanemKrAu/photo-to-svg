#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""secret_scan.py —— 推送前的密钥自检（gitleaks 的轻量替代，无外部依赖）。

用法：
  python3 tools/secret_scan.py            # 扫描工作区（排除 .git）
  python3 tools/secret_scan.py --staged   # 只扫 git 暂存区（推荐配合 pre-commit）

命中任何一条 → 退出码 1，必须清理后再提交。
"""
import os
import re
import sys
import subprocess

PATTERNS = [
    ("GitHub Token",        r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub Fine-grained", r"github_pat_[A-Za-z0-9_]{60,}"),
    ("OpenAI / 通用 sk-",    r"\bsk-[A-Za-z0-9]{32,}"),
    ("Anthropic",           r"sk-ant-[A-Za-z0-9\-_]{32,}"),
    ("AWS Access Key",      r"\bAKIA[0-9A-Z]{16}\b"),
    ("Google API Key",      r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    ("Slack Token",         r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
    ("Stripe",              r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{24,}"),
    ("私钥块",               r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ("JWT",                 r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("数据库连接串",          r"(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s\"']{8,}"),
    ("含密码的 URL",          r"\bhttps?://[^\s/:@\"']+:[^\s/:@\"']{6,}@[^\s\"']+"),
    ("疑似赋值式密钥",         r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "output", ".idea"}
# 允许的占位符 / 示例值
ALLOW = re.compile(r"(?i)(xxx+|your[_-]?|<[^>]+>|example|placeholder|dummy|\.\.\.|REDACTED)")

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2",
              ".ttf", ".otf", ".zip", ".gz", ".tar", ".pdf", ".mp4", ".mp3", ".pyc"}


def scan_text(name, text, hits):
    for label, pat in PATTERNS:
        for m in re.finditer(pat, text):
            frag = m.group(0)
            if ALLOW.search(frag):
                continue
            line_no = text[:m.start()].count("\n") + 1
            hits.append((name, line_no, label, frag[:28] + ("…" if len(frag) > 28 else "")))


def scan_paths(paths):
    hits = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.splitext(f)[1].lower() in BINARY_EXT:
                        continue
                    try:
                        txt = open(fp, encoding="utf-8", errors="ignore").read()
                    except Exception:
                        continue
                    scan_text(fp, txt, hits)
        else:
            if os.path.splitext(p)[1].lower() in BINARY_EXT:
                continue
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            scan_text(p, txt, hits)
    return hits


def staged_files():
    r = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                       capture_output=True, text=True)
    return [f for f in (r.stdout or "").splitlines() if f.strip() and os.path.exists(f)]


def main():
    if "--staged" in sys.argv:
        files = staged_files()
        if not files:
            print("暂存区为空，跳过。")
            return 0
        targets = files
        print("扫描暂存区 %d 个文件…" % len(files))
    else:
        targets = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
        print("扫描工作区…")

    hits = scan_paths(targets)
    if hits:
        print("\n发现 %d 处疑似密钥：\n" % len(hits))
        for name, line, label, frag in hits:
            print("  [%s] %s:%s  %s" % (label, name, line, frag))
        print("\n请清理后再提交。误报可在 ALLOW 正则里加白名单。")
        return 1
    print("未发现疑似密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
