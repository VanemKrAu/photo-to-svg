# 出处与许可

## 上游项目

本项目建立在 **AvroraCL/image-to-css-art** 之上：

| | |
|---|---|
| 仓库 | https://github.com/AvroraCL/image-to-css-art |
| 作者 | [AvroraCL](https://github.com/AvroraCL) |
| 许可 | **MIT License** · Copyright (c) 2026 AvroraCL |
| 说明 | 将参考图片离线描摹为纯 HTML + CSS 单文件插画的 Agent 插件，支持轮廓提取、局部渐变与输出审计 |

上游提供的是**算法管线**：Oklab 颜色量化 → 邻接区域合并 → 亚像素轮廓提取 →
偶奇多边形挖洞 → 局部渐变拟合，输出为 CSS `clip-path` 堆叠的 `<div>`。

感谢原作者。没有这条管线，本项目的 SVG 输出无从谈起。

---

## 这个仓库做了什么

### 1. 完整搬运了 skill（`skill/`）

`skill/` 目录 = 上游 `skills/image-to-css-art/` 的**原样副本**，用于：

- 直接复制到 agent 的 skills 目录即可使用（离线、可审计）
- MIT 协议要求的版权与许可声明完整保留（见 `skill/LICENSE`）

**与上游的差异**（仅一处，已明确标注）：

| 文件 | 差异 |
|---|---|
| `skill/SKILL.md` | **前 68 行与上游逐字节一致**；文件末尾追加了一节 `Local extension`，说明本项目的 `extras/` 扩展。追加部分用 HTML 注释 `<!-- 以下为本仓库追加 -->` 明确分隔 |
| 其余全部文件 | 与上游逐字节一致 |

> 搬运时发现本地那份 `scripts/css_art/quality.py` 落后于上游
> （上游已修复「缩略图窄边可能取整为 0，导致 `cv2.resize` 报错」的问题），
> 已同步为上游修复版。

### 2. 新增了 SVG 输出扩展（`skill/extras/`）

上游产物是 CSS `<div>` 堆叠；`extras/` 里的两个脚本复用同一套管线，
但输出**真正的 SVG `<path>`**：

| 脚本 | 作用 |
|---|---|
| `extras/build_svg_art.py` | 描摹成 SVG（`<path>` + `fill-rule="evenodd"` 挖洞 + `linearGradient`）+ 生成逐笔回放页 |
| `extras/verify_svg.py` | 栅格化重绘算 MAE，出三联对比图 |

这两个脚本**不修改上游任何文件**，整个 `extras/` 可独立删除。

### 3. 新增了完整流水线（`tools/`）

在 extras 的基础上继续往下做了很多（这部分完全是本项目新增）：

| 脚本 | 作用 |
|---|---|
| `tools/make_art.py` | **入口**：一条命令串起描摹 → 补描边 → 出 Canvas 回放页，含画布尺寸自检 |
| `tools/svg2canvas.py` | **Canvas 回放页**：真线稿层、大色块扫描线切分、视觉重量时间轴、宽屏并排、PC 适配 |
| `tools/secret_scan.py` | 推送前的密钥自检（无外部依赖） |

以及 `docs/` 下的三份文档，其中 `docs/踩坑记录.md` 记录的 12 个坑
全部来自本项目的实际调试过程（线稿做成了几何色块、整片横向白线、线稿整体错位、
手机卡顿、描边越大越糊……），跟上游代码无关，但都是复用这条管线时会遇到的。

---

## 两者的关系

```
上游 AvroraCL/image-to-css-art         本项目 photo-to-svg
┌────────────────────────────┐        ┌──────────────────────────────────┐
│ scripts/css_art/           │        │ skill/          上游原样副本      │
│   量化 → 合并 → 轮廓 → 渐变 │  ────► │   scripts/       (CSS 输出)      │
│   输出 CSS clip-path       │        │   extras/        SVG 输出 ★      │
└────────────────────────────┘        │ tools/           完整流水线 ★    │
                                      │   make_art.py    一条命令出全套   │
   「纯 HTML+CSS、禁用任何图片资源」    │   svg2canvas.py  Canvas 回放页   │
   → 用上游原版                        │                                  │
                                      │   ★ = 本项目新增                 │
                                      └──────────────────────────────────┘
```

**该用哪个？**

| 需求 | 用什么 |
|---|---|
| 纯 HTML + CSS、有资源限制（禁 img/svg/script/base64/外链） | 上游原版 `skill/scripts/image_to_css.py` |
| 要矢量 `.svg` 文件、或想看绘制过程回放 | 本项目 `tools/make_art.py` |

---

## 许可

- `skill/` 下的上游代码：**MIT License**，Copyright (c) 2026 AvroraCL，全文见 `skill/LICENSE`
- 本项目新增部分（`tools/`、`skill/extras/`、`docs/`、`install.sh` 等）：同样以 MIT 发布

按 MIT 要求，分发本仓库时请**保留 `skill/LICENSE`**。
