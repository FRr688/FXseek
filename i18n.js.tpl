/* ============================================================================
 * FXseek 媒体库 —— 界面语言（简体中文 / English）
 *
 * 设计：**词典 + DOM 翻译器**，不改调用点。
 *   - 界面上的静态文案（HTML 里写死的、以及 JS 用模板字符串拼出来的）
 *     统一由 `apply(root)` 遍历文本节点与 title/placeholder/aria-label/alt
 *     属性来翻译。这样 ui.html / settings.html 里上百处 innerHTML 拼接
 *     一个字都不用动，改词条也只改这一处。
 *   - JS 里少数「先查后拼」的逻辑需要拿到**当前语言**做分支时，用 `T(zh, en)`。
 *
 * 两个必须守住的点：
 *   1. **幂等**。apply() 可以对同一棵子树反复跑（语言切换、DOM 更新都会再跑），
 *      所以翻译前必须能拿到「原文」。用 WeakMap 记住每个节点上一次被翻译前的
 *      文本，永远从原文出发；否则英译中再译英会越滚越长。
 *   2. **跳过用户数据**。日志、文件名、路径、搜索词这些是用户自己的内容，
 *      翻了就是灾难。靠 `SKIP_SEL` 与 `data-no-i18n` 挡住。
 * ==========================================================================*/
(function () {
  'use strict';

  var ZH2EN = __DICT__;

  /* 词条里带参数的（如「已接入 {n} 个 Agent」）走这里，按顺序套用。
     [正则, 英文替换模板] —— 只在当前语言是 en 时生效。 */
  var RULES = [
    [/^已接入 (\d+) 个 Agent$/, 'Connected to $1 agents'],
    [/^已断开 (\d+) 个 Agent$/, 'Disconnected $1 agents'],
    [/^已补上 (\d+) 个 Agent 的技能$/, 'Injected skills for $1 agents'],
    [/^打标 (\d+)\/(\d+)$/, 'Tagging $1/$2'],
    [/^转写 (\d+)\/(\d+)$/, 'Transcribing $1/$2'],
    [/^待打标 (\d+)$/, '$1 pending tags'],
    [/^待转写 (\d+)$/, '$1 pending transcriptions'],
    [/^(\d+) 个待补$/, '$1 to fill'],
    [/^共 (\d+) 条索引记录$/, '$1 index records'],
    [/^已清理 (\d+) 条失效记录$/, 'Cleaned $1 stale records'],
    [/^已删除 (\d+) 条失效记录$/, 'Removed $1 stale records'],
    [/^已清理 (\d+) 个缓存文件$/, 'Cleared $1 cache files'],
    [/^本轮已补 (\d+)\/(\S+)$/, 'This round: $1/$2'],
    [/^空闲 (\d+)s \/ (\d+)s$/, 'Idle $1s / $2s'],
    [/^正在补(标签|转写)$/, function (m, w) {
      return 'Filling in ' + (w === '标签' ? 'tags' : 'transcriptions');
    }]
  ];

  /* 绝不翻译的容器：日志、文件名、路径、搜索历史 —— 全是用户自己的内容。 */
  var SKIP_SEL = '.loglist,table.logs,.qhist,.srclist .nm,.card .nm,.row .nm,' +
                 '.path,.p,.msgbody,#q,.histbox,.logrow';

  var LANG = 'zh';
  var origText = new WeakMap();   // 文本节点 → 原始字符串
  var origAttr = new WeakMap();   // 元素 → {attr: 原始值}

  function lang() { return LANG; }
  function isEn() { return LANG === 'en'; }

  /* JS 里需要按语言分支时用它：T('中文', 'English') */
  function T(zh, en) { return isEn() ? en : zh; }

  /* 查词典；查不到就套 RULES；再不行原样返回。 */
  function translate(s) {
    if (!s) return s;
    var t = ZH2EN[s];
    if (t !== undefined) return t;
    for (var i = 0; i < RULES.length; i++) {
      var m = s.match(RULES[i][0]);
      if (m) {
        var rep = RULES[i][1];
        if (typeof rep === 'function') return rep.apply(null, m);
        return s.replace(RULES[i][0], rep);
      }
    }
    return s;
  }

  function skip(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.hasAttribute && el.hasAttribute('data-no-i18n')) return true;
    if (el.closest && el.closest(SKIP_SEL)) return true;
    return false;
  }

  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];

  function applyNode(root) {
    if (!root) return;
    if (root.nodeType === 3) { applyText(root); return; }
    if (root.nodeType !== 1) return;
    if (skip(root)) return;

    // 1) 属性
    for (var a = 0; a < ATTRS.length; a++) {
      var name = ATTRS[a];
      if (!root.hasAttribute(name)) continue;
      var cur = root.getAttribute(name);
      var store = origAttr.get(root);
      if (!store) { store = {}; origAttr.set(root, store); }
      if (store[name] === undefined) store[name] = cur;
      var want = isEn() ? translate(store[name]) : store[name];
      if (cur !== want) root.setAttribute(name, want);
    }

    // 2) 自身文本节点 + 后代（TreeWalker 比递归 querySelectorAll 稳，
    //    也不会把 <script>/<style> 里的内容当文案翻）
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        if (n.nodeType === 1) {
          var tag = n.tagName;
          if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
            return NodeFilter.FILTER_REJECT;
          }
          if (skip(n)) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = n.parentNode;
        if (p && skip(p)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) applyText(n);
    }
  }

  function applyText(node) {
    var cur = node.nodeValue;
    if (origText.has(node)) {
      // 已被翻译过：只在「当前值仍是我们写进去的译文」时才回写，
      // 否则说明 JS 自己更新了这个节点（比如计数刷新），要重新取原文。
      var rec = origText.get(node);
      if (cur !== rec.out) { rec.zh = cur; }
      rec.out = isEn() ? translate(rec.zh) : rec.zh;
      if (cur !== rec.out) node.nodeValue = rec.out;
      return;
    }
    var rec2 = { zh: cur, out: null };
    rec2.out = isEn() ? translate(cur) : cur;
    origText.set(node, rec2);
    if (rec2.out !== cur) node.nodeValue = rec2.out;
  }

  /* 切换语言：设 <html lang>、跑一遍、并让后续 DOM 变更自动跟上。 */
  var mo = null;
  function setLang(l) {
    LANG = (l === 'en') ? 'en' : 'zh';
    document.documentElement.lang = (LANG === 'en') ? 'en' : 'zh-CN';
    applyNode(document.body);
    watch();
  }

  /* MutationObserver：JS 拼出来的卡片、toast、状态行都会命中。
     **必须去抖** —— 索引/补全进行中时 DOM 每秒要变动几十次，
     不防抖会把主线程吃满（这正是「开着的时候后台每 20 秒看一眼」
     那段逻辑最怕的事）。 */
  var pending = false;
  function watch() {
    if (mo || !window.MutationObserver || !document.body) return;
    mo = new MutationObserver(function (muts) {
      if (LANG !== 'en') return;      // 中文是原文，不用翻译，省掉全部开销
      if (pending) return;
      pending = true;
      setTimeout(function () {
        pending = false;
        // 只处理新增节点；已存在节点上被改的文本由 applyText 的
        // 原文比对兜住（mutation 里丢掉了旧值，重跑整棵树代价太大）。
        for (var i = 0; i < muts.length; i++) {
          var added = muts[i].addedNodes;
          for (var j = 0; j < added.length; j++) {
            if (added[j].nodeType === 1) applyNode(added[j]);
            else if (added[j].nodeType === 3) applyText(added[j]);
          }
        }
      }, 50);
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  window.I18N = {
    apply: applyNode,
    setLang: setLang,
    lang: lang,
    isEn: isEn,
    T: T,
    dict: ZH2EN
  };

  // 页面里可能在 i18n.js 之前就设过 data-lang，这里补齐一次。
  if (document.body) applyNode(document.body);
})();
