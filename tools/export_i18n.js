#!/usr/bin/env node
/*
 * 把 i18n.js 里的 ZH2EN 词典 + RULES 正则导出成各平台能吃的资源文件。
 *
 * 为什么要有这个脚本：手机版是原生（SwiftUI / Compose），用不上 i18n.js 里那个
 * DOM walker，但 **ZH2EN 和 RULES 本身是纯数据 / 纯逻辑，与 DOM 无关**。
 * 手工抄 594 条一定会漂移，而且桌面端以后还会加词条 —— 所以做成脚本。
 *
 * 用法：
 *   node tools/export_i18n.js                    # 输出到 build/i18n/
 *   node tools/export_i18n.js --out /tmp/x       # 指定输出目录
 *   node tools/export_i18n.js --check            # 只校验不写文件（CI 用）
 *
 * 产物：
 *   en.lproj/Localizable.strings   iOS，**中文原文即 key**
 *   zh-Hans.lproj/Localizable.strings   恒等映射（放进去是为了让 Xcode 认这个语言）
 *   i18n_en.json                   { "中文": "English" }，Android / 任意运行时用
 *   rules.json                     正则规则（纯字符串模板可导出）
 *   REPORT.md                      哪些 RULES 用了函数、必须手工移植
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const SRC = path.join(ROOT, 'i18n.js');

// ---------- 参数 ----------
const argv = process.argv.slice(2);
function argOf(name, dflt) {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const OUT = path.resolve(argOf('--out', path.join(ROOT, 'build', 'i18n')));
const CHECK_ONLY = argv.includes('--check');

// ---------- 1. 从 i18n.js 里抠出 ZH2EN 与 RULES ----------
// 用一个极小的 shim 顶替 window/document，然后把文件当普通脚本跑起来。
// i18n.js 是 IIFE，不会真的碰 DOM 直到 setLang 被调用，所以这样是安全的。
function loadDict() {
  let code = fs.readFileSync(SRC, 'utf8');

  // i18n.js 末尾会把 API 挂到 window.I18N 上，给个假的就行。
  // body 给 null —— 文件最后一行是 `if (document.body) applyNode(document.body)`，
  // 给个空对象它会真的去遍历，直接抛错。
  const sandbox = {
    window: {},
    document: {
      documentElement: { lang: '', setAttribute() {} },
      body: null,
      title: '',
      addEventListener() {},
      querySelectorAll: () => [],
    },
    MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
    navigator: { language: 'zh' },
    console,
  };
  sandbox.globalThis = sandbox;

  // ★ ZH2EN / RULES 是 IIFE 内部变量，外面追加代码抓不到（ReferenceError）。
  //   所以把捕获语句注入到 IIFE **内部** —— 也就是替换掉最后那个 `})();`。
  const tail = '})();';
  const at = code.lastIndexOf(tail);
  if (at < 0) throw new Error('i18n.js 结构变了：找不到结尾的 ' + tail);
  code = code.slice(0, at)
    + '  globalThis.__CAPTURED = { ZH2EN: ZH2EN, RULES: RULES, SKIP_SEL: SKIP_SEL };\n'
    + code.slice(at);

  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: SRC });

  const cap = sandbox.__CAPTURED;
  if (!cap || cap.err) throw new Error('抓取 ZH2EN/RULES 失败：' + (cap && cap.err));
  return cap;
}

// ---------- 2. .strings 转义 ----------
// Apple 的 .strings 是「C 风格字符串 + 分号」：
//   反斜杠、双引号要转义；换行写成 \n；制表符写成 \t
function stringsEscape(s) {
  return String(s)
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\r\n/g, '\\n')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\n')
    .replace(/\t/g, '\\t');
}

// ---------- 3. XML 转义（Android） ----------
function xmlEscape(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '\\\'');
}

// ---------- 4. 主流程 ----------
function main() {
  const { ZH2EN, RULES } = loadDict();

  const entries = Object.keys(ZH2EN).map((zh) => ({ zh, en: ZH2EN[zh] }));

  // 健康检查。判据要挑准，否则一堆误报会让这个检查被无视：
  //   · 词条里**大量是「行内碎片」**（被 <b> 切开的半句话），它们的译文本来就可能是
  //     「一个空格」或「本来就该是英文」——那是设计，不是问题。
  //   · 真正值得报的只有两类：译文**完全为空**、以及**中文词条却没被翻译**。
  const CJK = /[\u4e00-\u9fff]/;
  const problems = [];
  const whitespaceOnly = [];
  const enSeen = new Map();
  for (const e of entries) {
    const keyHasCJK = CJK.test(e.zh);
    // ★ 「英文译文是空串」**不能当问题报** —— 这是本项目的合法手法：
    //   英文语序和中文不同时，被切碎的行内碎片在英文那边就该是空的。
    //   例：`共 <b>6</b> 条索引记录` → "共 " 译成 ""，因为英文是 "6 index records"，
    //       前面不需要词；`以 /v1 结尾` → " 结尾" 译成 ""，因为 "ending with" 已经在前半句了。
    //   所以只进「待复核」列表，不算失败。
    if (keyHasCJK && e.en !== '' && e.en === e.zh) {
      problems.push(`中文词条未翻译: ${JSON.stringify(e.zh).slice(0, 70)}`);
    } else if (keyHasCJK && e.en.trim() === '') {
      whitespaceOnly.push(e);   // 空串或纯空白 —— 待复核，非失败
    }
    if (enSeen.has(e.en)) enSeen.get(e.en).push(e.zh);
    else enSeen.set(e.en, [e.zh]);
  }

  // RULES：能导出的（模板是纯字符串）与不能导出的（模板是函数）
  const simpleRules = [];
  const fnRules = [];
  RULES.forEach((r, i) => {
    // ★ 不能用 instanceof —— RULES 是在 vm 沙箱里造的，
    //   它的 RegExp 构造函数和本模块的不是同一个，instanceof 永远 false。
    if (!Array.isArray(r) || Object.prototype.toString.call(r[0]) !== '[object RegExp]') return;
    const tpl = r[1];
    const item = { pattern: r[0].source, flags: r[0].flags, replace: typeof tpl === 'string' ? tpl : null };
    if (typeof tpl === 'string') simpleRules.push(item);
    else fnRules.push({ index: i, pattern: r[0].source, flags: r[0].flags });
  });

  // ---- 产物 1：iOS en.lproj ----
  const iosLines = [
    '/* 由 tools/export_i18n.js 从 wemm_app/i18n.js 自动生成 —— 不要手工编辑。',
    ' * 改文案请改 i18n.js 的 ZH2EN，然后重新运行这个脚本。',
    ' *',
    ' * ★ 中文原文就是 key。Swift 侧写 NSLocalizedString("搜索全部素材…", comment: "")，',
    ' *   英文包命中这张表，中文包自动回退到 key 本身（也就是中文）——',
    ' *   所以 zh-Hans.lproj 里不需要重复一遍，但为了 Xcode 认这个语言还是放一份恒等映射。',
    ` * 词条数：${entries.length}`,
    ' */',
    '',
  ];
  for (const e of entries) {
    iosLines.push(`"${stringsEscape(e.zh)}" = "${stringsEscape(e.en)}";`);
  }
  iosLines.push('');

  // ---- 产物 2：iOS zh-Hans（恒等映射）----
  const iosZhLines = [
    '/* 由 tools/export_i18n.js 自动生成 —— 不要手工编辑。',
    ' * 恒等映射：中文 key → 中文。放着是为了让 Xcode / 系统认这个本地化。',
    ` * 词条数：${entries.length}`,
    ' */',
    '',
  ];
  for (const e of entries) {
    iosZhLines.push(`"${stringsEscape(e.zh)}" = "${stringsEscape(e.zh)}";`);
  }
  iosZhLines.push('');

  // ---- 产物 3：通用 JSON（Android / 任意运行时）----
  const json = {};
  for (const e of entries) json[e.zh] = e.en;

  // ---- 产物 4：rules.json ----
  const rulesOut = {
    _note: '由 tools/export_i18n.js 从 wemm_app/i18n.js 的 RULES 导出。' +
           'replace 里用 $1/$2 引用捕获组，与 JS String.replace 语义一致。' +
           'regexReplace 为 null 的表示原实现是函数，见 REPORT.md。',
    count: simpleRules.length,
    rules: simpleRules,
  };

  // ---- 产物 5：REPORT.md ----
  const rep = [];
  rep.push('# i18n 导出报告');
  rep.push('');
  rep.push(`源文件：\`i18n.js\``);
  rep.push('');
  rep.push('| 项目 | 数量 |');
  rep.push('| --- | --- |');
  rep.push(`| ZH2EN 词条 | ${entries.length} |`);
  rep.push(`| RULES 总数 | ${RULES.length} |`);
  rep.push(`| RULES 可导出（字符串模板） | ${simpleRules.length} |`);
  rep.push(`| **RULES 需手工移植（函数模板）** | **${fnRules.length}** |`);
  rep.push(`| 词典问题 | ${problems.length} |`);
  rep.push('');
  if (fnRules.length) {
    rep.push('## ⚠️ 这些 RULES 用了函数，必须手工移植');
    rep.push('');
    rep.push('正则本身能搬，但替换逻辑里带条件判断，各平台要各写一遍：');
    rep.push('');
    for (const f of fnRules) rep.push(`- \`/${f.pattern}/${f.flags}\``);
    rep.push('');
  }
  if (problems.length) {
    rep.push('## ⚠️ 词典里真正的问题');
    rep.push('');
    for (const p of problems) rep.push(`- ${p}`);
    rep.push('');
  }
  if (whitespaceOnly.length) {
    rep.push(`## 待复核：译文为空或只有空白（${whitespaceOnly.length} 条）`);
    rep.push('');
    rep.push('**这一类大多是合法的，不是 bug。** 英文语序和中文不同时，被 `<b>` / `<code>`');
    rep.push('切碎的行内碎片在英文那边就该是空的。典型两条：');
    rep.push('');
    rep.push('- `共 <b>6</b> 条索引记录` → `"共 "` 译成 `""`，因为英文是 "6 index records"，前面不需要词');
    rep.push('- `以 <code>/v1</code> 结尾` → `" 结尾"` 译成 `""`，因为 "ending with" 已经在前半句里了');
    rep.push('');
    rep.push('**要复核的是**：某个碎片在英文里其实需要一个词、却被留空了。逐条看一遍：');
    rep.push('');
    for (const e of whitespaceOnly) rep.push(`- ${JSON.stringify(e.zh)} → ${JSON.stringify(e.en)}`);
    rep.push('');
  }

  // 重复译文（不同中文 → 同一英文）通常没问题，但值得看一眼
  const dupes = [...enSeen.entries()].filter(([, zhs]) => zhs.length > 1);
  if (dupes.length) {
    rep.push(`## 重复译文（${dupes.length} 组，通常是正常的同义句）`);
    rep.push('');
    for (const [en, zhs] of dupes.slice(0, 25)) {
      rep.push(`- \`${en.slice(0, 70)}\` ← ${zhs.length} 条`);
    }
    if (dupes.length > 25) rep.push(`- …还有 ${dupes.length - 25} 组`);
    rep.push('');
  }

  // ---------- 写盘 ----------
  console.log(`ZH2EN 词条      : ${entries.length}`);
  console.log(`RULES 总数      : ${RULES.length}`);
  console.log(`  · 可导出      : ${simpleRules.length}`);
  console.log(`  · 需手工移植  : ${fnRules.length}`);
  console.log(`词典问题        : ${problems.length}`);
  console.log(`待复核（空译文）: ${whitespaceOnly.length}（多为合法的碎片留空，见 REPORT.md）`);

  if (CHECK_ONLY) {
    if (problems.length) { console.error('\n✗ --check 失败：词典有问题'); process.exit(1); }
    console.log('\n✓ --check 通过');
    return;
  }

  fs.mkdirSync(path.join(OUT, 'en.lproj'), { recursive: true });
  fs.mkdirSync(path.join(OUT, 'zh-Hans.lproj'), { recursive: true });
  fs.writeFileSync(path.join(OUT, 'en.lproj', 'Localizable.strings'), iosLines.join('\n'), 'utf8');
  fs.writeFileSync(path.join(OUT, 'zh-Hans.lproj', 'Localizable.strings'), iosZhLines.join('\n'), 'utf8');
  fs.writeFileSync(path.join(OUT, 'i18n_en.json'), JSON.stringify(json, null, 2) + '\n', 'utf8');
  fs.writeFileSync(path.join(OUT, 'rules.json'), JSON.stringify(rulesOut, null, 2) + '\n', 'utf8');
  fs.writeFileSync(path.join(OUT, 'REPORT.md'), rep.join('\n'), 'utf8');

  console.log(`\n✓ 已写入 ${path.relative(ROOT, OUT)}/`);
  console.log('  en.lproj/Localizable.strings');
  console.log('  zh-Hans.lproj/Localizable.strings');
  console.log('  i18n_en.json');
  console.log('  rules.json');
  console.log('  REPORT.md');
}

main();
