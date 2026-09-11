# AGENTS.md —— 给 AI 助手的操作规则

> 这份文件是给**在这台设备上工作的 AI 助手**读的。用户说「把这张图转成 SVG」「描摹一下」
> 「像上次那样处理」时，**不要反问、不要讨论方案、不要手工分步**，直接按下面的流程执行。
> 这些规则是踩了十几个坑换来的，逐条都有代价，别自作聪明跳过。

---

## 关于这个仓库

本项目建立在 **[AvroraCL/image-to-css-art](https://github.com/AvroraCL/image-to-css-art)**（MIT）之上：

- `skill/` = 上游 skill 的**原样副本**（含 `LICENSE`），可直接装进 agent 的 skills 目录
- `skill/extras/` = 本项目新增的 SVG 输出扩展
- `tools/` = 本项目的完整流水线（**日常只用它**）
- 上游原版输出的是 CSS `clip-path`；本项目输出真正的 SVG + Canvas 回放页

出处与许可详见 [`ATTRIBUTION.md`](ATTRIBUTION.md)。

---

## 0. 环境自检（第一次用）

```bash
bash install.sh          # 装依赖 + 自检，约 1 分钟
```

装完记下打印出来的 python 路径（通常是 `<项目>/.venv/bin/python`），下面用 `$PY` 指代。

自检（可选，但换设备后建议跑一次）：

```bash
$PY tools/smoke_test.py            # 几秒，查依赖/语法/同步/链接
$PY tools/smoke_test.py --render   # 再真跑一张小图，约 40 秒
```

> **改过 `tools/` 里任何脚本后**，务必跑一次 `smoke_test.py`：
> 它会检查 `tools/` 与 `skill/extras/` 的同名脚本有没有分叉（两边必须一样）。

如果这台设备上的 agent 还没装这个 skill，装上：

```bash
cp -r skill ~/.claude/skills/image-to-css-art      # Claude Code
cp -r skill ~/.agents/skills/image-to-css-art      # 通用 skills 目录
```

---

## 1. 首选：一条命令出全套

```bash
$PY tools/make_art.py <原图> <作品名> ["标题"] [1×秒数=90] [线稿时间占比=0.15] [epsilon=0.25] [线稿线宽=2.6]
```

例：

```bash
$PY tools/make_art.py /path/photo.jpg 海边人像 "海边人像 · 逐笔绘制回放" 90 0.15 0.25 2.6
```

约 70 秒出全套，落在 `output/<作品名>/`，**就这三个文件**：

| 文件 | 说明 |
|---|---|
| `<名>.svg` | 矢量原图（补过描边，无白点） |
| `<名>.html` | Canvas 逐笔回放页（含真线稿层、宽屏并排、PC 键盘） |
| `<名>.json` | 本次参数报告 |

**不要额外生成用户没要的文件**——不要预览图、不要对比图、不要中间产物。
验证一律渲染到 `/tmp`，只在回复里报数据。

---

## 2. 硬性规则

| # | 规则 | 违反的后果 |
|---|---|---|
| 1 | **不要用「读图片」类工具读 png/jpg**，改用 Python 分析像素 | 部分客户端会报 `Invalid input`，对话直接崩 |
| 2 | **产物一律进 `output/<作品名>/`** | 曾把 160 个文件堆在工作区根目录，用户发火 |
| 3 | **越还原越好，不要为了体积妥协**（除非用户嫌手机卡） | 用户明确要求过 |
| 4 | 画布尺寸**一律从 SVG 的 `viewBox` 读，绝不硬编码** | 见踩坑 #6，会导致线稿整体错位 |
| 5 | 切横条时必须让相邻条**重叠一行** | 见踩坑 #7，否则整片横向白线 |

---

## 3. 用户可能的追加要求，对应怎么做

| 用户说 | 怎么做 |
|---|---|
| 「手机很卡 / 打开很卡 / 能不能优化」 | 已经默认是 Canvas 版了。若还卡：`--passes 2 --min-area 6`，或把长边减半 |
| 「线稿不太像 / 全是几何形状」 | 检查 `svg2canvas.py` 的 `build_lineart()` 是不是又用回了 `findContours`+`fill`。**必须走折线追踪 + `stroke`**，见踩坑 #5 |
| 「线稿细一点 / 粗一点」 | 命令最后一位是线宽，默认 2.6 |
| 「线稿时间长一点 / 短一点」 | 命令第 6 位是线稿时间占比，默认 0.15 |
| 「出现白线 / 白点」 | 白点→补 stroke（见踩坑 #3）；横向白线→切横条没重叠（踩坑 #7） |
| 「太糊了」 | ① 检查 stroke-width 是不是被调大了（最优 0.3~0.6）；② 把 epsilon 从 0.25 降到 0.08 |
| 「突然大面积上色」 | 时间权重是不是又写成等速/分段线性了？**必须用「面积加权 + 累计权重二分查找」**（踩坑 #4） |
| 「怎么不是先画线稿」 | `--order sketch` 的「线稿」**不是轮廓线**，真线稿在 `build_lineart()` 里（踩坑 #2） |

---

## 4. 收尾：推送到 GitHub 前必须做的检查

```bash
$PY tools/secret_scan.py --staged     # 密钥扫描，命中就停下清理
git diff --cached --stat              # 确认没有 .env / *.key / credentials.json
git log --oneline -3                  # 确认 commit message 里没有敏感信息
git push
```

三步全过才能 push。任何一步失败都要修完重跑整套。

---

## 5. 完整踩坑记录

见 [`docs/踩坑记录.md`](docs/踩坑记录.md)。**改动 `tools/` 下任何脚本前先读一遍**，
里面有每个设计决策的原因；参数怎么选见 [`docs/参数速查.md`](docs/参数速查.md)。
