/* worker.js —— 在 Web Worker 里跑 Pyodide，主线程不会卡住。
 *
 * 流程：加载 Pyodide（约 30 MB，首次较慢，之后走浏览器缓存）
 *      → 装 numpy / opencv-python / Pillow（Pyodide 自带预编译版）
 *      → 把 tools/ 与 scripts/ 下的 Python 脚本写进内存文件系统
 *      → 调用 web_run.run() 跑完整流水线
 *      → 把产物（SVG / HTML）以 Uint8Array 传回主线程
 */
import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/pyodide.mjs';

const PKGS = ['numpy', 'opencv-python', 'Pillow'];

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

async function boot() {
  post({ type: 'status', text: '正在加载 Python 运行时（首次约 30 MB，之后走缓存）…' });
  py = await loadPyodide();

  post({ type: 'status', text: '正在加载 numpy / OpenCV / Pillow…' });
  await py.loadPackage(PKGS);

  post({ type: 'status', text: '正在写入脚本…' });
  py.FS.mkdirTree('/app/tools');
  py.FS.mkdirTree('/app/scripts/css_art');
  py.FS.mkdirTree('/work');
  for (const rel of APP_FILES) {
    const r = await fetch(new URL(rel, self.location.href).href);
    if (!r.ok) throw new Error('取不到 ' + rel + '（HTTP ' + r.status + '）');
    const buf = new Uint8Array(await r.arrayBuffer());
    py.FS.writeFile('/app/' + rel, buf);
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
    if (m.type === 'boot') {
      if (!py) await boot();
      else post({ type: 'ready' });
      return;
    }

    if (m.type === 'run') {
      py.globals.set('_JS_EMIT', (msg) => post({ type: 'log', text: String(msg) }));
      py.FS.mkdirTree('/work');
      py.FS.writeFile('/work/input' + (m.ext || '.png'), new Uint8Array(m.data));

      const cfg = JSON.stringify({
        input: '/work/input' + (m.ext || '.png'),
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
      const out = py.runPython('_go(_JS_CFG)');
      const res = JSON.parse(out);

      // 取出产物（readFile 返回 Uint8Array，转成可转移的 buffer 发回去）
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
