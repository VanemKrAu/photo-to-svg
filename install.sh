#!/usr/bin/env bash
# 一键安装：检查依赖 → 安装 Python 包 → 自检 → 打印用法
#
#   bash install.sh              # 装到当前目录（默认）
#   bash install.sh --system     # 依赖用系统 python 装（不加 --user）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_FLAG="--user"
[[ "${1:-}" == "--system" ]] && USER_FLAG=""

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
bad() { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; }

say "1/4  检查 Python"
if ! command -v python3 >/dev/null; then
  bad "找不到 python3，请先安装 Python 3.9+"
  exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
ok "python3 $PYV"

say "2/4  安装依赖（numpy / Pillow / opencv-python-headless）"

# pip 安装包装：
#   * 兼容 Ubuntu 23.04+ 的 PEP 668「外部管理环境」保护
#   * 参数用数组拼，避免空字符串被当成参数传给 pip
pip_install() {
  local py="$1" want_user="${2:-}"
  local args=(-q)
  [[ "$want_user" == "--user" ]] && args+=(--user)
  args+=(-r "$HERE/requirements.txt")
  if "$py" -m pip install "${args[@]}" 2>/tmp/_pip_err; then
    return 0
  fi
  if grep -q "externally-managed-environment" /tmp/_pip_err; then
    say "     系统 python 受 PEP 668 保护，改用 --user --break-system-packages"
    "$py" -m pip install -q --user --break-system-packages -r "$HERE/requirements.txt"
    return $?
  fi
  cat /tmp/_pip_err
  return 1
}

PY=""
if [[ -z "${VIRTUAL_ENV:-}" && -z "${SKIP_VENV:-}" ]]; then
  if [[ ! -x "$HERE/.venv/bin/python" ]]; then
    say "     创建虚拟环境 .venv"
    python3 -m venv "$HERE/.venv" >/dev/null 2>&1 || true
  fi
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    "$HERE/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    if pip_install "$HERE/.venv/bin/python"; then
      ok "已装进 $HERE/.venv"
      PY="$HERE/.venv/bin/python"
    fi
  fi
fi

if [[ -z "$PY" ]]; then
  # venv 建不起来（缺 python3-venv）或显式 SKIP_VENV：退回系统 python
  [[ -z "${SKIP_VENV:-}" ]] && say "     虚拟环境不可用（可能缺 python3-venv），退回系统 python"
  pip_install python3 "$USER_FLAG"
  ok "已装进系统 python"
  PY="python3"
  [[ -z "${SKIP_VENV:-}" ]] && say "     提示：装 python3-venv 后重跑本脚本可获得更干净的环境"
fi

say "3/4  依赖自检"
"$PY" - <<'EOF'
import sys
try:
    import numpy, PIL, cv2
    print("  ✓ numpy %s / Pillow %s / opencv %s" % (numpy.__version__, PIL.__version__, cv2.__version__))
except Exception as e:
    print("  ✗ 依赖缺失：%s" % e); sys.exit(1)
EOF

say "4/4  工具自检"
"$PY" -c "import ast,glob,sys
for f in glob.glob('$HERE/tools/*.py'):
    ast.parse(open(f,encoding='utf-8').read())
print('  ✓ tools/ 下 %d 个脚本语法正常' % len(glob.glob('$HERE/tools/*.py')))"

cat <<EOF

────────────────────────────────────────────────────────
安装完成。以后这样用：

  $PY $HERE/tools/make_art.py <原图> <作品名> "标题" 90 0.15 0.25 2.6

产物落到  $HERE/output/<作品名>/  下：
  <名>.svg   矢量原图（电脑上看，几十 MB）
  <名>.html  Canvas 逐笔回放页（手机/电脑都能开，十几 MB）
  <名>.json  本次参数报告

参数含义：秒数 / 线稿时间占比 / 轮廓精度 epsilon / 线稿线宽
想改输出位置：export SVG_ART_OUT=/你的/目录

没装 git 也想让它当 skill 用？把整个目录放到 agent 的 skills 里即可
（例如 ~/.claude/skills/photo-to-svg/ 或 ~/.agents/skills/photo-to-svg/）。
────────────────────────────────────────────────────────
EOF
