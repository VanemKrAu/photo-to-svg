# 换设备安装指南

在这个仓库里，同一套命令在 **Linux / macOS / Windows(WSL)** 上都能跑。
下面按系统给步骤。

---

## 0. 需要什么

| 必需 | 说明 |
|---|---|
| Python 3.9+ | Ubuntu 24.04 自带 3.12，实测通过 |
| numpy / Pillow / opencv-python-headless | `install.sh` 会装 |
| 约 2 GB 空闲磁盘 | 一张大图的 SVG 可能有 30~50 MB |

不需要：GPU、Node.js、任何图形界面。

---

## 1. 装 Python（若已有可跳过）

### Ubuntu / Debian

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv
```

> 如果系统 python 是"外部管理"的（Ubuntu 23.04+），`install.sh` 会自动建虚拟环境，
> 不需要手动加 `--break-system-packages`。

### macOS

```bash
brew install python
```

### Windows

建议用 **WSL2 + Ubuntu**（见上面 Ubuntu 步骤），比原生 Windows 省事。
若必须原生：

1. 从 [python.org](https://www.python.org/downloads/) 装 Python 3.11+
2. 安装时**勾选 "Add Python to PATH"**
3. 用 **PowerShell** 执行下面第 2 步

---

## 2. 克隆并安装

```bash
git clone https://github.com/VanemKrAu/photo-to-svg.git
cd photo-to-svg
bash install.sh
```

安装脚本会：

1. 检查 python3
2. 在项目下建 `.venv` 并装依赖（装不动才退回系统 python）
3. 自检三个依赖版本
4. 自检 `tools/` 下所有脚本语法
5. 打印用法

装完记下它打印的 python 路径，通常是 `.venv/bin/python`（下称 `$PY`）。

> **Windows PowerShell**
> ```powershell
> python -m venv .venv
> .venv\Scripts\python -m pip install -r requirements.txt
> ```
> 之后把下面的 `$PY` 换成 `.venv\Scripts\python`。

---

## 3. 验证能跑通

先跑自检（几秒）：

```bash
$PY tools/smoke_test.py
```

它会检查：依赖是否齐全、脚本语法（含顶层重复定义）、**`tools/` 与 `skill/extras/` 是否分叉**、
`skill/` 是否完整可识别、已安装的 skill 是否落后、网站（web/）文件清单、
文档链接与硬编码路径、是否有缓存残留。

想连"真跑一遍"也验证（约 40 秒，会用 `examples/` 里的图）：

```bash
$PY tools/smoke_test.py --render
```

全部通过会看到：

```
全部通过（16 项）
```

也可以自备一张照片跑完整流程：

```bash
$PY tools/make_art.py 你的照片.jpg 测试 "测试" 60 0 0.25 2.6
```

正常的话会看到：

```
描摹尺寸 720x1076   底板 #f0f0f0（固定纸白）  画面最高频色占比 10.9%  dark_cut 0
［1/3］描摹 SVG
［2/3］SVG 补描边（消白点）
［3/3］生成 Canvas 回放页（线稿层 + 扫描线切分 + 视觉重量时间轴）
   自检通过：画布尺寸 720x1076 三方一致
完成 → …/output/测试
  测试.svg   10.6 MB  （矢量原图，电脑上看）
  测试.html  4.7 MB  （Canvas 回放，手机/电脑都能开）
```

**看到「自检通过」才算成功**——那一步会核对 SVG 的 `viewBox` 和回放页里的
`<canvas>` 尺寸是否一致，不一致直接报错退出（这个错以前会导致线稿整体错位）。

## 4. 想让 AI 助手直接照做

把仓库放到 agent 的 skills 目录下，它下次读到 `AGENTS.md` 就会按规则执行：

```bash
# Claude Code
cp -r photo-to-svg ~/.claude/skills/

# 通用 skills 目录
cp -r photo-to-svg ~/.agents/skills/
```

`AGENTS.md` 里写了：什么话触发、怎么执行、哪些坑不能踩、
用户追加要求（"线稿细一点""手机很卡""出现白线"）分别怎么处理。

---

## 5. 常见问题

**Q：报 `ModuleNotFoundError: No module named 'cv2'`**
用 `install.sh` 打印的那个 python（通常是 `.venv/bin/python`），别用系统 `python3`。

**Q：`pip install` 很慢或超时**
换国内镜像：
```bash
$PY -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**Q：生成的 HTML 打开是空白**
文件很大（十几 MB），浏览器要解析几秒。等一等；或者把 SVG/HTML 下载到本地再开，
别在 GitHub 网页里点开。

**Q：想改输出目录**
```bash
export SVG_ART_OUT=/你的/目录
```

**Q：手机上太卡**
```bash
# 减少细节：跳过邻接合并 + 丢弃 6px 以下的碎片
$PY tools/make_art.py 照片.jpg 作品名 "标题" 90 0 0.5
```

**Q：内存不够（<4 GB）**
处理 1440×2162 的照片约需 1.5 GB 峰值内存。大图先降采样：
加 `--max-width 1000` 手动调用 `build_svg_art.py`（见 `docs/参数速查.md`）。

---

## 6. 不需要 git 的话

直接把整个目录拷过去（U 盘 / 网盘），然后从第 2 步的 `bash install.sh` 开始。
仓库不依赖 git 历史，脚本之间也不依赖绝对路径。
