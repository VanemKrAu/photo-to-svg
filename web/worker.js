/* worker.js —— 在 Web Worker 里跑 Pyodide，主线程不会卡住。
 *
 * 流程：加载 Pyodide（约 30 MB，首次较慢，之后走浏览器缓存）
 *      → 装 numpy / opencv-python / Pillow（Pyodide 自带预编译版）
 *      → 把 tools/ 与 scripts/ 下的 Python 脚本写进内存文件系统
 *      → 调用 web_run.run() 跑完整流水线
 *      → 把产物（SVG / HTML）以 Uint8Array 传回主线程
 *
 * 踩过的坑：
 *  1. loadPyodide() 在 Worker 里推导不出 indexURL，会去当前站点找 .whl → 404，
 *     而 loadPackage 对 404 是「静默跳过不报错」，最后炸在 import cv2。
 *     必须显式传 indexURL，并逐个 CDN 兜底。
 *  2. 装完包一定要真的 import 一遍验证，否则错误会推迟到用的时候才暴露。
 */

const PYODIDE_VERSION = 'v314.0.6';
const PKGS = ['numpy', 'opencv-python', 'Pillow'];

/* 按顺序尝试的 CDN。实测（2026-09）：
 *   cdn.jsdelivr.net/pyodide/<ver>/full/   快且稳（Pyodide 官方文档推荐入口）
 *   cdn.jsdelivr.net/npm/pyodide@<ver>/    同一台 CDN 的 npm 路径，作为换路径重试
 *   unpkg.com/pyodide@<ver>/               备选 CDN；注意是包根目录，不带 full/
 * 网络抖动很常见（一次 Failed to fetch 就会整条线路作废），所以每条都重试。
 */
const CDN_BASES = [
  `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`,
  `https://cdn.jsdelivr.net/npm/pyodide@${PYODIDE_VERSION.replace('v', '')}/`,
  `https://unpkg.com/pyodide@${PYODIDE_VERSION.replace('v', '')}/`,
];
const RETRY_PER_BASE = 2;

/* 需要写进虚拟文件系统的文件（路径相对于站点根目录） */
const APP_FILES = [
  'web_run.py',
  'tools/build_svg_art.py',
  'tools/svg2canvas.py',
  'scripts/css_art/__init__.py',
  'scripts/css_art/audit.py',
  'scripts/css_art/cli.py',
  'scripts/css_art/geometry.py',
  'scripts/css_art/quality.py',
  'scripts/css_art/regions.py',
  'scripts/css_art/render.py',
];

let py = null;
const post = (msg) => self.postMessage(msg);

/* ---------------- 本地缓存（Cache Storage API） ----------------
 * 为什么不靠浏览器 HTTP 缓存：
 *   CDN 给的确实是 max-age=31536000，但 HTTP 缓存由浏览器全权管理 ——
 *   手机浏览器/应用内 WebView/隐私模式都可能在后台清掉它，
 *   30 MB 的体积也容易被当成「可回收」优先淘汰。
 * 所以这里用 Cache Storage 显式存一份（配额通常几百 MB 起，且只有我们自己能删）。
 * 效果：第二次打开直接从磁盘读，不再走网络。
 */
const CACHE_NAME = 'pyodide-' + PYODIDE_VERSION;
const CACHE_HOSTS = /^https:\/\/(cdn\.jsdelivr\.net|unpkg\.com)\//;
let cache = null;

const netStats = { files: 0, bytes: 0, cached: 0 };

async function openCache() {
  if (cache) return cache;
  if (!self.caches) return null;          // 极老的浏览器没有 Cache Storage
  try { cache = await caches.open(CACHE_NAME); } catch (_) { cache = null; }
  return cache;
}

/* 拦下 Pyodide 自己的 fetch：先查缓存，未命中再走网络并存起来 */
function installCacheLayer() {
  const originalFetch = self.fetch.bind(self);
  self.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input
              : (input && input.url) ? input.url : String(input);
    const c = await openCache();
    // 非 CDN 资源（本站脚本）不缓存；带 Range 的请求也不能直接用整份缓存顶替
    const rangeHeader = init && init.headers &&
      (init.headers.get ? init.headers.get('range') : init.headers.range);
    if (!c || !CACHE_HOSTS.test(url) || rangeHeader) {
      return originalFetch(input, init);
    }
    try {
      const hit = await c.match(url);
      if (hit) {
        netStats.cached++;
        post({ type: 'cache', state: 'hit', url, stats: netStats });
        return hit;
      }
    } catch (_) {}
    const resp = await originalFetch(input, init);
    if (resp && resp.ok) {
      const len = Number(resp.headers.get('content-length') || 0);
      netStats.files++; netStats.bytes += len;
      post({ type: 'cache', state: 'miss', url, bytes: len, stats: netStats });
      // 后台写入，不阻塞 Pyodide 使用这个响应
      c.put(url, resp.clone()).catch(() => {});
    }
    return resp;
  };
}

/* 查缓存里已经存了多少（主线程也能查，用来在界面上显示） */
async function cacheSummary() {
  const c = await openCache();
  if (!c) return { count: 0, bytes: 0, supported: false };
  try {
    const keys = await c.keys();
    let bytes = 0;
    for (const req of keys) {
      const r = await c.match(req);
      if (r) bytes += Number(r.headers.get('content-length') || 0);
    }
    return { count: keys.length, bytes, supported: true };
  } catch (_) { return { count: 0, bytes: 0, supported: false }; }
}

/* 带进度回调的取文件（运行时 30 MB，得让用户看到在动） */
async function fetchBytes(url, label) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`取不到 ${label}（HTTP ${r.status}）`);
  const total = Number(r.headers.get('content-length') || 0);
  if (!r.body || !total) return new Uint8Array(await r.arrayBuffer());

  const reader = r.body.getReader();
  const chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    post({ type: 'dl', label, got, total });
  }
  const out = new Uint8Array(got);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

/* 带超时的 fetch：网络卡住时不至于无限等 */
function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label + ' 超时')), ms)),
  ]);
}

async function bootRuntime() {
  const attempts = [];
  let lastErr = null;
  for (let i = 0; i < CDN_BASES.length; i++) {
    for (let r = 1; r <= RETRY_PER_BASE; r++) {
      const base = CDN_BASES[i];
      const tag = `线路 ${i + 1}${RETRY_PER_BASE > 1 ? '-' + r : ''}`;
      try {
        post({ type: 'status', text: `正在加载 Python 运行时…（${tag}/${CDN_BASES.length}）` });
        const mod = await withTimeout(
          import(/* @vite-ignore */ base + 'pyodide.mjs'), 90000, '取 pyodide.mjs');
        const runtime = await withTimeout(mod.loadPyodide({
          indexURL: base,
          stdout: (s) => post({ type: 'log', text: s }),
          stderr: (s) => post({ type: 'log', text: s }),
        }), 180000, '初始化运行时');
        return runtime;
      } catch (e) {
        lastErr = e;
        attempts.push(tag + '：' + (e && e.message || e));
        post({ type: 'status', text: `${tag} 失败，重试…` });
      }
    }
  }
  throw new Error(
    'Python 运行时加载失败（试过 ' + attempts.length + ' 次）。\n' +
    '多半是网络取不到 Pyodide 的预编译包，请检查网络（或换个网络）后刷新重试。\n' +
    '明细：\n' + attempts.join('\n'));
}

async function boot() {
  installCacheLayer();
  const pre = await cacheSummary();
  post({ type: 'cacheinfo', ...pre });
  if (pre.count) {
    post({ type: 'status', text: `运行时已在本地缓存（${pre.count} 个文件），直接读取…` });
  }
  py = await bootRuntime();

  post({ type: 'status', text: '正在加载 numpy / OpenCV / Pillow…' });
  // 逐个装，某个包失败时能立刻指出是哪个
  for (const name of PKGS) {
    post({ type: 'status', text: `正在加载 ${name}…` });
    await py.loadPackage(name, {
      errorCallback: (m) => post({ type: 'log', text: '[加载] ' + m }),
    });
  }

  // 关键：装完必须真的导入一次，否则错误会推迟到生成时才暴露
  post({ type: 'status', text: '正在校验 Python 环境…' });
  const check = py.runPython(`
import json
_ok = True
_info = {}
for _mod, _name in [('numpy','numpy'), ('cv2','opencv-python'), ('PIL','Pillow')]:
    try:
        _m = __import__(_mod)
        _info[_name] = getattr(_m, '__version__', '?')
    except Exception as _e:
        _ok = False
        _info[_name] = '缺失：%s' % _e
json.dumps({'ok': _ok, 'info': _info})
`);
  const res = JSON.parse(check);
  if (!res.ok) {
    const missing = Object.entries(res.info)
      .filter(([, v]) => String(v).startsWith('缺失')).map(([k]) => k);
    throw new Error('这些包没装上：' + missing.join('、') +
      '。多半是网络取不到 Pyodide 的预编译包，请检查网络后重试。');
  }
  post({ type: 'versions', info: res.info });

  post({ type: 'status', text: '正在写入脚本…' });
  py.FS.mkdirTree('/app/tools');
  py.FS.mkdirTree('/app/scripts/css_art');
  py.FS.mkdirTree('/work');
  for (const rel of APP_FILES) {
    py.FS.writeFile('/app/' + rel, await fetchBytes(
      new URL(rel, self.location.href).href, rel));
  }

  py.runPython(`
import sys, json
sys.path.insert(0, '/app')
import web_run

def _emit(msg):
    _JS_EMIT(msg)

def _go(cfg):
    return web_run.run(cfg, _emit)
`);
  post({ type: 'ready' });
}

self.onmessage = async (ev) => {
  const m = ev.data;
  try {
    if (m.type === 'cachesummary') {
      post({ type: 'cacheinfo', ...(await cacheSummary()) });
      return;
    }
    if (m.type === 'clearcache') {
      let freed = 0;
      try {
        const c = await openCache();
        if (c) {
          const keys = await c.keys();
          for (const req of keys) {
            const r = await c.match(req);
            if (r) freed += Number(r.headers.get('content-length') || 0);
          }
          for (const req of keys) await c.delete(req);
        }
      } catch (_) {}
      post({ type: 'cachecleared', freed });
      return;
    }
    if (m.type === 'boot') {
      if (!py) await boot();
      else post({ type: 'ready' });
      return;
    }

    if (m.type === 'run') {
      py.globals.set('_JS_EMIT', (msg) => post({ type: 'log', text: String(msg) }));
      py.FS.mkdirTree('/work');
      const fname = '/work/input' + (m.ext || '.png');
      py.FS.writeFile(fname, new Uint8Array(m.data));

      const cfg = JSON.stringify({
        input: fname,
        name: m.name || 'artwork',
        title: m.title || '逐笔绘制回放',
        duration: m.duration || 90,
        outline: m.outline ?? 0.15,
        epsilon: m.epsilon ?? 0.25,
        linewidth: m.linewidth ?? 2.6,
        max_width: m.maxWidth || 0,
        preprocess: m.preprocess || 'auto',
      });

      // 用 globals 传参，别把 JSON 拼进 Python 源码：
      // 标题里只要有一个引号或反斜杠，拼字符串就会破。
      py.globals.set('_JS_CFG', cfg);
      const res = JSON.parse(py.runPython('_go(_JS_CFG)'));

      const svg = py.FS.readFile(res.svg);
      const html = py.FS.readFile(res.html);
      // readFile 返回的 Uint8Array 有可能共用一块更大的 buffer，
      // 直接把 .buffer 发出去会带上多余字节 → Blob 损坏。长度对不上就切一份。
      const exact = (u) => (u.byteLength === u.buffer.byteLength ? u.buffer : u.slice().buffer);
      const svgBuf = exact(svg), htmlBuf = exact(html);
      post({
        type: 'done',
        strokes: res.strokes,
        size: res.size,
        svgBytes: res.svg_bytes,
        htmlBytes: res.html_bytes,
        svg: svgBuf,
        html: htmlBuf,
      }, [svgBuf, htmlBuf]);
      return;
    }
  } catch (err) {
    post({ type: 'error', text: String(err && err.message || err) });
  }
};
