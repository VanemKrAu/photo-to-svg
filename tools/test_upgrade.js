#!/usr/bin/env node
/* test_upgrade.js —— 历史回放页升级的黄金样本回归测试
 *
 * 为什么需要它：升级层出过两个「静默失败」型事故（docs/踩坑记录.md #18 #19）——
 * 页面看着能用、日志没有错，只是升级没生效 / 重复打了补丁。这类问题只有真跑一遍
 * 「升级后的产物」才抓得到。本测试把六个世代的真实回放页（tests/fixtures/，由
 * tools/make_upgrade_fixtures.py 从 git 历史生成）逐个升级，断言：
 *
 *   1. 版本戳：升级后 == GEN_LATEST，且全篇只有一个戳；
 *   2. 幂等：从「升级结果（去戳）」再升一遍，与从原件升一遍完全一致
 *      —— 即 legacy 规则自身不重复插入（#19 的幂等断言就是干这个的）；
 *   3. 防重复哨兵：var fitW / #fsExit / id="view" 的计数与最新模板一致；
 *   4. 逐样本特性：老世代该拿到的升级（√时间轴、相册式 view、并排阈值 800）都拿到；
 *   5. 迁移链机制：多级链 / 未命中推进 / 缺环停止 / 异常兜底（假迁移单元验证）。
 *
 * 用法：node tools/test_upgrade.js
 * 退出码：0 = 全过；1 = 有失败。配合 tools/test_upgrade.py 可再跑浏览器级验证。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const FIX = path.join(ROOT, "tests", "fixtures");
const U = require(path.join(ROOT, "web", "upgrade.js"));

let pass = 0, fail = 0;
const ok = (m) => { pass++; console.log("  \u2713 " + m); };
const bad = (m) => { fail++; console.log("  \u2717 " + m); };
const eq = (a, b, m) => a === b ? ok(m + "（" + a + "）")
                                 : bad(m + "：期望 " + b + "，实际 " + a);
const truthy = (c, m) => c ? ok(m) : bad(m);
const count = (t, re) => (t.match(re) || []).length;

/* 升级结果里「戳」的总数 == 1；stripStamp 用于幂等比较（把戳还原掉再比） */
const stripStamp = (t) => t.replace(/<!--\s*p2sv-gen:\s*v\d+\s*-->\n/, "");

/* 防重复哨兵：升级后应达到「与最新模板一致」的计数（人工核对过，改模板时同步） */
const SENTINELS = [
  [/var fitW/g, 1, "var fitW 声明唯一"],
  [/#fsExit/g, 2, "#fsExit 样式两条"],
  [/id="view"/g, 1, "id=\"view\" 唯一"],
];
const shouldGone = [
  [/SKETCH_T=0\.15/g, "旧兜底值 SKETCH_T=0.15 被清除"],
  [/var LW = SKETCH_T>0/g, "旧时间轴代码段被替换"],
];

/* 样本清单：文件 → 期望拿到的升级特性（s1 最老，拿不全缩放系统是已知边界，见 README） */
const FEATURES = {
  "s1-29770ea.html": ["sqrt", "view", "w800"],
  "s2-f742973.html": ["sqrt", "view", "w800"],
  "s3-0f648ca.html": ["view", "w800"],
  "s4-eaa6ea5.html": ["view", "w800"],
  "s5-9ab8b88.html": ["view", "w800"],
  "s6-v1.html": [],
};
const FEAT_RE = {
  sqrt: [/Math\.sqrt\(AREA\[i\]\)/g, "新时间轴（√面积）"],
  view: [/id="view"/g, "相册式 view 容器"],
  w800: [/innerWidth>=800/g, "并排对照阈值 800"],
};

function testUnits() {
  console.log("\n[1/3] 单元：版本戳");
  eq(U.readVersion("<html>无戳</html>"), 0, "无戳文本读出 0");
  const s = U.stampVersion("<head>\n<title>x</title>", 1);
  eq(U.readVersion(s), 1, "stamp 后读出版本 1");
  eq(U.stampVersion(s, 1), s, "再 stamp 一次不变（幂等）");
  eq(count(s, /p2sv-gen/g), 1, "只有一个戳");

  console.log("\n[2/3] 单元：迁移链");
  const migs = [
    { from: 1, to: 2, apply: (t) => t.replace("AAA", "BBB") },
    { from: 2, to: 3, apply: (t) => t.replace("BBB", "CCC") },
  ];
  let r = U._t.applyMigrations("xAAAy", 1, 3, migs);
  eq(r.version, 3, "多级链走到 3");
  eq(r.text, "xCCCy", "多级链逐级替换");
  r = U._t.applyMigrations("nomatch", 1, 3, [
    { from: 1, to: 2, apply: () => null },              // 显式未命中：约定返回 null
    { from: 2, to: 3, apply: (t) => t + "!" },
  ]);
  eq(r.version, 3, "未命中仍推进版本");
  eq(r.text, "nomatch!", "未命中的一级跳过后，后续迁移照常应用");
  truthy(r.notes.length >= 1 && /未命中/.test(r.notes[0]), "未命中记录 note");
  r = U._t.applyMigrations("xAAAy", 2, 4, [migs[0]]);   // 2→3 缺环
  eq(r.version, 2, "缺环时停在缺环处");
  truthy(/缺/.test(r.notes[0] || ""), "缺环记录 note");
  r = U._t.applyMigrations("x", 1, 2, [{ from: 1, to: 2, apply: () => { throw new Error("boom"); } }]);
  eq(r.version, 2, "迁移异常仍推进（不卡死）");
  truthy(/异常/.test(r.notes[0] || ""), "异常记录 note");
  r = U._t.applyMigrations("x", 3, 3, migs);
  eq(r.version, 3, "已是最新版：零动作");
}

function testSamples() {
  console.log("\n[3/3] 样本：六个世代逐个升级");
  const files = Object.keys(FEATURES);
  for (const f of files) {
    const p = path.join(FIX, f);
    if (!fs.existsSync(p)) {
      bad(f + " 不存在（跑 tools/make_upgrade_fixtures.py 重新生成）");
      continue;
    }
    const html = fs.readFileSync(p, "utf8");
    const r = U.upgradeReplay(html, null);
    console.log("\n  ── " + f + " ──");

    /* 版本戳与幂等 */
    eq(r.version, U.GEN_LATEST, "升级后版本 = GEN_LATEST");
    eq(count(r.text, /p2sv-gen/g), 1, "全篇一个戳");
    truthy(U.upgradeReplay(html, null).text === r.text, "同一输入两次升级结果一致（确定性）");

    /* 幂等：从升级结果（去戳）再升一遍 == 直接从原件升一遍（都去戳后比较） */
    const r2 = U.upgradeReplay(stripStamp(r.text), null);
    truthy(stripStamp(r2.text) === stripStamp(r.text), "升级结果再升一遍：完全不变（幂等）");

    /* 防重复哨兵 */
    for (const [re, want, label] of SENTINELS) eq(count(r.text, re), want, label);
    for (const [re, label] of shouldGone) eq(count(r.text, re), 0, label);

    /* 结构不破坏 */
    truthy(r.text.indexOf("</html>") >= 0, "页面结构完整");
    truthy(/<canvas id="art"/.test(r.text), "canvas 在");

    /* 逐样本特性 */
    if (f === "s6-v1.html") {
      truthy(r.changed === false && r.text === html, "带戳最新版：零改动直通");
    }
    for (const key of FEATURES[f]) {
      const [re, label] = FEAT_RE[key];
      truthy(count(r.text, re) >= 1, "拿到升级：" + label);
    }
  }
}

console.log("历史回放页升级测试 —— " + path.relative(ROOT, FIX));
testUnits();
testSamples();
console.log("\n" + "-".repeat(52));
if (fail) {
  console.log("失败 " + fail + " 项（通过 " + pass + " 项）");
  process.exit(1);
} else {
  console.log("全部通过（" + pass + " 项断言）");
}
