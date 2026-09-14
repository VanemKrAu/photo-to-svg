/* upgrade.js —— 历史回放页升级库（纯文本、零依赖，浏览器与 Node 共用）
 *
 * 背景：网页版把「回放页 HTML」原样存在用户浏览器的 IndexedDB 里，没法重新生成；
 * 所以打开 / 下载历史作品时，要把老世代的页面「升级」成新样子。本库专干这件事。
 *
 * 两个时代：
 *   ① 无版本戳时代（2026-09-14 之前的产物）：页面里没有任何版本信息，只能靠
 *      「文本指纹」猜它生于哪一代 —— LAYOUT_RULES / AXIS_OLD 那套模糊规则。
 *      修过两个结构性事故（docs/踩坑记录.md #18 #19），代价是规则只能累积。
 *   ② 版本戳时代（v1 起）：生成端（tools/svg2canvas.py 模板）在页面 <title> 前
 *      埋一行 <!-- p2sv-gen: vN -->；升级时读号走精确迁移链（MIGRATIONS），
 *      每条迁移只处理「相邻版本」，apply 完版本号推进 ——
 *      「这条打过了吗」的启发式判定从此不存在，幂等性由版本号保证。
 *
 * 无戳的存量页面：先跑模糊规则（能升多少升多少），补戳为 LEGACY_TARGET（=1，
 * 模糊规则的等价版本），再接着走迁移链。
 *
 * ⚠️ 改回放页模板（tools/svg2canvas.py 的 TEMPLATE）时的固定动作：
 *   1. GEN_LATEST 加一，给 MIGRATIONS 追加 { from: 旧版, to: 新版本, apply }；
 *      apply(text) 返回 null = 「这段代码不在这份产物里」：跳过，但版本号照样推进；
 *   2. 不要再往 LAYOUT_RULES 里追加规则（那是冻结遗产，只服务无戳存量）；
 *   3. 跑 node tools/test_upgrade.js（幂等 / 不重复 / 目标版本，逐样本断言）。
 *   详见 docs/历史升级机制.md。
 *
 * 本文件是纯函数库：不碰 DOM、不碰 Blob、不碰 IndexedDB ——
 * 读 SVG 头部这类 IO 由调用方（web/index.html）做完、把文本传进来，Node 里可直接测。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) { module.exports = factory(); }
  else { root.P2SVUpgrade = factory(); }
})(typeof self !== 'undefined' ? self : this, function () {
"use strict";

var GEN_LATEST = 1;      /* 当前回放页模板版本：v1 = 第一个带版本戳的模板（2026-09-14） */
var LEGACY_TARGET = 1;   /* 模糊规则升完的等价版本。恒为 1，不随 GEN_LATEST 变 */
var MIGRATIONS = [       /* 未来：{ from: N, to: N+1, apply(text){ return text | null; } } */
];

function readVersion(text){
  /* 戳在 <head> 里（<title> 前），只搜前 4KB —— 正文有十几 MB，全量正则没必要 */
  var m = String(text).slice(0, 4096).match(/<!--\s*p2sv-gen:\s*v(\d+)\s*-->/);
  return m ? parseInt(m[1], 10) : 0;      /* 0 = 无版本戳（存量） */
}

function stampVersion(text, ver){
  if (readVersion(text)) return text;      /* 已有戳：不动（幂等的第一道保险） */
  var i = text.indexOf('<title>');
  if (i < 0) return text;                  /* 找不到锚点：放弃，保持原样 */
  var lineStart = text.lastIndexOf('\n', i) + 1;
  return text.slice(0, lineStart) +
         '<!-- p2sv-gen: v' + ver + ' -->\n' + text.slice(lineStart);
}

function applyMigrations(text, fromVer, latest, migrations){
  /* 精确迁移链：从 fromVer 逐级走到 latest。
     迁移未命中（apply 返回 null）→ 跳过但推进版本；
     链缺环 → 停下，note 说明（停在缺环处）。返回 { text, version, notes }。 */
  var out = text, v = fromVer, notes = [];
  while (v < latest) {
    var m = null;
    for (var i = 0; i < migrations.length; i++) {
      if (migrations[i].from === v) { m = migrations[i]; break; }
    }
    if (!m) { notes.push('迁移链缺 v' + v + ' -> v' + (v + 1) + '，停在 v' + v); break; }
    try {
      var nx = m.apply(out);
      if (nx == null) notes.push('v' + m.from + ' -> v' + m.to + ' 未命中（跳过）');
      else if (nx !== out) out = nx;
    } catch (e) { notes.push('v' + m.from + ' -> v' + m.to + ' 异常：' + e); }
    v = m.to;
  }
  return { text: out, version: v, notes: notes };
}

/* ---------- 时间轴：旧 ↔ 新代码段（从 web/index.html 逐字搬来） ---------- */
  const AXIS_OLD =
    "  for(i=NL;i<N;i++){ w=0.15+AREA[i]*0.002; W[i]=w; sumW+=w; }\n" +
    "  var LW = SKETCH_T>0 ? (SKETCH_T*sumW)/((1-SKETCH_T)*NL) : 0.15;\n" +
    "  for(i=0;i<NL;i++) W[i]=LW;";
  const AXIS_NEW =
    "  if(SKETCH_T>0){\n" +
    "    /* 指定了起稿时长：色块先按视觉重量算好，线稿反解成固定占比 */\n" +
    "    for(i=NL;i<N;i++){ w=0.15+AREA[i]*0.002; W[i]=w; sumW+=w; }\n" +
    "    var LW=(SKETCH_T*sumW)/((1-SKETCH_T)*NL);\n" +
    "    for(i=0;i<NL;i++) W[i]=LW;\n" +
    "  }else{\n" +
    "    /* 默认：线稿不单独占时间，但用「线的长度」而不是「bbox 面积」来估它的视觉重量——\n" +
    "       线是细长的，bbox 会把整条线的跨度都圈进去、明显超重（实测能把起稿拖到 13 秒）。\n" +
    "       √bbox面积 ≈ 线的特征长度，和色块那边的「面积」在几何上对齐。 */\n" +
    "    for(i=0;i<NL;i++) W[i]=0.15+Math.sqrt(AREA[i])*0.05;\n" +
    "    for(i=NL;i<N;i++) W[i]=0.15+AREA[i]*0.002;\n" +
    "  }";

/* ---------- 布局规则（从 web/index.html 逐字搬来）
   ⚠️ 冰冻区：这 40 条只为「无版本戳时代」的存量产物服务，不再新增。
   新的页面改动一律走上面的 MIGRATIONS 迁移链。 ---------- */
/* 布局升级规则（2026-09「相册式」缩放）。
   逐条独立应用 —— 用户的历史横跨好几个世代，要求「全中才生效」
   会让中间世代的产物一条都升不了。 */
  const LAYOUT_RULES = [
    { re: /  \/\* 首帧兜底尺寸[\s\S]{0,400}?touch-action:none\}/,
      to: `  /* .frame 是「视口」：撑满整个可用区域，图片按适配尺寸居中显示。
     不要给它 aspect-ratio —— 那样它会缩成「贴着图片的小框」，
     放大后只能在小框里裁剪，用不上整屏（手机全屏时尤其明显）。
     放大时图片在视口里平移，超出部分正好被这里的 overflow 裁掉。 */
  .frame{position:relative;flex:1 1 auto;min-width:0;align-self:stretch;overflow:hidden;touch-action:none}
  /* 跟随缩放/平移的「画面」容器：canvas 和对照层都放里面，一次 transform 全部同步 */
  .view{position:absolute;left:0;top:0;will-change:transform}` },
    { from: `  .stage{flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;
    padding:8px;gap:14px}`,
      to: `  .stage{position:relative;flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center;
    padding:8px;gap:14px}` },
    { from: `  /* 缩放控件：浮在画框右下角 */
  .zoombar{position:absolute;right:10px;bottom:10px;display:flex;gap:3px;z-index:5;
    background:rgba(10,10,16,.74);border:1px solid var(--line);border-radius:9px;
    padding:4px;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}`,
      to: `  /* 缩放控件：毛玻璃浮框，钉在「界面」右下角（画面区底部，紧挨底部工具栏上方）。
     定位基准是 .stage（整个观感区域）而不是 .frame（画面）——
     相对画面定位时它会跟着图走，图小的时候正好压在图上。 */
  .zoombar{position:absolute;right:10px;bottom:10px;z-index:10;
    display:flex;gap:3px;align-items:center;
    background:rgba(10,10,16,.74);border:1px solid var(--line);border-radius:9px;
    padding:4px;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}` },
    { re: /(    )<canvas id="art" width="(\d+)" height="(\d+)"><\/canvas>\n(    )<div id="ghost"><\/div>\n(    )<div id="split"><\/div>/,
      to: `$1<div class="view" id="view">\n$1  <canvas id="art" width="$2" height="$3"></canvas>\n$1  <div id="ghost"></div>\n$1  <div id="split"></div>` },
    { from: `    <div id="split"></div>\n    <div id="toast">`,
      to: `    <div id="split"></div>\n    </div>\n    <div id="toast">` },
    { from: `    <div class="zoombar" id="zoombar">
      <button id="zOut" title="缩小（滚轮 / 双指捏合）">−</button>
      <span class="lvl" id="zLvl">100%</span>
      <button id="zIn" title="放大（滚轮 / 双指捏合）">＋</button>
      <button id="zFit" title="适应窗口（按 0）">⤢</button>
      <button id="zRst" title="实际大小（按 1）">1:1</button>
    </div>
  </div>
  <div id="side"><div class="tag">原图</div></div>
</div>

<footer>
  <div class="row">
    <div class="bar" id="bar">
      <div class="track"></div><div class="fill" id="fill"></div><div class="knob" id="knob"></div>
    </div>
    <span class="meta" id="pct">0%</span>
  </div>`,
      to: `  </div>
  <div id="side"><div class="tag">原图</div></div>
  <div class="zoombar" id="zoombar">
    <button id="zOut" title="缩小（滚轮 / 双指捏合）">−</button>
    <span class="lvl" id="zLvl">100%</span>
    <button id="zIn" title="放大（滚轮 / 双指捏合）">＋</button>
    <button id="zFit" title="适应窗口（按 0）">⤢</button>
    <button id="zRst" title="实际大小（按 1）">1:1</button>
  </div>
</div>

<footer>
  <div class="row">
    <div class="bar" id="bar">
      <div class="track"></div><div class="fill" id="fill"></div><div class="knob" id="knob"></div>
    </div>
    <span class="meta" id="pct">0%</span>
  </div>` },
    { re: /(var CV=document\.getElementById\("art"\), W=\d+, H=\d+;\n)/,
      to: `$1var fitW=0, fitH=0;                     /* 图片的「适配尺寸」：显示在视口中央的基准大小 */\n` },
    { from: `var frame=document.getElementById("frame");\nfunction setSplit(clientX){\n  var r=frame.getBoundingClientRect();`,
      to: `var frame=document.getElementById("frame"), VIEW=document.getElementById("view");\nfunction setSplit(clientX){\n  var r=VIEW.getBoundingClientRect();          /* 分界按「画面」（不是视口）的比例算 */` },
    { from: `document.getElementById("full").addEventListener("click",toggleFull);\nframe.addEventListener("dblclick",toggleFull);`,
      to: `document.getElementById("full").addEventListener("click",toggleFull);\n/* 双击留给「放大/还原」（绑在 frame 上，黑边区域双击也有效）；全屏只用按钮或 F 键 */` },
    { from: `  else if(e.key==="1"){ e.preventDefault(); pause(); setZoom(1/fitScale); }\n  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); setZoom(zoom.s*1.4); }\n  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); setZoom(zoom.s/1.4); }`,
      to: `  else if(e.key==="1"){ e.preventDefault(); pause(); setZoomAt(1/fitScale); }\n  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); setZoomAt(zoom.s*1.4); }\n  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); setZoomAt(zoom.s/1.4); }` },
    { from: `  var dual=sideMode(), GAP=14;
  if(dual){
    sideEl.style.display="block";
    var cell=(aw-GAP)/2;
    var s=Math.min(cell/W, ah/H);
    var w=Math.max(1,Math.floor(W*s)), h=Math.max(1,Math.floor(H*s));
    frame.style.width=w+"px";  frame.style.height=h+"px";
    sideEl.style.width=w+"px"; sideEl.style.height=h+"px";
    fitScale = w / W;                       /* 1:1 按钮与快捷键要用它换算 */
    if(zoom.s <= 1.0001) applyView();
  }else{
    sideEl.style.display="none";
    var s2=Math.min(aw/W, ah/H);
    frame.style.width =Math.max(1,Math.floor(W*s2))+"px";
    frame.style.height=Math.max(1,Math.floor(H*s2))+"px";
    fitScale = s2;
    if(zoom.s <= 1.0001) applyView();
  }`,
      to: `  var dual=sideMode(), GAP=14, vw;
  if(dual){
    sideEl.style.display="block";
    vw=(aw-GAP)/2;                          /* 左栏（视口）可用宽度 */
    /* 双栏模式：右栏已是完整原图，左栏别再叠「半张原图 + 分界线」 */
    ghostEl.style.display="none";
    splitEl.style.display="none";
  }else{
    sideEl.style.display="none";
    vw=aw;
    /* 单栏：恢复对照层（开着的话）*/
    ghostEl.style.display=ghostOn?"block":"none";
    splitEl.style.display=ghostOn?"block":"none";
  }
  /* 视口尺寸由 CSS 撑满，这里只算「图片的适配尺寸」并把它居中 */
  var s=Math.min(vw/W, ah/H);
  fitScale=s;                               /* 1:1 按钮与快捷键要用它换算 */
  fitW=Math.max(1,Math.round(W*s)); fitH=Math.max(1,Math.round(H*s));
  VIEW.style.width =fitW+"px";
  VIEW.style.height=fitH+"px";
  if(dual){ sideEl.style.width=fitW+"px"; sideEl.style.height=fitH+"px"; }
  clampPan();
  applyView();` },
    /* 「相册式布局已就位、但还早于双栏隐藏对照层那一版（0bcafd4）」的页面 ——
       这正是用户历史记录里最常见的一批（0a7cbbc / 83dbe13 / ea3acd6 都属此列）。
       上面那条规则只在**老布局**（var cell=(aw-GAP)/2）上生效，这批页面匹配不到，
       于是它们永远拿不到「双栏别再叠半张原图 + 分界线」这个修复 ——
       这也是用户说「修完了网站上还是有问题」的来源之一。这条专门补它。 */
    { from: `  var dual=sideMode(), GAP=14, vw;
  if(dual){
    sideEl.style.display="block";
    vw=(aw-GAP)/2;                          /* 左栏（视口）可用宽度 */
  }else{
    sideEl.style.display="none";
    vw=aw;
  }`,
      to: `  var dual=sideMode(), GAP=14, vw;
  if(dual){
    sideEl.style.display="block";
    vw=(aw-GAP)/2;                          /* 左栏（视口）可用宽度 */
    /* 双栏模式：右栏已是完整原图，左栏别再叠「半张原图 + 分界线」 */
    ghostEl.style.display="none";
    splitEl.style.display="none";
  }else{
    sideEl.style.display="none";
    vw=aw;
    /* 单栏：恢复对照层（开着的话）*/
    ghostEl.style.display=ghostOn?"block":"none";
    splitEl.style.display=ghostOn?"block":"none";
  }` },
    { from: `  CV.style.transformOrigin = "0 0";
  CV.style.transform = "translate(" + zoom.tx + "px," + zoom.ty + "px) scale(" + zoom.s + ")";`,
      to: `  var fw=frame.clientWidth, fh=frame.clientHeight;
  /* 图片视觉左上角 = 「居中基准」+ 用户平移（tx/ty 是相对居中位置的偏移） */
  var bx=(fw-fitW*zoom.s)/2+zoom.tx, by=(fh-fitH*zoom.s)/2+zoom.ty;
  VIEW.style.transformOrigin = "0 0";
  VIEW.style.transform = "translate(" + bx + "px," + by + "px) scale(" + zoom.s + ")";` },
    { from: `function clampPan(){
  var maxX = Math.max(0, W * zoom.s - W), maxY = Math.max(0, H * zoom.s - H);
  zoom.tx = Math.min(0, Math.max(-maxX, zoom.tx));
  zoom.ty = Math.min(0, Math.max(-maxY, zoom.ty));
}
/* 以 (ax, ay) 为锚点缩放 —— 锚点是「相对画布左上角」的屏幕坐标。
   推导：画布点 a 的屏幕位置 = tx + a*s；要让它缩放后位置不变，
   则 tx' = p - (p - tx) * (s'/s)。 */
function setZoom(ns, ax, ay){
  ns = Math.min(24, Math.max(1, ns));
  if(ax === undefined){ ax = W * zoom.s / 2; ay = H * zoom.s / 2; }
  var k = ns / zoom.s;
  zoom.tx = ax - (ax - zoom.tx) * k;
  zoom.ty = ay - (ay - zoom.ty) * k;
  zoom.s = ns;
  clampPan();
  applyView();
}`,
      to: `/* 平移范围：图片比视口大时不许把边缘拖进视口；没占满的方向锁在居中 */
function clampPan(){
  var fw=frame.clientWidth, fh=frame.clientHeight;
  var vw=fitW*zoom.s, vh=fitH*zoom.s;
  var mx=Math.max(0,(vw-fw)/2), my=Math.max(0,(vh-fh)/2);
  zoom.tx = Math.max(-mx, Math.min(mx, zoom.tx));
  zoom.ty = Math.max(-my, Math.min(my, zoom.ty));
}
/* 以视口内的点 (cx, cy) 为锚点缩放（不传就锚定视口中心）——
   缩放后该点对应的屏幕位置保持不变，和相册/地图的手感一致。 */
function setZoomAt(ns, cx, cy){
  ns = Math.min(24, Math.max(1, ns));
  var fw=frame.clientWidth, fh=frame.clientHeight;
  var k = ns / zoom.s;
  var L = (fw-fitW*zoom.s)/2+zoom.tx, T = (fh-fitH*zoom.s)/2+zoom.ty;   /* 图片视觉左上角 */
  if(cx === undefined){ cx = fw/2; cy = fh/2; }
  var a = cx - L, b = cy - T;                    /* 锚点相对图片左上角 */
  zoom.s = ns;
  zoom.tx = cx - a*k - (fw-fitW*ns)/2;
  zoom.ty = cy - b*k - (fh-fitH*ns)/2;
  clampPan();
  applyView();
}` },
    { from: `  var r = CV.getBoundingClientRect();\n  setZoom(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);`,
      to: `  var r = frame.getBoundingClientRect();\n  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);` },
    { from: `    var a = ptrs[ids[0]], b = ptrs[ids[1]];
    pinch = { d: Math.hypot(a.x-b.x, a.y-b.y), s: zoom.s,
              cx:(a.x+b.x)/2, cy:(a.y+b.y)/2 };`,
      to: `    var a = ptrs[ids[0]], b = ptrs[ids[1]];
    var r = frame.getBoundingClientRect();
    pinch = { d: Math.hypot(a.x-b.x, a.y-b.y), s: zoom.s,
              cx:(a.x+b.x)/2 - r.left, cy:(a.y+b.y)/2 - r.top };` },
    { from: `    if(d > 0) setZoom(pinch.s * (d / pinch.d), pinch.cx, pinch.cy);`,
      to: `    if(d > 0) setZoomAt(pinch.s * (d / pinch.d), pinch.cx, pinch.cy);` },
    { from: `/* 双击画面：1× ⇄ 2.5× 切换（全屏改用按钮或 F 键） */
CV.addEventListener("dblclick", function(e){
  var r = CV.getBoundingClientRect();
  if(zoom.s > 1.01) resetView();
  else setZoom(2.5, e.clientX - r.left, e.clientY - r.top);
});`,
      to: `/* 双击画面：1× ⇄ 2.5× 切换（绑在视口上，黑边区域双击也有效） */
frame.addEventListener("dblclick", function(e){
  var r = frame.getBoundingClientRect();
  if(zoom.s > 1.01) resetView();
  else setZoomAt(2.5, e.clientX - r.left, e.clientY - r.top);
});` },
    { from: `document.getElementById("zIn").onclick  = function(){ pause(); setZoom(zoom.s * 1.4); };\ndocument.getElementById("zOut").onclick = function(){ pause(); setZoom(zoom.s / 1.4); };`,
      to: `document.getElementById("zIn").onclick  = function(){ pause(); setZoomAt(zoom.s * 1.4); };\ndocument.getElementById("zOut").onclick = function(){ pause(); setZoomAt(zoom.s / 1.4); };` },
    { from: `document.getElementById("zRst").onclick = function(){ pause(); setZoom(1 / fitScale); };`,
      to: `document.getElementById("zRst").onclick = function(){ pause(); setZoomAt(1 / fitScale); };` },
    /* 色块补同色描边（2026-09-14）：不补的话色块间的发丝缝会露出底板，
       深色图上就是密集的黑线/黑斑（用户报「脸上有黑色斑纹」） */
    { from: `    ctx.fill("evenodd");
  }
}`,
      to: `    ctx.fill("evenodd");
    /* 补一层同色描边：色块之间会有发丝缝，不补的话缝里露出底板 ——
       深色图上就是密集的黑线/黑斑（脸这种细节密集处最明显）。 */
    ctx.strokeStyle=ctx.fillStyle;
    ctx.lineWidth=0.7; ctx.lineJoin="round";
    ctx.stroke();
  }
}` },
    /* 右栏原图改用可变换层 #sideView（2026-09-14）：可滚轮缩放、与左栏同步；
       顺带修「拖出画面松手后画面粘住」与「分界线跟着鼠标跑」 */
    { from: `  #side{display:none;position:relative;flex:0 0 auto;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden;
    background-image:var(--ghostimg);background-size:100% 100%;background-repeat:no-repeat}`,
      to: `  #side{display:none;position:relative;flex:0 0 auto;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden}
  #sideView{position:absolute;left:0;top:0;will-change:transform;
    background-image:var(--ghostimg);background-size:100% 100%;background-repeat:no-repeat}` },
    { from: `  <div id="side"><div class="tag">原图</div></div>`,
      to: `  <div id="side"><div id="sideView"></div><div class="tag">原图</div></div>` },
    { from: `var frame=document.getElementById("frame"), VIEW=document.getElementById("view");`,
      to: `var frame=document.getElementById("frame"), VIEW=document.getElementById("view"),
    SIDEVIEW=document.getElementById("sideView");` },
    { from: `  if(dual){ sideEl.style.width=fitW+"px"; sideEl.style.height=fitH+"px"; }
  clampPan();`,
      to: `  if(dual){
    sideEl.style.width=fitW+"px"; sideEl.style.height=fitH+"px";
    SIDEVIEW.style.width=fitW+"px"; SIDEVIEW.style.height=fitH+"px";
  }
  clampPan();` },
    { from: `  VIEW.style.transform = "translate(" + bx + "px," + by + "px) scale(" + zoom.s + ")";
  var zl = document.getElementById("zLvl");`,
      to: `  VIEW.style.transform = "translate(" + bx + "px," + by + "px) scale(" + zoom.s + ")";
  if(SIDEVIEW){
    SIDEVIEW.style.transformOrigin = "0 0";
    SIDEVIEW.style.transform = "translate(" + zoom.tx + "px," + zoom.ty + "px) scale(" + zoom.s + ")";
  }
  var zl = document.getElementById("zLvl");` },
    { from: `frame.addEventListener("pointerdown", function(e){
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };
  var ids = Object.keys(ptrs);`,
      to: `frame.addEventListener("pointerdown", function(e){
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };
  try{ frame.setPointerCapture(e.pointerId); }catch(_){}
  var ids = Object.keys(ptrs);` },
    { from: `frame.addEventListener("pointermove",function(e){
  if(!ghostOn) return;
  if(e.pointerType==="mouse" || e.buttons) setSplit(e.clientX);
});`,
      to: `frame.addEventListener("pointermove",function(e){
  if(!ghostOn) return;
  if(e.buttons) setSplit(e.clientX);   /* 只在按住拖动时移动分界线 */
});` },
    /* 这条的 from 只是「左栏 wheel 那三行」，它要补的是右栏滚轮监听；
       而右栏滚轮在共享版/独立版里写法完全不同，靠 to 的新增块认不出「已经补过」——
       独立版页面会被它再插一段共享版监听（下面那条再改回独立版，看着结果对、
       其实每跑一次都改写一次，幂等就废了）。所以显式指一个**两版都有**的哨兵。 */
    { sentinel: 'sideEl.addEventListener("wheel", function(e){',
      from: `  var r = frame.getBoundingClientRect();
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });`,
      to: `  var r = frame.getBoundingClientRect();
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });
/* 右栏（原图）也能滚轮缩放：与左栏共享同一套缩放/平移状态 */
sideEl.addEventListener("wheel", function(e){
  e.preventDefault();
  pause();
  var sr = SIDEVIEW.getBoundingClientRect();
  var ax = (e.clientX - sr.left) + (frame.clientWidth  - fitW*zoom.s)/2;
  var ay = (e.clientY - sr.top ) + (frame.clientHeight - fitH*zoom.s)/2;
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), ax, ay);
}, { passive:false });` },

    /* 全屏改「CSS 伪全屏」（2026-09-14）：Android WebView（VIA 等）会把
       requestFullscreen 当「视频全屏」并强制横屏，竖图也被转；CSS 方案绕开它 */
    { from: `  footer{flex:0 0 auto;padding:8px 12px calc(10px + env(safe-area-inset-bottom));
    border-top:1px solid var(--line);background:linear-gradient(0deg,#141422,#0d0d16);
    display:flex;flex-direction:column;gap:8px}`,
      to: `  footer{flex:0 0 auto;padding:8px 12px calc(10px + env(safe-area-inset-bottom));
    border-top:1px solid var(--line);background:linear-gradient(0deg,#141422,#0d0d16);
    display:flex;flex-direction:column;gap:8px}
  /* 伪全屏（点「全屏」或按 F）：藏起页头/页脚，画面区占满整屏。
     不用 Fullscreen API —— Android WebView（VIA 等）的宿主会把它当
     「视频全屏」处理并强制横屏，竖图也被转；CSS 方案完全绕开它。 */
  body.fs header, body.fs footer{display:none}
  body.fs .stage{padding:0}
  body.fs .zoombar{bottom:14px}
  #fsExit{display:none;position:fixed;right:10px;top:10px;z-index:30;
    font:inherit;font-size:12.5px;color:var(--text);background:rgba(10,10,16,.74);
    border:1px solid var(--line);border-radius:9px;padding:7px 12px;cursor:pointer;
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  body.fs #fsExit{display:block}` },
    { from: `</footer>`,
      to: `</footer>
<button id="fsExit" type="button">✕ 退出全屏</button>` },
    { from: `/* ---- 全屏 ---- */
function toggleFull(){
  var d=document, el=d.documentElement;
  if(d.fullscreenElement||d.webkitFullscreenElement){
    (d.exitFullscreen||d.webkitExitFullscreen).call(d);
  }else{
    (el.requestFullscreen||el.webkitRequestFullscreen).call(el);
  }
}
document.getElementById("full").addEventListener("click",toggleFull);`,
      to: `/* ---- 全屏：CSS 伪全屏（藏起页头/页脚，画面区占满）----
   不用 Fullscreen API：Android WebView（VIA 等调用系统内核的浏览器）会把
   requestFullscreen 交给宿主处理，宿主常按「视频全屏」对待并强制横屏 ——
   竖图也会被转成横屏。CSS 方案不碰系统接口，所有浏览器行为一致，
   嵌在 iframe 里也能用（不需要 allowfullscreen）。 */
function toggleFull(){
  /* 嵌在别人的 iframe 里（网页端的预览框）：把「全屏」请求交给外层 ——
     自己这点「伪全屏」只能在框内变大，铺不满屏幕。 */
  if(window.parent && window.parent !== window){
    try{ window.parent.postMessage({ type: "art:fullscreen" }, "*"); return; }catch(_){}
  }
  var on = document.body.classList.toggle("fs");
  var b = document.getElementById("full");
  if(b) b.textContent = on ? "⛶ 退出全屏" : "⛶ 全屏";
  setTimeout(layout, 60);        /* 可用区域变了，重算画面适配 */
}
document.getElementById("full").addEventListener("click",toggleFull);
document.getElementById("fsExit").addEventListener("click",toggleFull);` },
    /* ---------- 2026-09-15：右栏改成「自己一套缩放 / 平移」 ----------
       原先进双栏后两栏**共享同一个 zoom**：滚轮在哪边滚都是两张一起放大，
       右栏还压根没绑拖动 —— 就是「拖原图拖不动、拖左栏时原图却跟着跑」。
       这批规则把它拆成两套独立状态（鼠标落在哪一栏就只动哪一栏）。
       老页面先被上面的规则升到「共享版」，再被这批改成「独立版」；
       用共享版生成过的作品也直接命中。
       每条都靠 alreadyApplied 判断是否已打过，重复跑不会二次插入。
       **from 一律只取代码行、不带注释** —— 注释在不同世代被手写改写过
       （共享版那批规则里的注释就和模板里的不一致），带上就匹配不上了。 */
    { from: `  #side{display:none;position:relative;flex:0 0 auto;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden}`,
      to: `  #side{display:none;position:relative;flex:0 0 auto;touch-action:none;
    box-shadow:0 10px 40px rgba(0,0,0,.8),0 0 0 1px var(--line);border-radius:5px;overflow:hidden}` },
    { from: `var zoom = { s: 1, tx: 0, ty: 0 };`,
      to: `var zoom = { s: 1, tx: 0, ty: 0 };
/* 右栏（原图）自己的一套缩放/平移：两栏互不影响，鼠标落在哪一栏就只动哪一栏 */
var zoom2 = { s: 1, tx: 0, ty: 0 };
/* 最后交互的是哪一栏：底部 ± 按钮与键盘 +/- 作用于它 */
var lastPane = "left";` },
    { from: `  else if(e.key==="0"){ e.preventDefault(); pause(); resetView(); }
  else if(e.key==="1"){ e.preventDefault(); pause(); setZoomAt(1/fitScale); }
  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); setZoomAt(zoom.s*1.4); }
  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); setZoomAt(zoom.s/1.4); }`,
      to: `  else if(e.key==="0"){ e.preventDefault(); pause(); resetView(); resetSide(); }
  else if(e.key==="1"){ e.preventDefault(); pause(); setZoomAt(1/fitScale); setZoomAt2(1/fitScale); }
  else if(e.key==="+"||e.key==="="){ e.preventDefault(); pause(); zoomBy(1.4); }
  else if(e.key==="-"||e.key==="_"){ e.preventDefault(); pause(); zoomBy(1/1.4); }` },
    { from: `  clampPan();
  applyView();
}
window.addEventListener("resize",layout);`,
      to: `  clampPan();
  applyView();
  clampPan2();
  applySide();
}
window.addEventListener("resize",layout);` },
    { from: `  if(SIDEVIEW){
    SIDEVIEW.style.transformOrigin = "0 0";
    SIDEVIEW.style.transform = "translate(" + zoom.tx + "px," + zoom.ty + "px) scale(" + zoom.s + ")";
  }
  var zl = document.getElementById("zLvl");
  if(zl) zl.textContent = Math.round(zoom.s * 100) + "%";
}`,
      to: `  syncZoomLabel();
}
/* 底部那个百分比：跟随「最后一次操作的那一栏」 */
function syncZoomLabel(){
  var zl = document.getElementById("zLvl");
  if(zl) zl.textContent = Math.round((lastPane==="right"?zoom2.s:zoom.s) * 100) + "%";
}` },
    { from: `  e.preventDefault();
  pause();
  var r = frame.getBoundingClientRect();`,
      to: `  e.preventDefault();
  pause(); lastPane="left";
  var r = frame.getBoundingClientRect();` },
    { from: `sideEl.addEventListener("wheel", function(e){
  e.preventDefault();
  pause();
  var sr = SIDEVIEW.getBoundingClientRect();
  var ax = (e.clientX - sr.left) + (frame.clientWidth  - fitW*zoom.s)/2;
  var ay = (e.clientY - sr.top ) + (frame.clientHeight - fitH*zoom.s)/2;
  setZoomAt(zoom.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), ax, ay);
}, { passive:false });`,
      to: `/* 右栏（原图）：自己的一套缩放 / 平移，跟左栏各走各的 */
function applySide(){
  if(!SIDEVIEW) return;
  SIDEVIEW.style.transformOrigin = "0 0";
  SIDEVIEW.style.transform = "translate(" + zoom2.tx + "px," + zoom2.ty + "px) scale(" + zoom2.s + ")";
  syncZoomLabel();
}
function clampPan2(){
  if(!SIDEVIEW) return;
  var sw=sideEl.clientWidth, sh=sideEl.clientHeight;
  var mx=Math.max(0,(fitW*zoom2.s-sw)/2), my=Math.max(0,(fitH*zoom2.s-sh)/2);
  zoom2.tx = Math.max(-mx, Math.min(mx, zoom2.tx));
  zoom2.ty = Math.max(-my, Math.min(my, zoom2.ty));
}
function setZoomAt2(ns, cx, cy){
  ns = Math.min(24, Math.max(1, ns));
  if(cx === undefined){ cx = sideEl.clientWidth/2; cy = sideEl.clientHeight/2; }
  var k = ns / zoom2.s;
  zoom2.tx = cx - (cx - zoom2.tx)*k;
  zoom2.ty = cy - (cy - zoom2.ty)*k;
  zoom2.s = ns;
  clampPan2(); applySide();
}
function resetSide(){ zoom2.s=1; zoom2.tx=0; zoom2.ty=0; applySide(); }
sideEl.addEventListener("wheel", function(e){
  e.preventDefault();
  pause(); lastPane="right";
  var r = sideEl.getBoundingClientRect();
  setZoomAt2(zoom2.s * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - r.left, e.clientY - r.top);
}, { passive:false });
/* 右栏的双指捏合 / 放大后单指拖动 —— 只动右栏 */
var ptrs2 = {}, pinch2 = null, pan2 = null;
sideEl.addEventListener("pointerdown", function(e){
  lastPane="right";
  ptrs2[e.pointerId] = { x:e.clientX, y:e.clientY };
  try{ sideEl.setPointerCapture(e.pointerId); }catch(_){}
  var ids = Object.keys(ptrs2);
  if(ids.length === 2){
    var a = ptrs2[ids[0]], b = ptrs2[ids[1]];
    var r = sideEl.getBoundingClientRect();
    pinch2 = { d:Math.hypot(a.x-b.x, a.y-b.y), s:zoom2.s,
               cx:(a.x+b.x)/2 - r.left, cy:(a.y+b.y)/2 - r.top };
    pan2 = null;
  } else if(zoom2.s > 1.0001){
    pan2 = { x:e.clientX, y:e.clientY, tx:zoom2.tx, ty:zoom2.ty };
  }
});
sideEl.addEventListener("pointermove", function(e){
  if(!ptrs2[e.pointerId]) return;
  ptrs2[e.pointerId] = { x:e.clientX, y:e.clientY };
  var ids = Object.keys(ptrs2);
  if(pinch2 && ids.length >= 2){
    e.preventDefault();
    var a = ptrs2[ids[0]], b = ptrs2[ids[1]];
    var d = Math.hypot(a.x-b.x, a.y-b.y);
    if(d > 0) setZoomAt2(pinch2.s * (d/pinch2.d), pinch2.cx, pinch2.cy);
  } else if(pan2){
    e.preventDefault();
    zoom2.tx = pan2.tx + (e.clientX - pan2.x);
    zoom2.ty = pan2.ty + (e.clientY - pan2.y);
    clampPan2(); applySide();
  }
}, { passive:false });
function endPtr2(e){
  delete ptrs2[e.pointerId];
  if(Object.keys(ptrs2).length < 2) pinch2 = null;
  if(Object.keys(ptrs2).length === 0) pan2 = null;
}
sideEl.addEventListener("pointerup", endPtr2);
sideEl.addEventListener("pointercancel", endPtr2);` },
    /* 这条的新增代码只有一行 lastPane="left";（16 字符），短到不足以当哨兵，
       显式指一个更长的特征串，否则它会认不出自己已经打过、反复插同一行。 */
    { sentinel: 'frame.addEventListener("pointerdown", function(e){\n  lastPane="left";',
      from: `frame.addEventListener("pointerdown", function(e){
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };`,
      to: `frame.addEventListener("pointerdown", function(e){
  lastPane="left";
  ptrs[e.pointerId] = { x:e.clientX, y:e.clientY };` },
    { from: `document.getElementById("zIn").onclick  = function(){ pause(); setZoomAt(zoom.s * 1.4); };
document.getElementById("zOut").onclick = function(){ pause(); setZoomAt(zoom.s / 1.4); };
document.getElementById("zFit").onclick = function(){ pause(); resetView(); };
document.getElementById("zRst").onclick = function(){ pause(); setZoomAt(1 / fitScale); };`,
      to: `/* ± 作用于「最后操作的那一栏」；⤢ 与 1:1 是全局动作，两栏一起归位 */
function zoomBy(k){
  if(lastPane==="right") setZoomAt2(zoom2.s * k);
  else setZoomAt(zoom.s * k);
}
document.getElementById("zIn").onclick  = function(){ pause(); zoomBy(1.4); };
document.getElementById("zOut").onclick = function(){ pause(); zoomBy(1 / 1.4); };
document.getElementById("zFit").onclick = function(){ pause(); resetView(); resetSide(); };
document.getElementById("zRst").onclick = function(){ pause(); setZoomAt(1 / fitScale); setZoomAt2(1 / fitScale); };` },
    /* 并排对照的宽度阈值：老回放页要求 iframe 宽 ≥1000，而 iframe ≈ 窗口宽 − 416，
       换算下来窗口得 1416 以上 —— 1280 / 1366 这类常见笔记本永远进不了并排对照。
       降到 800 与内置演示页、以及 tools/svg2canvas.py 现在的取值保持一致。 */
    { from: 'return ghostOn && window.innerWidth>=1000 && (window.innerWidth/window.innerHeight)>=1.0;',
      to:   'return ghostOn && window.innerWidth>=800 && (window.innerWidth/window.innerHeight)>=1.0;' },
  ];

/* ---------- 规则引擎（从 web/index.html 逐字搬来） ---------- */
  /* 「这条规则是不是已经打过了」——把 to 里相对 from 新增的**代码行**按连续段切块
     （注释行不计：规则里的注释常是手写简写版，和模板里的详细注释对不上，
      拿带注释的整段去比对必然失配，于是重复插入），每个块都在输出里找得到才算打过。

     为什么不能用单个哨兵串：短的一行代码在文档别处也会出现 ——
     ghostEl.style.display=ghostOn?"block":"none"; 在「对照」按钮的点击处理里就有，
     拿它当哨兵会把「还没打过的页面」误判成「打过了」，双栏隐藏对照层这种修复就永远加不上。
     反过来也不能「任意一块命中就算打过」：旧页面可能碰巧包含其中一块。
     所以：候选块**全部**命中才算打过。 */
  function newBlocks(r){
    if(r.sentinel) return [r.sentinel];
    const toLines = String(r.to).split('\n');
    const fromTrim = new Set((r.re ? '' : String(r.from))
      .split('\n').map(s => s.trim()).filter(Boolean));
    /* 只按行首判断是不是注释行：行尾挂注释的代码行（var fitW=0, fitH=0; 加一段块注释）
       是货真价实的代码，不能连它一起排除 —— 排掉之后这条规则就认不出自己已经打过了 */
    const isComment = (t) => !t || t.startsWith('/*') || t.startsWith('*') || t.startsWith('//');
    const blocks = [];
    let cur = [];
    for(const ln of toLines){
      const t = ln.trim();
      /* 收进块的是**原始行（含缩进）**：块最终要拿去在未改动的原文里 indexOf，
         带着缩进才找得到。这里踩过坑 —— 曾经 push 的是 trim 过的行，
         结果哨兵永远匹配不上，「已经打过」恒为 false，同一条规则被反复应用。 */
      if(t && !isComment(t) && !fromTrim.has(t) && !/\$\d/.test(t)) cur.push(ln);
      else { if(cur.length) blocks.push(cur.join('\n')); cur = []; }
    }
    if(cur.length) blocks.push(cur.join('\n'));
    let res = blocks.filter(b => b.trim().length >= 20);
    /* 兜底：正则规则的 to 常整行都带 $1/$2，退回「不含 $n 的长片段」 */
    if(!res.length){
      res = String(r.to).split(/\$\d+/)
        .map(s => s.replace(/^\n+/, '').replace(/\n+$/, ''))
        .filter(s => s.trim().length >= 20 && !fromTrim.has(s.trim()) && !isComment(s.trim()));
    }
    return res;
  }
  function alreadyApplied(out, r){
    const blocks = newBlocks(r);
    return blocks.length > 0 && blocks.every(b => out.indexOf(b) >= 0);
  }

  function upgradeLayout(text){
    let out = text, hit = 0;
    for(const r of LAYOUT_RULES){
      if(alreadyApplied(out, r)) continue;            // 这条的效果已经在了，别重复打
      if(r.re){
        if(!r.re.test(out)) continue;                 // 这条对不上：跳过，不放弃整轮
        out = out.replace(r.re, r.to); hit++;
      }else{
        // 恰好命中一处才动手。用两次 indexOf 代替 split().length：split 会为
        // 十几 MB 的正文复制一整份数组，39 条规则跑下来主线程会明显卡顿。
        const i0 = out.indexOf(r.from);
        if(i0 < 0 || out.indexOf(r.from, i0 + r.from.length) >= 0) continue;
        out = out.replace(r.from, r.to); hit++;
      }
    }
    return hit ? out : null;
  }


/* ---------- 底板色修复（纯文本版；原 async 版按 Blob 读 SVG 头部，这里改由调用方传入） ---------- */
function fixBaseColorText(htmlText, svgHead){
  /* 早期回放页把底板写死成浅灰（#f0f0f0），深色图里「与底色相近、被整簇跳过的块」
     会露成满屏白斑。SVG 头部有权威的底板色（生成时算的 <rect fill="…">），
     从这里读出来替换。读不到、或 HTML 里找不到那两处写死值，就原样返回（不改）。 */
  try {
    if (!svgHead) return null;
    var m = String(svgHead).match(/<rect width="[\d.]+" height="[\d.]+" fill="(#[0-9a-fA-F]{6})"/);
    if (!m) return null;
    var bg = m[1];
    var hit = 0, out = htmlText;
    out = out.replace(/(clearAll\(\)\{ ctx\.fillStyle=")#[0-9a-fA-F]{6}(")/,
                      function (_, a, b) { hit++; return a + bg + b; });
    out = out.replace(/(canvas\{display:block;width:100%;height:100%;background:)#[0-9a-fA-F]{6}/,
                      function (_, a) { hit++; return a + bg; });
    return hit ? out : null;
  } catch (_) { return null; }
}

/* ---------- 时间轴升级（从 web/index.html 的 upgradeAxis ① 搬来，纯文本） ---------- */
function upgradeTimeline(text){
  /* 2026-09-12：线稿从「固定 15%」改成按视觉重量自然播。两种旧形态：
     · 已是新结构（√公式）但兜底值写成了 0.15 → 改成 0；
     · 老结构（AXIS_OLD）→ 整段换成 AXIS_NEW。 */
  var out = text;
  if (out.indexOf('Math.sqrt(AREA[i])') >= 0) {
    if (out.indexOf('SKETCH_T=0.15') >= 0) {
      return out.replace('SKETCH_T=0.15', 'SKETCH_T=0');
    }
    return null;
  }
  if (out.indexOf(AXIS_OLD) >= 0) {
    return out.replace(AXIS_OLD, AXIS_NEW).replace(/SKETCH_T=[0-9.]+/, 'SKETCH_T=0');
  }
  return null;
}

/* ---------- 总入口：把一份回放页文本升到当前版本 ----------
   返回 { text, version, changed, notes }。
   · text    升级后的文本（可能原样）
   · version 升级后的版本号（有戳产物读到几就是几；无戳产物补到 LEGACY_TARGET 再走链）
   · changed 文本有没有被改过（含行尾归一化与补戳）
   · notes   迁移链的说明（缺环 / 未命中），调用方自行 console.warn
   svgHead = SVG 文件开头 1MB 的文本（用于底板色修复），可为 null。 */
function upgradeReplay(text, svgHead){
  if (!text) return { text: text, version: 0, changed: false, notes: [] };
  var out = text, notes = [];
  /* 行尾归一化：Windows 产出的 \r\n 会让所有规则 / AXIS 文本对不上（它们都是 LF）。
     不归一化，本机生成的历史记录一条都命中不了。HTML 不在乎行尾，改掉无副作用。 */
  if (out.indexOf('\r\n') >= 0) out = out.replace(/\r\n/g, '\n');

  var ver = readVersion(out);
  if (ver === 0) {
    /* 无戳：模糊规则能升多少升多少，升完补戳（等价版本 = LEGACY_TARGET） */
    var t1 = upgradeTimeline(out); if (t1 != null) out = t1;
    var t2 = upgradeLayout(out);   if (t2 != null) out = t2;
    out = stampVersion(out, LEGACY_TARGET);
    ver = LEGACY_TARGET;
  }
  if (ver < GEN_LATEST) {
    var r = applyMigrations(out, ver, GEN_LATEST, MIGRATIONS);
    out = r.text; ver = r.version;
    notes = notes.concat(r.notes);
  }
  /* 底板色修复：对新产物是 no-op；对老产物把写死的浅底换回 SVG 里的权威值 */
  if (svgHead) {
    var bc = fixBaseColorText(out, svgHead);
    if (bc != null) out = bc;
  }
  return { text: out, version: ver, changed: out !== text, notes: notes };
}

return {
  GEN_LATEST: GEN_LATEST,
  LEGACY_TARGET: LEGACY_TARGET,
  readVersion: readVersion,
  stampVersion: stampVersion,
  upgradeReplay: upgradeReplay,
  /* 测试钩子（tools/test_upgrade.js 用；页面代码不要依赖） */
  _t: {
    applyMigrations: applyMigrations,
    upgradeLayout: upgradeLayout,
    upgradeTimeline: upgradeTimeline,
    fixBaseColorText: fixBaseColorText,
    LAYOUT_RULES: LAYOUT_RULES,
    AXIS_OLD: AXIS_OLD,
    AXIS_NEW: AXIS_NEW
  }
};
});
