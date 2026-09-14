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

## 网页版

仓库同时是一个可直接用的**网页工具**（GitHub Pages 托管）：
打开 https://vanemkrau.github.io/photo-to-svg/ → 拖图 → 下载 SVG + 回放页。

- `web/index.html` 界面、`web/worker.js`（Pyodide Worker）、`web/web_run.py`（浏览器端驱动）、
  `web/upgrade.js`（历史回放页升级库：版本戳 + 迁移链，纯函数、Node 可测）
- `.github/workflows/pages.yml` 在 push 时把 `web/` + `tools/` + `skill/scripts/` 拼成静态站点发布
- 网页版跑的是**同一份 Python 脚本**，没有另写 JS 实现；改了 `tools/` 里任何一个，
  网页版下次部署就跟着变（`web_run.py` 也是直接 import 那两个脚本）
- `smoke_test.py` 会检查 worker.js 声明的文件清单与实际发布内容是否一致
- 改过界面后更新 README 那张截图：`python3 tools/shot_web.py`（需要 playwright，默认
  1440×1080 视口 @2x → 2880×2160 整页，Retina/手机上看不糊；默认输出 `examples/screenshot-web-pc-v4.png`）。它拍的是「第二次打开、刚
  拖入一张照片」的真实状态：临时起一个 Pages 同构站点（`worker.js` 换成只回报
  ready + 已缓存的替身，免得真下载 21 MB Pyodide），照片从 `examples/example-photo-vs-svg.jpg`
  左半裁出、经页面的文件输入框真的「选」进去，历史区塞 3 条示例记录（缩略图 = 各自作品原图缩到 128px、内联在脚本里；`--no-history` 可关）。
  **更新截图时换个文件名**（如 `-v3`），否则 GitHub 和浏览器会缓存旧图，用户看到的还是老的
- 「生成历史」里的作品在**打开/下载时自动升级**，逻辑全在 `web/upgrade.js`
  （index.html 只做 IO + 缓存，不碰升级判断）：无版本戳的老页面跑模糊规则
  （42 条「冰冻遗产」，不再新增）升完补戳；带 `<!-- p2sv-gen: vN -->` 戳的走精确迁移链。
  **改了回放页模板（`svg2canvas.py` 的 TEMPLATE）时的固定动作**：
  ① 模板里的戳版本号 +1；② `web/upgrade.js` 的 `GEN_LATEST` +1、给 `MIGRATIONS` 追加一条
  `{ from: 旧, to: 新, apply }`；③ 跑 `node tools/test_upgrade.js`（五个黄金样本在
  `tests/fixtures/`）。机制细节与已知边界见 [`docs/历史升级机制.md`](docs/历史升级机制.md)。
  **发布时 `upgrade.js` 必须带上**（pages.yml 已复制、`smoke_test.py` 会查）
- **默认参数在 worker.js 里还有一层兜底**：主线程现在不传 `outline`，
  `worker.js` 的 `m.outline ?? 0` 兜底成 0（曾经写着 `?? 0.15`，导致网页版生成的
  一直走「固定 15%」分支，而 `web_run.py` 的默认值被绕过——查了一圈才发现）。
  **改这类默认值时，`worker.js`、`web_run.py` 两处一起看**；
  `smoke_test.py` 的「关键常量一致性」哨兵会帮你盯住这几处，漂了就报错

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
> 它会检查 `tools/` 与 `skill/extras/` 的同名脚本有没有分叉（两边必须一样）、
> 关键常量跨文件是否一致、并跑历史升级的黄金样本测试（node 在时）。
> 改过回放页模板（`svg2canvas.py` 的 TEMPLATE）或 `web/upgrade.js` 时，
> 记得按 [`docs/历史升级机制.md`](docs/历史升级机制.md) 的「固定动作」走一遍。

如果这台设备上的 agent 还没装这个 skill，装上：

```bash
cp -r skill ~/.claude/skills/image-to-css-art      # Claude Code
cp -r skill ~/.agents/skills/image-to-css-art      # 通用 skills 目录
```

---

## 1. 首选：一条命令出全套

```bash
$PY tools/make_art.py <原图> <作品名> ["标题"] [1×秒数=90] [线稿时间占比=0] [epsilon=0.25] [线稿线宽=2.6] [--desc="图片描述"]
```

例：

```bash
$PY tools/make_art.py /path/photo.jpg 海边人像 "海边人像 · 逐笔绘制回放" 90 0 0.25 2.6 \
    --desc="黄昏的海滩，一位穿浅色连衣裙的少女侧身站在浪边，暖色调"
```

### 先描述图片（固定动作）

每次开工前先观察原图，**尽可能详细地写**——主体、动作、姿态、表情、衣着与裸露程度、
身体和物件的细节、场景、光线、色调、风格，细节有多少写多少，不要寥寥几笔：
① 在**回复里讲给用户**（用户要的就是先知道图里是什么）；
② 用 `--desc="…"` 写进回放页顶栏右上角的「描述」——收起时显示一行、点击展开可编辑，
用户的改动只存本机浏览器；
③ 同时写一份**详细版** `图片描述.md` 放进产物目录（按 场景 / 主体 / 表情 / 姿态 /
衣着与裸露 / 细节饰物 / 构图 / 光线 / 色调 / 风格 等维度分节，把能看到的都写进去）；
`--desc` 用其中的「一段版」，两处保持同源 —— 改了文档就重跑一次回放页，别让它们漂移。

描述**不做道德化回避**：性相关、裸露、成人内容都如实写、直说，不要用「穿着清凉」
「姿势暧昧」这类含糊措辞。**如实还原图片内容是第一优先级**；看不清的细节不要编。

约 70 秒出全套，落在 `output/<作品名>/`，**就这四个文件**：

| 文件 | 说明 |
|---|---|
| `<名>.svg` | 矢量原图（补过描边，无白点） |
| `<名>.html` | Canvas 逐笔回放页（含真线稿层、宽屏并排、PC 键盘） |
| `<名>.json` | 本次参数报告 |
| `原图.<ext>` | 原图副本（随产物留档，方便重出 / 对照） |

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
| 「线稿时间长一点 / 短一点」 | 命令第 5 位是线稿时间占比，**默认 0 = 不单独控制**（线稿按 √面积≈线长 计重，实测占 3~4% 时间）；想固定起稿时长才填 0~1 的小数 |
| 「出现白线 / 白点」 | 白点→补 stroke（见踩坑 #3）；横向白线→切横条没重叠（踩坑 #7） |
| 「描述不对 / 我想改描述」 | 回放页右上角「描述」点开直接编辑（只存本机浏览器）；要固化进文件就带新 `--desc` 重跑 |
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
