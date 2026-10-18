#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""FXseek 媒体库 —— 桌面外壳（打包分发版的入口）。

双击 .app 之后发生的事，按顺序：

  1. 先起一个「准备窗口」：因为模型有 1.9GB，用户第一次打开要下几分钟，
     不能让他对着一个 Dock 图标干瞪眼。这个窗口显示进度条 / 下载速度 /
     剩余时间，顺便把「换下载源」的按钮放在手边。
  2. 起本地 HTTP 服务（app.py 的 run_server）在后台线程里。它是阻塞式的
     serve_forever，所以必须放线程，不能占着主线程。
  3. 服务就绪后，把准备窗口换成真正的主窗口，指向 http://127.0.0.1:<port>/。

线程模型上有个必须注意的点：pywebview 的 GUI 必须跑在主线程（Cocoa 要求），
所以是「主线程跑 GUI、后台线程跑服务」，不能反过来。

命令行用法（开发时）：
    ./venv/cpython-3.11/bin/python3.11 launcher.py [--port 8231] [--debug]
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 进度换算要用到模型清单和总字节数。放在模块级：Prep.progress() 每 0.25 秒被调一次，
# 每次再 import 一次纯属浪费；而且函数内部的局部 import 拿不到这几个名字（先前就因此
# 抛 NameError，进度条一直卡在 0 MB / 0 MB）。
try:
    from model_dl import MODEL_FILES, EXPECTED_SIZES, TOTAL_BYTES
except Exception:          # 清单缺失不该让窗口起不来，退回空清单（进度退化为单文件）
    MODEL_FILES, EXPECTED_SIZES, TOTAL_BYTES = [], {}, 0

# 工作模式的覆盖窗（第二块 UI）。模块级只 import os/threading，
# webview / pyobjc 全是函数内延迟导入，所以这里直接 import 不会拖慢冷启动。
import workwin as _WORKWIN

APP_TITLE = "FXseek 媒体库"   # 仅作默认值；实际标题走 _tr() 按设置语言出中/英

# 主窗口默认尺寸：给素材墙留够地方（大图视图一列 300px 起步，放得下 3~4 列）
WIN_W, WIN_H = 1440, 900
WIN_MIN = (1060, 680)


# --------------------------------------------------------------------------
# 准备窗口（下载进度 / 启动中）
# --------------------------------------------------------------------------

# 准备窗口就是一个自包含的 HTML 页面，通过 pywebview 的 js_api 跟 Python 对话：
# Python 侧主动 push 进度（evaluate_js 调 window.__seek.set(...)），
# 用户点「换源」则走 Python 暴露的 choose_source(src) 方法。
_PREP_TPL = r"""<!DOCTYPE html>
<html lang="%(lang)s">
<head>
<meta charset="utf-8">
<title>%(apptitle)s</title>
<style>
  :root{
    --bg:#0f1115; --card:#171a21; --line:#252a34;
    --tx:#e8ecf4; --tx2:#98a2b8; --tx3:#626d84;
    --blue:#4f7dff; --purple:#a855f7;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f4f5f8; --card:#fff; --line:#e3e6ee;
           --tx:#141821; --tx2:#5b6478; --tx3:#98a0b3; }
  }
  *{box-sizing:border-box}
  html,body{height:100%%;margin:0}
  body{
    background:var(--bg); color:var(--tx);
    font:14px/1.5 -apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif;
    display:flex; align-items:center; justify-content:center;
    -webkit-user-select:none; user-select:none; overflow:hidden;
  }
  .box{width:460px; text-align:center; padding:0 24px}
  .logo{
    width:64px; height:64px; margin:0 auto 18px; border-radius:17px;
    background:linear-gradient(135deg,#6366f1,#a855f7 55%%,#ec4899);
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 10px 30px rgba(139,92,246,.34); overflow:hidden;
  }
  .logo img{width:40px; height:auto; display:block}
  h1{font-size:20px; margin:0 0 6px; letter-spacing:.2px}
  .sub{color:var(--tx2); font-size:13px; margin-bottom:24px}
  .bar{
    height:7px; border-radius:99px; background:var(--line);
    overflow:hidden; margin-bottom:11px;
  }
  .bar > i{
    display:block; height:100%%; width:0%%;
    background:linear-gradient(90deg,#38bdf8,#a855f7);
    border-radius:99px; transition:width .3s ease;
  }
  .meta{display:flex; justify-content:space-between; align-items:baseline;
        color:var(--tx2); font-size:12.5px; font-variant-numeric:tabular-nums;
        min-height:18px}
  .meta .big{font-size:19px; font-weight:600; color:var(--tx); letter-spacing:.3px}
  .meta .eta{color:var(--tx3); font-size:12px}
  .bytes{margin-top:3px; color:var(--tx3); font-size:12px;
         font-variant-numeric:tabular-nums; min-height:16px}
  /* 正在下载的文件名：单独一行，等宽数字免得文件名跳动时整行左右晃 */
  .fname{margin-top:3px; color:var(--tx3); font-size:11.5px; min-height:15px;
         white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
         font-variant-numeric:tabular-nums; visibility:hidden}
  .src{
    margin-top:26px; padding-top:18px; border-top:1px solid var(--line);
    color:var(--tx3); font-size:12.5px;
  }
  .src b{color:var(--tx2); font-weight:600}
  .btns{display:flex; gap:8px; justify-content:center; margin-top:11px}
  button{
    font:inherit; font-size:12.5px; padding:6px 13px; border-radius:8px;
    border:1px solid var(--line); background:var(--card); color:var(--tx);
    cursor:pointer; transition:.15s;
  }
  button:hover{border-color:var(--blue); color:var(--blue)}
  button.on{border-color:var(--blue); color:var(--blue); background:rgba(79,125,255,.09)}
  .hint{margin-top:20px; color:var(--tx3); font-size:12px; min-height:17px}
  /* 热启动（模型已经在本地）：把只在「下载」时有意义的东西全收起来——下载源
     按钮、百分比、已下字节、文件名。否则第二次打开的用户会看到一个和首次下载
     一模一样的窗口，以为又要重下 1.9GB（用户报过这个 bug）。 */
  body.warm .src, body.warm .meta, body.warm .bytes, body.warm .fname{display:none}
  body.warm .bar > i{width:100%% !important; animation:breathe 1.3s ease-in-out infinite}
  @keyframes breathe{0%%,100%%{opacity:.3} 50%%{opacity:1}}
  .spin{
    width:15px;height:15px;border:2px solid var(--line);border-top-color:var(--blue);
    border-radius:50%%;display:inline-block;vertical-align:-2px;margin-right:7px;
    animation:sp .8s linear infinite;
  }
  @keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
  <div class="box">
    <div class="logo"><img src="data:image/png;base64,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" alt=""></div>
    <h1 id="title">%(title)s</h1>
    <div class="sub" id="sub">%(sub)s</div>

    <div class="bar"><i id="fill"></i></div>
    <div class="meta">
      <span id="left" class="big">0%%</span>
      <span id="eta" class="eta"></span>
    </div>
    <div class="bytes"><span id="bytes">%(conn)s</span></div>
    <div class="fname" id="fname"></div>

    <div class="src">
      %(src)s：<b id="srcName">%(src_def)s</b>
      <div class="btns">
        <button id="bMirror" class="on" onclick="pick('mirror')">%(b_mirror)s</button>
        <button id="bHf" onclick="pick('hugging')">%(b_hf)s</button>
      </div>
    </div>

    <div class="hint" id="hint"><span class="spin"></span>%(hint)s</div>
  </div>

<script>
  var S = {
    set: function (o) {
      if (o.title) document.getElementById('title').textContent = o.title;
      if (o.sub !== undefined) document.getElementById('sub').textContent = o.sub;
      if (o.pct !== undefined) {
        document.getElementById('fill').style.width = Math.max(0, Math.min(100, o.pct)) + '%%';
      }
      if (o.left !== undefined) document.getElementById('left').textContent = o.left;
      if (o.eta !== undefined) document.getElementById('eta').textContent = o.eta;
      if (o.bytes !== undefined) document.getElementById('bytes').textContent = o.bytes;
      if (o.fname !== undefined) {
        var fe = document.getElementById('fname');
        fe.textContent = o.fname;
        fe.style.visibility = o.fname ? 'visible' : 'hidden';
      }
      // hint 为 null 表示「这一步没有要说的话」，保持上一次的提示不动。
      // 不能直接拼进 innerHTML——null 会被拼成字符串 "null"（踩过）。
      if (o.hint) {
        document.getElementById('hint').innerHTML =
          '<span class="spin"></span>' + o.hint;
      }
      if (o.done) {
        document.getElementById('hint').innerHTML = '%(done)s';
      }
    },
    src: function (name) {
      document.getElementById('srcName').textContent = name;
    },
    // 'warm' = 模型已经在本地，没有下载这回事；'' = 正常下载进度。
    // 只切一个 body class，样式里把下载相关的那几块 display:none 掉。
    mode: function (m) {
      document.body.classList.toggle('warm', m === 'warm');
    }
  };
  window.__seek = S;

  function pick(src) {
    document.getElementById('bMirror').classList.toggle('on', src === 'mirror');
    document.getElementById('bHf').classList.toggle('on', src === 'hugging');
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.choose_source(src);
    }
  }

  // 页面加载完主动去取一次当前状态。方向是「页面拉」而不是「Python 推」，
  // 因为服务线程常常比这个窗口先就绪（模型已在本地时它跑得飞快），
  // Python 推的那几句会打在还没加载的页面上被吞掉——用户就只能盯着下面这段
  // 默认文案干等读模型的 20 秒，以为每次打开都要重下 1.9GB。
  function pull(tries) {
    tries = tries || 0;
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.prep_state) {
      if (tries < 100) { setTimeout(function () { pull(tries + 1); }, 60); }
      return;
    }
    api.prep_state().then(function (st) {
      if (!st) { return; }
      if (st.mode) { S.mode(st.mode); }
      if (st.state) { S.set(st.state); }
    }).catch(function () {});
  }
  pull();
  window.addEventListener('pywebviewready', function () { pull(); });
</script>
</body>
</html>
"""


def _prep_html():
    """准备窗口页面。原生窗口吃不到 i18n.js，按设置语言出中/英两版。"""
    en = _lang() == "en"
    return _PREP_TPL % {
        "lang": "en" if en else "zh-CN",
        "apptitle": _tr("FXseek 媒体库", "FXseek Library"),
        "title": "Starting" if en else "正在启动",
        "sub": "Preparing the interface…" if en else "正在准备界面…",
        "conn": "Connecting…" if en else "正在连接…",
        "src": "Download source" if en else "下载源",
        "src_def": "China mirror (recommended)" if en else "国内镜像（推荐）",
        "b_mirror": "China mirror" if en else "国内镜像",
        "b_hf": "HuggingFace official" if en else "HuggingFace 官方",
        "hint": "Almost there — hang tight" if en else "马上就好，不用管它",
        "done": "Model ready, opening the interface…" if en else "模型就绪，马上打开界面…",
    }


def _fmt_mb(n):
    """按量级换单位。

    原来一律 "%.0f MB"：下到几十 MB 时四舍五入很粗糙，1.9 GB 的权重更是
    「1904 MB」这种读不出来的数字。这里小于 1 GB 用 MB，超过就换 GB。
    ★ 这个函数的 def 行曾经在批量改文件时被误删，只剩函数体挂在
      _prep_html 的 return 后面 —— 于是首次下载模型时 NameError:
      name '_fmt_mb' is not defined，启动页直接「启动失败」。
    """
    n = float(n or 0)
    if n >= 1073741824:
        return "%.2f GB" % (n / 1073741824.0)
    return "%.1f MB" % (n / 1048576.0)


def _fmt_eta(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return ""
    if sec < 60:
        return _tr("剩余 %d 秒" % sec, "%ds left" % sec)
    return _tr("剩余 %d 分 %02d 秒" % (sec // 60, sec % 60),
               "%dm %02ds left" % (sec // 60, sec % 60))


class Prep:
    """准备窗口的 Python 侧把手：被后台线程调用，往前端 push 进度。"""

    def __init__(self, window=None):
        self.window = window
        self._last = 0.0
        self._idx = 0
        self._cnt = 0
        self.source = "mirror"
        self._lock = threading.Lock()
        # 最近一次推给前端的全部字段 + 当前模式。窗口可能比服务线程起得晚，
        # 那时候推过去的消息会被 `window.__seek &&` 短路吞掉，所以得留一份，
        # 等页面真的能收消息了再补发（replay）。
        self._state = {}
        self._mode = ""
        # 页面主动来取过一次状态之后才为 True。在那之前一律不推——服务线程往往
        # 比窗口先就绪（模型已在本地时 find_model() 是毫秒级的），这时候推过去的
        # 消息不是被 window.__seek && 短路吞掉，就是让 evaluate_js 干等信号量。
        self._page_ready = False

    def bind(self, window):
        self.window = window

    def mark_page_ready(self):
        self._page_ready = True

    def snapshot(self):
        """准备页面自己来取一次当前状态。

        由页面发起，所以不存在「Python 推的时候页面还没加载」的竞态——
        这正是修「模型已经下好了，第二次打开还是弹首次下载模型」那个 bug 的关键。
        """
        with self._lock:
            self._page_ready = True
            return {"mode": self._mode, "state": dict(self._state)}

    def _push(self, payload, force=False):
        w = self.window
        if not w:
            return
        now = time.time()
        # 前端刷新限流：进度回调很密，50ms 一次足够顺滑，再密只是白烧 CPU
        with self._lock:
            for k, v in payload.items():
                # None = 「这一步没有要说的话」，不该覆盖掉上一条真实提示
                if v is not None:
                    self._state[k] = v
            if not self._page_ready:
                return
            if not force and now - self._last < 0.05:
                return
            self._last = now
        try:
            import json
            w.evaluate_js("window.__seek && window.__seek.set(%s)" % json.dumps(payload))
        except Exception:
            # 窗口可能已经被用户关掉了，进度照常走，不该因此中断下载
            pass

    def mode(self, m):
        """切准备窗口的模式：'warm'（模型已在本地）/ ''（真的在下载）。"""
        self._mode = m or ""
        if not self._page_ready:
            return                      # 页面就绪后会来取，见 snapshot()
        w = self.window
        if not w:
            return
        try:
            import json
            w.evaluate_js("window.__seek && window.__seek.mode(%s)" % json.dumps(self._mode))
        except Exception:
            pass

    # ---- 给 model_dl 的 progress 回调 ----
    def progress(self, ev):
        """把进度事件排版成准备窗口要的那几行。

        ★ 事件里的 done / total / pct / speed / eta 现在已经是「整个模型」的
        口径了 —— model_dl.download_model 统一折算过（它按清单顺序把前面文件的
        期望大小整块累加，再用最近几秒的滑窗算速度）。以前它报的是单文件进度，
        所以这里自己按 MODEL_FILES 又累加了一遍；现在还照旧累加就会算重，
        界面会显示成「1.9 GB 的模型下到 3.8 GB」。
        """
        got = int(ev.get("done") or 0)
        all_bytes = int(ev.get("total") or 0) or TOTAL_BYTES or 1
        sp = ev.get("speed") or 0
        overall = int(ev.get("pct") or 0)

        # index/count 现在每个事件都带着了，但老事件流没有，所以照旧兜一下。
        idx = ev.get("index") or 0
        cnt = ev.get("count") or 0
        if idx:
            self._idx, self._cnt = idx, cnt
        else:
            idx, cnt = self._idx, self._cnt
        fn = ev.get("file") or ""

        got = max(0, min(got, all_bytes))
        overall = max(0, min(100, overall))

        # 剩余时间按整包算，比按单文件算靠谱
        eta = 0
        if sp and sp > 1024:
            eta = int((all_bytes - got) / sp)

        # 左下角：大字百分比；右上角：整包剩余时间
        left = "%d%%" % overall
        eta_txt = _fmt_eta(eta) if eta > 0 else ""
        # 进度条正下方：实时已下 / 总量
        bytes_line = "%s / %s" % (_fmt_mb(got), _fmt_mb(all_bytes))
        # 再下一行：正在下载的文件名（速度附在后面，它属于「当前这个文件」）
        fname = os.path.basename(fn) if fn else ""
        if fname and sp and sp > 1024:
            fname += "  ·  %.1f MB/s" % (sp / 1048576.0)

        hint = None
        if cnt:
            hint = _tr("正在下载第 %d / %d 个文件" % (idx, cnt),
                   "Downloading file %d / %d" % (idx, cnt))
            # 1.9 GB 的权重文件排在最末，下到它会长时间只有这一个文件在动
            if fn == "model.safetensors":
                hint = _tr("正在下载模型主文件（1.9 GB），这一步最久，请耐心等",
                   "Downloading the main model file (1.9 GB) — this takes the longest")

        self._push({
            "pct": overall,
            "left": left,
            "eta": eta_txt,
            "bytes": bytes_line,
            "fname": fname,
            "hint": hint,
        })

    def stage(self, title, sub=None, hint=None, pct=None):
        p = {"title": title}
        if sub is not None:
            p["sub"] = sub
        # 空串 = 沿用上一句提示（前端 set() 也是真值才更新），别把它记进 _state
        if hint:
            p["hint"] = hint
        if pct is not None:
            p["pct"] = pct
        self._push(p, force=True)

    def set_source_name(self, label):
        w = self.window
        if not w:
            return
        try:
            import json
            w.evaluate_js("window.__seek && window.__seek.src(%s)" % json.dumps(label))
        except Exception:
            pass


class Api:
    """暴露给准备窗口的 JS API（window.pywebview.api.*）。"""

    def __init__(self, prep):
        self.prep = prep

    def prep_state(self):
        """准备页面加载完之后自己来取一次当前状态（页面拉，不是 Python 推）。"""
        try:
            return self.prep.snapshot()
        except Exception:
            return None

    def set_titlebar_theme(self, theme):
        """页面把当前主题/语言变化告诉原生层：同步 NSWindow 标题栏外观 + 窗口标题。

        WebKit 的内容跟着 body[data-theme] 走，但**系统标题栏**是 AppKit 画的，
        不认识网页主题——所以默认永远灰黑，浅色主题下突兀。这里用
        NSAppearance 让标题栏跟主题一致（aqua = 浅色，DarkAqua = 深色）。
        顺带把窗口标题按当前语言重设（zh「FXseek 媒体库」/ en「FXseek Library」），
        这样设置页切语言后顶栏标题即时跟着变，不用重启。

        ★ 线程纪律：pywebview 的 js_api 调用一律在**后台线程**里跑
        （util.js_bridge_call 里 thread.start()），而 NSWindow 的
        setAppearance_/setTitle_ 只能在主线程调，所以必须 callAfter 转回主线程。
        （注意：用户报过的「点关闭没反应」真凶是 launcher._Ctl 与
        tray_helper._Ctl 的 pyobjc 同名类碰撞，见 _ask_close 里的注释；
        这里的线程转投是另一条必须遵守的 AppKit 纪律。）"""
        try:
            import AppKit
            win = self._main_window()
            if win is None:
                return {"ok": False}
            name = "NSAppearanceNameDarkAqua" if theme == "dark" else "NSAppearanceNameAqua"
            try:
                appearance = AppKit.NSAppearance.appearanceNamed_(name)
            except Exception:
                appearance = None
            title = _tr("FXseek 媒体库", "FXseek Library")

            def _apply():
                try:
                    if appearance is not None:
                        win.setAppearance_(appearance)
                    win.setTitle_(title)
                    # ── 顶栏融进页面（用户：「像软件，不像网页」）──
                    # 标题栏已在 _tune_main_window 里转成透明，透明标题栏
                    # 露出的是**窗口背景色**；把它刷成页面 --bg 的同一个颜色
                    # （暗 #16181d / 亮 #eef1f5），系统标题条和网页内容就
                    # 无缝了。appearance 只管红绿灯按钮的明暗，背景色独立
                    # 生效——就算 NSAppearance 没吃上，顶栏颜色也对。
                    _apply_titlebar_colors(win, theme)
                except Exception as e:
                    print("[launcher] 同步标题栏失败：%s" % e)

            # 转主线程执行（见上面的线程纪律说明）。
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(_apply)
            except Exception:
                _apply()
            return {"ok": True}
        except Exception as e:
            print("[launcher] 同步标题栏主题失败：%s" % e)
            return {"ok": False}

    def _main_window(self):
        """拿主窗口的 NSWindow（pywebview 建窗后挂在 window.native 上）。"""
        try:
            w = _QUIT.get("window")
            if w is None:
                return None
            return _ns_window(w)
        except Exception:
            return None

    def choose_source(self, src):
        """用户在准备窗口点了「换下载源」。只改下一次启动用的源，
        当前这次下载不打断（半截文件已经落盘，换源重来更亏）。"""
        if src in ("mirror", "hugging"):
            self.prep.source = src
            import model_dl
            _sd = model_dl.SOURCES[src]
            self.prep.set_source_name(
                _sd.get("label_en", _sd["label"]) if _lang() == "en" else _sd["label"])
        return {"ok": True, "source": self.prep.source}


# --------------------------------------------------------------------------
# 窗口行为：缩放、关闭确认、菜单栏（托盘）
# --------------------------------------------------------------------------

# pywebview 6.2 的 macOS 后端既没有 set_min_size()（调了也只是被 try/except
# 吞掉，于是主窗口根本没有最小尺寸，能被拖成一条缝），窗口的 style mask 也只能
# 在建窗时给。这两件事都得自己用 pyobjc 补。
_MASK_MINIATURIZABLE = 1 << 2      # NSWindowStyleMaskMiniaturizable
_MASK_RESIZABLE = 1 << 3           # NSWindowStyleMaskResizable

# 进程级状态：关闭流程要跨「Cocoa 回调 → 弹窗 → 用户选择」几跳，
# 用一个 dict 捎着走，比到处传参清楚。
_REOPEN_CLS = None      # AppDelegate 子类只能定义一次，见 _reopen_delegate_class
_CLOSE_CTL_CLS = None   # 关闭弹窗按钮控制器类，同理只能定义一次，见 _ask_close

_QUIT = {
    "flag": False,        # True = 用户已决定退出，关闭请求一律放行
    "hidden": False,      # True = 面板已收进菜单栏（窗口被隐藏）
    "window": None,
    "dock_target": None,  # 程序坞右键菜单的 target，同上，必须引用住
    "tray_proc": None,    # 菜单栏图标助手（子进程），退出时要收掉
    "delegate": None,     # 自己那个 AppDelegate 子类实例，被 GC 掉 Dock 就点不回来
    "observer": None,     # 激活通知的观察者，同理必须引用住
    "api": "",            # 形如 127.0.0.1:8231，显示在菜单里（MCP 就是连这个地址）
}


def _on_main(fn, *args):
    """把 AppKit 那些「只能在主线程干」的活儿丢回主线程。

    NSStatusItem / NSAlert 这类是主线程限定的：从别的线程直接建会抛
    `NSInternalInconsistencyException - NSWindow should only be instantiated on
    the main thread!`（实测过）。正常路径本来就是主线程（closing 事件是同步跑在
    Cocoa 主线程上的），这里只是兜底，免得将来从后台线程调就悄悄失效。
    """
    try:
        import AppKit
        if AppKit.NSThread.isMainThread():
            fn(*args)
            return
    except Exception:
        pass
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn, *args)
    except Exception:
        fn(*args)


def _on_main_sync(fn, *args, timeout=5.0):
    """在主线程上跑 `fn` 并**等它返回**（要拿返回值的场合）。

    ★ 和 `_on_main` 的分工：那个是「丢过去就不管了」，用在关闭窗口这类
      fire-and-forget 的场合；而注册全局热键必须拿到 `(ok, why)` 才能回给设置页
      （失败了得让用户看见「这个键被别的软件占了」），所以这里用 Event 做一次
      同步往返。

    ★ 已经在主线程时必须**直接调用**，不能走 callAfter + wait —— 主线程自己
      在等自己，必死锁（`done.wait()` 永远等不到那个还没机会跑的 `_wrap`）。
    """
    try:
        import AppKit
        if AppKit.NSThread.isMainThread():
            return fn(*args)
    except Exception:                                       # noqa: BLE001
        return fn(*args)
    box, done = {}, threading.Event()

    def _wrap():
        try:
            box["v"] = fn(*args)
        except Exception as e:                              # noqa: BLE001
            box["e"] = e
        finally:
            done.set()

    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_wrap)
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": "没法把活儿丢回主线程：%s" % e}
    if not done.wait(timeout):
        # 超时就老实说超时。**绝不能**在拿不到结果时假装成功 —— 那样设置页会
        # 显示「已改成 ⌥⌘G」，实际按下去毫无反应，用户只会以为功能坏了。
        return {"ok": False, "error": "主线程正忙，热键没来得及注册，请再试一次"}
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _ns_window(window):
    """拿到 pywebview 底下那个 NSWindow。

    用 window.native：pywebview 建窗时做了 `pywebview_window.native = self.window`
    （cocoa.py 里那句），是可靠入口。走 window.gui.window 反而拿不到——
    Window.gui 那一层在这个版本里是空的，之前就因此静默返回 None。
    """
    return getattr(window, "native", None)


_DBLCLICK_MONITOR = None


def _titlebar_dblclick(ns):
    """双击标题栏要做什么：跟随系统偏好 AppleActionOnDoubleClick。

    macOS 默认（键不存在时）就是 Zoom —— 也就是「缩放」。"""
    try:
        import AppKit
        pref = (AppKit.NSUserDefaults.standardUserDefaults()
                .stringForKey_("AppleActionOnDoubleClick") or "Zoom")
    except Exception:
        pref = "Zoom"
    try:
        if pref == "Minimize":
            ns.performMiniaturize_(None)
        elif pref == "None":
            pass
        else:
            ns.performZoom_(None)
    except Exception as e:
        print("[launcher] 标题栏双击动作失败：%s" % e)


def _install_titlebar_dblclick(ns):
    """自己接管「双击标题栏缩放」。

    背景：标题栏为了让配色融进页面，被设成 transparent 并由我们上色之后，
    实测双击不再触发原生 performZoom（用户反馈「双击窗口顶栏没有任何反应」）。
    这里用本地事件监视器在事件派发前拦下：命中标题栏区域（内容视图之上、
    避开左侧红绿灯）且 clickCount == 2 时，执行系统偏好动作并把事件吃掉
    （返回 None）—— 吃掉是为了避免原生路径也能处理时被触发两次。
    """
    global _DBLCLICK_MONITOR
    if _DBLCLICK_MONITOR is not None:
        return
    try:
        import AppKit

        def handler(event):
            try:
                if event.clickCount() != 2:
                    return event
                content = ns.contentView()
                if content is None:
                    return event
                ch = content.frame().size.height
                loc = event.locationInWindow()
                # 内容视图之上 = 标题栏；x > 90 把红绿灯那一片留给系统
                if loc.y > ch and loc.x > 90:
                    _titlebar_dblclick(ns)
                    return None
            except Exception as e:
                print("[launcher] 标题栏双击监视失败：%s" % e)
            return event

        _DBLCLICK_MONITOR = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown, handler)
        print("[launcher] 已接管标题栏双击（按系统偏好缩放/最小化）")
    except Exception as e:
        print("[launcher] 安装标题栏双击监视失败：%s" % e)


def _theme_from_settings():
    """当前该给标题栏用的主题：显式 dark/light 直接用，auto 按本地时间
    （07:00-19:00 亮，其余暗——和设置页 AUTO 的规则一致）。"""
    try:
        import app as APP
        t = (APP.load_settings() or {}).get("theme") or "auto"
    except Exception:
        t = "auto"
    if t in ("dark", "light"):
        return t
    try:
        h = time.localtime().tm_hour
        return "light" if 7 <= h < 19 else "dark"
    except Exception:
        return "light"


def _apply_titlebar_colors(ns, theme):
    """透明标题栏的配色（必须主线程）：窗口背景 = 页面 --bg 同色，
    NSAppearance 随主题（管红绿灯按钮明暗）。

    这样透明标题栏露出的就是页面底色，顶栏和内容无缝——用户要求
    「顶栏融入软件，像软件不像网页」。暗 #16181d / 亮 #eef1f5
    （取自 ui.html/settings.html 的 --bg，改这两处颜色要同步这里）。"""
    try:
        import AppKit
        dark = (theme == "dark")
        try:
            appearance = AppKit.NSAppearance.appearanceNamed_(
                "NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua")
            if appearance is not None:
                ns.setAppearance_(appearance)
        except Exception:
            pass
        if dark:
            r, g, b = 0.0863, 0.0941, 0.1137   # #16181d
        else:
            r, g, b = 0.9333, 0.9451, 0.9608   # #eef1f5
        col = AppKit.NSColor.colorWithRed_green_blue_alpha_(r, g, b, 1.0)
        ns.setBackgroundColor_(col)
        # ★ 关键：标题栏自身有一层**不透明底色**（pywebview 建窗时按
        #   windowBackgroundColor 刷的，深色外观下就是量到的 (44,44,44) 灰条），
        #   注释原话「so that it does not change with the window color」——
        #   只设窗口背景色永远盖不住它。必须把这层标题栏视图也刷成同色
        #   （取法与 pywebview cocoa.py:710 完全一致：contentView 的
        #   superview = NSThemeFrame，其最后一个 subview = 标题栏视图）。
        try:
            tv = ns.contentView().superview().subviews().lastObject()
            if tv is not None and hasattr(tv, "setBackgroundColor_"):
                tv.setBackgroundColor_(col)
        except Exception as e:
            print("[launcher] 标题栏视图上色失败：%s" % e)
    except Exception as e:
        print("[launcher] 标题栏配色失败：%s" % e)


def _tune_main_window(window):
    """主界面出来之后，补上「可缩放 + 最小尺寸」，并把窗口摆到屏幕中间。

    1) 可缩放：准备窗口是按 520×430 设计的固定小窗，建窗时 resizable=False；
       macOS 因此不给 NSResizableWindowMask，标题栏第三个（绿色）按钮就是灰的、
       点不动（用户报过）。pywebview 6.2 也没有运行期改 resizable 的接口，
       只能自己补这个 style mask。
    2) 最小尺寸：pywebview 6.2 的 macOS 后端没有 set_min_size()，launcher 里原来
       那句 window.set_min_size(*WIN_MIN) 一直被 try/except 吞掉，主窗口其实没有
       最小尺寸，能被拖成一条缝。
    3) 居中：resize 是从原本 520×430 的位置长成 1440×900 的，左下角不动，于是很
       容易长到屏幕外面（实测 origin.y = -205，小半个窗口掉在屏幕下方）。
    """

    def _apply():
        ns = _ns_window(window)
        if ns is None:
            print("[launcher] 拿不到原生窗口，跳过缩放/居中调整")
            return
        try:
            ns.setStyleMask_(ns.styleMask() | _MASK_RESIZABLE | _MASK_MINIATURIZABLE)
            ns.setContentMinSize_((float(WIN_MIN[0]), float(WIN_MIN[1])))
            ns.center()
        except Exception as e:
            print("[launcher] 调整主窗口失败：%s" % e)
        # ── 顶栏去「网页感」：透明标题栏 + 不显示居中标题文字 ──
        # 透明标题栏露出窗口背景色（由 set_titlebar_theme 按主题刷成页面
        # --bg 同色），标题文字藏掉（页面自己顶栏已有品牌与标题），
        # 只留红绿灯按钮——和原生软件（VS Code / Finder 这类）一致。
        # 不用 FullSizeContentView：内容区仍在标题栏下方，拖拽窗口、
        # 双击缩放、红绿灯全部保持原生行为，零风险。
        try:
            import AppKit
            ns.setTitlebarAppearsTransparent_(True)
            ns.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        except Exception as e:
            print("[launcher] 标题栏透明化失败：%s" % e)
        # 透明之后立刻上配色（on_loaded 时窗口一定在，页面的第一次
        # set_titlebar_theme 可能赶在 window 挂好之前，会静默早退）。
        _apply_titlebar_colors(ns, _theme_from_settings())
        # 透明标题栏会让原生双击缩放失效，这里自己补上（见函数注释）。
        _install_titlebar_dblclick(ns)

    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_apply)
    except Exception:
        _apply()


def _arm_page_ready(prep, delay=3.0):
    """兜底：万一 js_api 桥没接上（pywebview 没注入 window.pywebview），
    页面就永远来不了 prep_state 这一趟。过几秒强制放行，让 evaluate_js
    照老路子往下推，不至于一句进度都看不见。

    正常路径用不着它：页面加载完会自己调 prep_state 把闸门打开。
    """

    def _arm():
        time.sleep(delay)
        prep.mark_page_ready()

    threading.Thread(target=_arm, daemon=True, name="fxseek-prepgate").start()


def _close_action():
    """读「点关闭按钮怎么办」的持久化选择：ask / quit / tray。"""
    try:
        import app as APP
        v = (APP.load_settings() or {}).get("close_action") or "ask"
        return v if v in ("ask", "quit", "tray") else "ask"
    except Exception:
        return "ask"


def _save_close_action(v):
    """写回持久化选择。这里也过一遍白名单：写进去的必须是三个合法值之一，
    免得把对话框里意外冒出来的别的字符串留在设置里。"""
    try:
        import app as APP
        APP.save_settings({"close_action": v if v in ("ask", "quit", "tray") else "ask"})
    except Exception:
        pass


def _on_closing(window):
    """标题栏关闭按钮 / Cmd+W 的拦截。

    返回值语义（pywebview 的 Event.set → should_close）：返回 False = 取消关闭，
    返回 True = 放行。第一次点关闭时先问一句「直接退出还是最小化到菜单栏」，
    所以这里返回 False 把关闭拦下来，再排一个原生对话框上去。
    """
    if _QUIT["flag"]:
        return True
    action = _close_action()
    if action == "quit":
        return True
    if action == "tray":
        # 同理，隐藏窗口这件事推到关闭事件处理完之后再做
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_to_tray, window)
        except Exception:
            _to_tray(window)
        return False
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_ask_close, window)
    except Exception:
        _ask_close(window)
    return False


def _ask_close(window):
    """原生三键对话框：退出 / 最小化到菜单栏 / 取消，可勾选「记住我的选择」。"""
    global _CLOSE_FX
    _CLOSE_FX = None
    try:
        import AppKit
    except Exception:
        return
    try:
        app = AppKit.NSApplication.sharedApplication()

        # 按钮共用的小控制器：点了就把本次模态会话以「按钮 tag」结束。
        # ★ 只能定义一次：pyobjc 里第二次定义同名类会抛「… is overriding
        #   existing Objective-C class」，之后按钮就再也收不到点击了。所以缓存起来。
        # ★ ★ 类名必须**全局唯一**：pyobjc 按 __name__ 往 ObjC 运行时里注册，
        #   tray_helper._show_about（菜单栏「关于」，b575 起在主进程里 import 调用）
        #   也定义过一个 `class _Ctl` —— 同进程里两边都叫 _Ctl，谁后定义谁炸。
        #   实测日志 171 次「关闭确认框失败：_Ctl is overriding…」全是这个碰撞：
        #   本会话先点过「关于」→ 关闭弹窗弹不出来（用户报的「点关闭没反应」）；
        #   先关过窗口 → 反过来「关于」弹不出来。两边各改唯一名 _CloseCtl/_AboutCtl。
        global _CLOSE_CTL_CLS
        if _CLOSE_CTL_CLS is None:
            class _CloseCtl(AppKit.NSObject):
                def buttonClick_(self, sender):
                    AppKit.NSApp().stopModalWithCode_(sender.tag())
            _CLOSE_CTL_CLS = _CloseCtl
        ctl = _CLOSE_CTL_CLS.alloc().init()

        # 手搓横向面板（NSAlert 的按钮永远竖着堆，还自带大图标/说明区，砍不掉）。
        W, H = 600, 148
        panel = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H),
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered, False)
        panel.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        panel.setTitlebarAppearsTransparent_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)            # 磨砂圆角外透出桌面
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setLevel_(AppKit.NSModalPanelWindowLevel)
        panel.center()
        cv = panel.contentView()
        cv.setWantsLayer_(True)
        # 磨砂底（与「关于」面板同款：HUD 材质 + 14px 圆角）
        try:
            fx = AppKit.NSVisualEffectView.alloc().initWithFrame_(cv.bounds())
            fx.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
            fx.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
            fx.setState_(AppKit.NSVisualEffectStateActive)
            fx.setWantsLayer_(True)
            fx.layer().setCornerRadius_(14.0)
            fx.layer().setMasksToBounds_(True)
            fx.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
            cv.addSubview_(fx)
            _CLOSE_FX = fx
        except Exception:
            _CLOSE_FX = None

        title = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(24, H - 42, W - 48, 24))
        title.setStringValue_(_tr("要退出 FXseek 吗？", "Quit FXseek?"))
        title.setFont_(AppKit.NSFont.boldSystemFontOfSize_(15))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        (_CLOSE_FX or cv).addSubview_(title)

        remember = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(24, H - 74, 320, 22))
        remember.setButtonType_(AppKit.NSButtonTypeSwitch)
        remember.setTitle_(_tr("记住我的选择，下次不再询问",
                              "Remember my choice, don't ask again"))
        remember.setState_(AppKit.NSControlStateValueOff)
        remember.sizeToFit()
        (_CLOSE_FX or cv).addSubview_(remember)

        # 三个按钮横排一行，从右往左摆：默认动作（退出）最靠右，macOS 惯例。
        BW, BH, GAP, M = 168, 34, 10, 20
        defs = ((_tr("退出 FXseek", "Quit FXseek"), 1, BW),
                (_tr("最小化到菜单栏", "Minimize to menu bar"), 2, BW),
                (_tr("取消", "Cancel"), 3, 96))
        x = W - M
        btns = {}
        for label, tag, bw in defs:
            x -= bw
            b = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, 18, bw, BH))
            b.setTitle_(label)
            b.setBezelStyle_(AppKit.NSBezelStyleRounded)
            b.setTarget_(ctl)
            b.setAction_("buttonClick:")
            b.setTag_(tag)
            (_CLOSE_FX or cv).addSubview_(b)
            btns[tag] = b
            x -= GAP
        btns[1].setKeyEquivalent_("\r")     # 回车 = 退出（默认高亮）
        btns[3].setKeyEquivalent_("\x1b")   # Esc = 取消
        try:
            panel.setInitialFirstResponder_(btns[1])
        except Exception:
            pass

        resp = app.runModalForWindow_(panel)
        remembered = remember.state() == AppKit.NSControlStateValueOn
        panel.orderOut_(None)
        # 注意：这里人还在模态会话里。关窗口 / 换激活策略这类活儿
        # 不能在模态里做（AppKit 会忽略掉一部分，隐藏窗口和 Dock 图标状态就
        # 对不上了），所以统统排到模态结束之后再跑。
        if resp == 1:
            _save_close_action("quit" if remembered else "ask")
            _QUIT["flag"] = True
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(_quit_now, window)
            except Exception:
                _quit_now(window)
        elif resp == 2:
            _save_close_action("tray" if remembered else "ask")
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(_to_tray, window)
            except Exception:
                _to_tray(window)
        # 取消（3）/ Esc（0）：什么都不做，窗口留着
    except Exception as e:
        print("[launcher] 关闭确认框失败：%s" % e)


def _to_tray(window):
    """把面板收起来：窗口隐藏，进程、HTTP 服务、后台任务照常跑。

    这里**不切 activationPolicy**：切了 Dock 图标就没了，而用户找回面板靠的
    正是 Dock 图标（点一下、或右键菜单），留着它比「看起来更像个后台程序」重要。
    """
    if window is None:
        return

    def _do():
        _QUIT["hidden"] = True
        # 记一下「刚藏起来的时刻」：窗口一藏，应用往往会立刻收到一次
        # NSApplicationDidBecomeActive（等于自己把自己弄成前台），激活回调会以为
        # 用户在点 Dock，转手就把面板又摆出来 —— 表现就是「点了最小化，窗口闪一下
        # 又回来了」。所以刚藏起来的这一小段时间内不理激活通知（踩过）。
        _QUIT["hid_at"] = time.time()
        try:
            window.hide()
        except Exception:
            pass

    _on_main(_do)


def _from_tray():
    """把收起来的面板还回来。"""
    if _QUIT.get("hidden"):
        _QUIT["hidden"] = False
        print("[launcher] 面板已唤回")

    def _do():
        w = _QUIT.get("window")
        if w is not None:
            try:
                w.show()
            except Exception:
                pass
        try:
            import AppKit
            AppKit.NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    _on_main(_do)


_RESTORE_GRACE = 1.5       # 刚藏起来的这段时间内不响应激活通知，见 _to_tray


def _restore_panel():
    """面板收起来了就唤回来（点 Dock 图标、或右键菜单「打开面板」都走这里）。"""
    # ★★ 工作模式开着的时候**一律不许**把主窗叫回来。
    #   踩过的坑：`workwin.show()` 为了让搜索框能直接打字，会调
    #   `NSApp.activateIgnoringOtherApps_(True)`；这一下会发
    #   `NSApplicationDidBecomeActiveNotification`，正好打在这个回调上。
    #   若此刻 `_QUIT["hidden"]` 是 True（用户之前把面板收进过菜单栏），这里就会
    #   `_from_tray()` → `window.show()`，**刚被收起来的主窗立刻又被摆出来** ——
    #   用户看到的就是「设置页跟着工作模式一起出现」，而且两套 UI 叠着，搜索框的
    #   胶囊行和主窗的胶囊行重影（被报成「排版互相挤压」和「面板一直闪」）。
    #   工作模式下主窗必须缺席，这是硬约束，不走 Dock 唤回这条路。
    try:
        import workwin as _W
        # ★ 2026-10-15 第②条：这里必须用 `engaged()` 而不是 `is_open()`。
        #   `is_open()` 只问搜索层可不可见，而搜索层平时是收着的（暂存区那条
        #   绳窗才是工作模式的常驻形态）—— 一旦收着，这个「不许唤回」的闸门
        #   就形同虚设。点「收工」按钮会 makeKeyAndOrderFront（顺带激活 App），
        #   正好走到这里，于是休闲模式主窗被摆出来，两套 UI 同时出现；
        #   弹过一次之后 _QUIT["hidden"] 变 False，后面几次才不再弹 ——
        #   用户看到的就是「前几次点收工会把主窗唤醒」。
        if _W.engaged():
            print("[launcher] 工作模式开着（engaged），忽略这次 Dock/激活唤回",
                  flush=True)
            # ★ 光「忽略」还不够：万一是别处（旧逻辑、系统恢复窗口）把它摆出来的，
            #   这里顺手再收一次，保证「两套 UI 不同屏」这条硬约束不被绕开。
            #   注意用的是 `_QUIT["window"]`（休闲模式的主窗），不是
            #   workwin 的 `window()`（那个是工作模式的搜索层）。
            try:
                _mw = _QUIT.get("window")
                if _mw is not None and not _QUIT.get("hidden"):
                    _mw.hide()
                    _QUIT["hidden"] = True
                    print("[launcher] 工作模式下主窗又冒出来了，收回去", flush=True)
            except Exception:                               # noqa: BLE001
                pass
            return
    except Exception:                                       # noqa: BLE001
        pass
    if time.time() - float(_QUIT.get("hid_at") or 0) < _RESTORE_GRACE:
        return          # 刚藏下去的那一下，别自己把自己叫回来
    # ★ 范老板 2026-10-16：「休闲模式关掉窗口 → 切工作模式 → 切回休闲，
    #   点程序坞图标唤不醒窗口了」。原来这里只看 `_QUIT["hidden"]` 这个标记：
    #   这条来回切的路上标记可能被某一步清成 False，而窗口其实还藏着 ——
    #   于是 `hidden` 为假就直接 return，Dock 点下去什么也没发生。
    #   改成**问窗口本身**：只要它在（且不可见），就唤回来。
    _w = _QUIT.get("window")
    try:
        _vis = bool(getattr(_w, "visible", True)) if _w is not None else False
    except Exception:                                       # noqa: BLE001
        _vis = False
    if not _QUIT.get("hidden") and _vis:
        return
    if not _vis:
        print("[launcher] Dock 唤回：窗口不可见（hidden=%s），唤回" % _QUIT.get("hidden"),
              flush=True)
    _from_tray()


def _reopen_delegate_class():
    """我们那份 AppDelegate 子类。

    ★ 只能定义一次：pyobjc 里第二次定义同名类会抛
    「_ReopenDelegate is overriding existing Objective-C class」，那之后
    setDelegate_ 就再也不会执行了（踩过：Dock 点不回面板就是这个原因）。
    """
    global _REOPEN_CLS
    if _REOPEN_CLS is not None:
        return _REOPEN_CLS
    from webview.platforms import cocoa as _cocoa
    import AppKit as _ak

    class _ReopenDelegate(_cocoa.BrowserView.AppDelegate):
        def applicationShouldHandleReopen_hasVisibleWindows_(
                self, sender, has_visible_windows):
            # 应用已经在最前面、只是窗口被藏了：系统走这条路。
            _restore_panel()
            return True

        def applicationDockMenu_(self, sender):
            # 保底入口：右键程序坞图标也能开/关面板、退出。
            # （菜单栏图标虽然由子进程负责，但万一它没摆上，这条一定在。）
            try:
                tgt = _QUIT.get("dock_target")
                if tgt is None:
                    tgt = _TrayTarget.alloc().init()
                    _QUIT["dock_target"] = tgt
                m = _ak.NSMenu.alloc().init()
                a = _ak.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "打开面板" if _QUIT.get("hidden") else "隐藏面板",
                    b"togglePanel:", "")
                a.setTarget_(tgt)
                m.addItem_(a)
                m.addItem_(_ak.NSMenuItem.separatorItem())
                b = _ak.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "退出 FXseek", b"quitApp:", "")
                b.setTarget_(tgt)
                m.addItem_(b)
                return m
            except Exception as e:
                print("[launcher] 程序坞菜单没建成：%s" % e)
                return None

    _REOPEN_CLS = _ReopenDelegate
    return _REOPEN_CLS


def _install_reopen_hook():
    """让「点 Dock 图标」也能把藏起来的面板叫回来。

    两条路都接上，免得漏：
      1. 应用不在最前面时点 Dock 图标 → 先激活，发 NSApplicationDidBecomeActive
         通知；在通知里看 _QUIT["hidden"]，是藏着的就唤回来。
      2. 应用已经在最前面、只是窗口被藏了 → 系统走
         applicationShouldHandleReopen:hasVisibleWindows:，得挂在自己的
         AppDelegate 上。pywebview 自己也装了个 AppDelegate（负责 should_terminate
         等），所以这里**子类化它**再 setDelegate_，原行为一个不丢。

    这个函数会**被调用两次**（main() 里一次、on_loaded 里一次）：pywebview 的
    webview.start() 会给每个窗口调 create_window()，那里有
    `app.setDelegate_(BrowserView._shared_app_delegate)`（cocoa.py:615），把我们先
    装的顶掉。所以第二次必须在 start() 之后重装。类只定义一次，见
    _reopen_delegate_class()。
    """
    try:
        import AppKit
    except Exception:
        return

    # 路 1：激活通知。用 target/selector 而不是 block —— block 观察者在
    # pyobjc 里要自己管生命周期，出过 SIGTRAP，没必要冒这个险。
    if _QUIT.get("observer") is None:
        try:
            class _ActivateObserver(AppKit.NSObject):
                def appActivated_(self, note):
                    _restore_panel()

            obs = _ActivateObserver.alloc().init()
            AppKit.NSNotificationCenter.defaultCenter(
            ).addObserver_selector_name_object_(
                obs, b"appActivated:",
                AppKit.NSApplicationDidBecomeActiveNotification, None)
            _QUIT["observer"] = obs
        except Exception as e:
            print("[launcher] 装激活回调失败：%s" % e)

    # 路 2：AppDelegate 的 reopen
    try:
        cls = _reopen_delegate_class()
        d = _QUIT.get("delegate")
        if d is None or not isinstance(d, cls):
            d = cls.alloc().init()
            _QUIT["delegate"] = d
        AppKit.NSApp().setDelegate_(d)
    except Exception as e:
        print("[launcher] 装 Dock 重开回调失败：%s" % e)


_ABOUT_MENU_TARGET = None   # 「关于」菜单项回调目标，必须引用住（同 _TrayTarget）


def _rewire_about_menu_once(port):
    """单次尝试：找到 orderFrontStandardAboutPanel: 菜单项并改接。找到返回 True。"""
    import AppKit
    menu = AppKit.NSApp().mainMenu()
    if menu is None:
        return False
    for item in menu.itemArray() or []:
        sub = item.submenu()
        if sub is None:
            continue
        for mi in (sub.itemArray() or []):
            try:
                act = mi.action()
            except Exception:
                continue
            if act and str(act) == "orderFrontStandardAboutPanel:":
                import tray_helper as TH
                TH.STATE["port"] = port
                TH.STATE["data_dir"] = _data_dir()
                TH.STATE["running"] = True
                mi.setAction_(b"showAbout:")
                mi.setTarget_(_ABOUT_MENU_TARGET)
                print("[launcher] 「关于」菜单已改接磨砂面板")
                return True
    return False


def _rewire_about_menu(port):
    """把菜单栏 FXseek →「关于」从系统标准面板换成我们自己的横向磨砂面板。

    pywebview 的 Cocoa 后端在 _add_app_menu 里自动挂了一条
    `orderFrontStandardAboutPanel:`（系统标准 About，图标是 python3.11 文件夹），
    跟 tray_helper 里那个「关于 FXseek」磨砂面板是两回事。这里把那条菜单项的
    动作改成调用 tray_helper._show_about()（横向磨砂面板，中英随设置）。

    tray_helper 是独立进程的助手，_show_about 读它的 STATE 现算文案；这里把它
    当成普通模块 import 进来、把 STATE 填成我们进程的实际值，然后直接调。
    launcher 没给 pywebview 传 menu，windowDidBecomeKey 不会重建菜单（cocoa.py
    只在 i.menu 非空时重建），所以改接一次就是永久的。

    线程纪律：**改主菜单只能在主线程**（非主线程改会抛
    NSInternalInconsistencyException "Main menu contents may only be modified
    from the main thread"，实测）。时序上 on_loaded 触发时 first_show 可能还没
    建菜单——所以这里不睡觉硬等，找不到就交回 runloop，0.5s 后再试（最多 20 次，
    10 秒），全失败保留系统面板（不会更糟）。
    """
    global _ABOUT_MENU_TARGET
    try:
        import AppKit
        import tray_helper as TH   # 提前 import：模块级 pyobjc 依赖在主进程也可用
        TH.STATE["port"] = port
        TH.STATE["data_dir"] = _data_dir()
        TH.STATE["running"] = True

        if _ABOUT_MENU_TARGET is None:
            class _AboutTarget(AppKit.NSObject):
                def showAbout_(self, sender):
                    try:
                        import tray_helper as _TH
                        _TH.STATE["running"] = True
                        _TH._show_about()
                    except Exception as e:
                        print("[launcher] 关于面板弹不出：%s" % e)
            _ABOUT_MENU_TARGET = _AboutTarget.alloc().init()

        state = {"tries": 0}

        def attempt():
            try:
                if _rewire_about_menu_once(port):
                    return
                state["tries"] += 1
                if state["tries"] >= 20:
                    print("[launcher] 10 秒内没找到「关于」菜单项，保留系统面板")
                    return
                AppHelper.callLater(0.5, attempt)
            except Exception as e:
                print("[launcher] 改接尝试失败：%s" % e)

        from PyObjCTools import AppHelper
        if threading.current_thread() is threading.main_thread():
            attempt()
        else:
            # 从非主线程进来（测试/兜底路径）：主菜单只能在主线程改，转投主线程。
            AppHelper.callAfter(attempt)
    except Exception as e:
        print("[launcher] 改接「关于」菜单失败：%s" % e)
    return False


def _quit_now(window):
    """真正退出：先置标志放行 should_close，再关掉唯一的窗口。

    pywebview 会在最后一个窗口关闭后从 start() 返回；兜一个 3 秒的补救线程，
    万一它还赖着不走（隐藏过的窗口有时不触发退出），就直接硬退。
    """
    _QUIT["flag"] = True

    def _do():
        _kill_tray_helper()
        if window is None:
            return
        try:
            window.destroy()
        except Exception as e:
            print("[launcher] 关闭窗口失败：%s" % e)

    _on_main(_do)

    def _bail():
        time.sleep(3.0)
        print("[launcher] 窗口已关但进程还在，强制退出")
        sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=_bail, daemon=True, name="fxseek-bail").start()


try:
    import AppKit as _AppKit

    class _TrayTarget(_AppKit.NSObject):
        """菜单栏图标的菜单项回调。

        pyobjc 要求 target 是个 Objective-C 对象；而且必须有人一直引用着
        （存在 _QUIT["tray_target"] 里），否则一 GC 菜单点下去就是悬空指针。
        """

        def togglePanel_(self, sender):
            """点第一项：藏着就打开，开着就收起。"""
            if _QUIT.get("hidden"):
                _from_tray()
            else:
                w = _QUIT.get("window")
                if w is not None:
                    _to_tray(w)

        def quitApp_(self, sender):
            _quit_now(_QUIT.get("window"))

except Exception:            # 非 macOS / 没有 pyobjc：静默退化，不做托盘
    _TrayTarget = None


# ---- 菜单栏图标：交给独立子进程去做 ----
#
# 为什么不在自己进程里建 NSStatusItem：macOS 对**由 LaunchServices 直接启动的
# 进程**（用户双击 .app 那种）不给排菜单栏图标 —— 图标窗口高度永远是 0，看不见
# 也不报错。这不是我们的 bug：写了个最小的纯 AppKit 程序对照，同一个程序双击
# 启动排不上、终端直接 exec 启动就正常；而**由别的进程 exec 出来的子进程**两种
# 情况下都排得上。所以图标交给 tray_helper.py，主程序跟它用本机 HTTP 说一句话
# （路由在 app.py：/v1/panel/show、/v1/panel/hide、/v1/app/quit）。
_TRAY_PIDFILE = ".tray.pid"
# keeper 最多重开几个助手：真出事时别无限刷进程（历史上 4 次够用，这里留点余量）
_TRAY_KEEPER_MAX = 6


def _quit_requested():
    """用户是不是已经决定退出了（窗口关掉那条路也会走这里）。

    keeper 靠它区分「助手自己没了，重开一个」和「主程序要关了，别添乱」。
    _QUIT["flag"] 在 _quit_now 里置位；但菜单栏那条退出路径（托盘里的
    「退出 FXseek」）也走 _quit_now，所以一个标志就够。
    """
    return bool(_QUIT.get("flag"))


def _tray_pid_path():
    try:
        return os.path.join(_data_dir(), _TRAY_PIDFILE)
    except Exception:
        return None


def _kill_tray_helper():
    """收掉上一个图标助手：不然重启一次菜单栏就多一个点不动的图标。"""
    pids = []
    proc = _QUIT.get("tray_proc")
    if proc is not None and proc.poll() is None:
        pids.append(proc.pid)
    pf = _tray_pid_path()
    if pf and os.path.isfile(pf):
        try:
            with open(pf) as f:
                n = int((f.read() or "0").strip() or 0)
            if n and n not in pids:
                pids.append(n)
        except Exception:
            pass
        try:
            os.remove(pf)
        except Exception:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    _QUIT["tray_proc"] = None


def _spawn_tray_helper(port):
    """起一个菜单栏图标助手（子进程 → 系统才会给它排位置）。"""
    if _QUIT.get("tray_proc") is not None and _QUIT["tray_proc"].poll() is None:
        return
    helper = os.path.join(HERE, "tray_helper.py")
    if not os.path.isfile(helper):
        print("[launcher] 找不到 tray_helper.py，跳过菜单栏图标")
        return
    _kill_tray_helper()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", helper, "--port", str(port),
             "--parent", str(os.getpid()),
             # 助手跟主程序说话还有第二条通道：命令文件（服务停掉以后 HTTP 用不了，
             # 而「启动服务器」正好要在这时候用）。文件放用户数据目录。
             "--data-dir", _data_dir()])
    except Exception as e:
        print("[launcher] 启动菜单栏图标助手失败：%s" % e)
        return
    _QUIT["tray_proc"] = proc
    pf = _tray_pid_path()
    if pf:
        try:
            with open(pf, "w") as f:
                f.write(str(proc.pid))
        except Exception:
            pass


def _tray_keeper(port):
    """助手自己认输退出（退出码 2）时重开一个 —— 换进程还有机会。

    为什么要有这个：这个系统里 NSStatusItem 摆不摆得上跟进程当时的状态有关，
    同一个进程里反复重建不一定能救回来，换个新进程往往就好了。

    ★ 补一个保险（用户报「菜单栏图标过一段时间就消失」）：万一助手不是走
    os._exit(2) 而是别的路径没了（比如被系统当成「异常终止」直接回收，
    poll() 拿到的是负的信号号而不是 2），老代码 `if rc != 2: return` 就直接
    不管了 —— 图标从此没人管，永久消失。所以：**主程序还活着**的时候，
    助手无论以什么码消失都重开；但最多 _TRAY_KEEPER_MAX 次，免得真出事时刷屏。
    主程序自己退出时 _QUIT["flag"] 已置位、或 _QUIT["tray_proc"] 被清成 None，
    两条都会让这里安静地退出。
    """
    tries = 0
    while tries < _TRAY_KEEPER_MAX:
        time.sleep(6.0)
        proc = _QUIT.get("tray_proc")
        if proc is None:
            return                     # 主程序在收尾了（_kill_tray_helper 清空的）
        rc = proc.poll()
        if rc is None:
            continue                   # 助手还活着，不用管
        if _quit_requested():
            return                     # 主程序要关了，别添乱
        if rc == 2:
            why = "没摆上图标，自己认输了"
        else:
            why = "意外退出（rc=%s）" % rc
        tries += 1
        print("[launcher] 菜单栏助手%s，重开一个（第 %d 次）" % (why, tries))
        sys.stdout.flush()
        _QUIT["tray_proc"] = None
        _spawn_tray_helper(port)


def _install_context_menu_hook():
    """把右键菜单里的「拷贝/全选」放回来。

    pywebview 在 cocoa.py 里有一条（非 debug 时）：

        def willOpenMenu_withEvent_(self, menu, event):
            if not _state['debug']:
                menu.removeAllItems()

    它把 WKWebView 已经建好的原生菜单**整份清空**，于是右键什么都没有 ——
    用户选了字也拷不走（配合 text_select，两个缺一个都不行）。它的本意是
    「别让用户看见 Reload / Back 这类网页菜单」，但连拷贝一起干掉就过了。

    这里改成**黑名单**：只摘掉明确的网页导航/开发类条目，其余一律保留。
    这么写是刻意的 —— 白名单一旦猜错 action 名，用户又会回到「拷不走」，
    而黑名单最多是菜单里多留一项，永远不会把「拷贝」弄丢。

    判断顺序：identifier（WKMenuItemIdentifier*，最可靠，不受界面语言影响）
    → action 选择器 → 标题兜底。三条都不认识就当它是好条目留着。
    """
    # WebKit 内置菜单项的 identifier（macOS 上就是这些字符串）
    DROP_IDS = {
        "WKMenuItemIdentifierGoBack",
        "WKMenuItemIdentifierGoForward",
        "WKMenuItemIdentifierReload",
        "WKMenuItemIdentifierStop",
        "WKMenuItemIdentifierOpenLink",
        "WKMenuItemIdentifierOpenLinkInNewWindow",
        "WKMenuItemIdentifierOpenLinkInNewWindowWithConfiguration",
        "WKMenuItemIdentifierOpenMediaInNewWindow",
        "WKMenuItemIdentifierDownloadLinkedFile",
        "WKMenuItemIdentifierDownloadMedia",
        "WKMenuItemIdentifierShareMenu",
        "WKMenuItemIdentifierInspectElement",
        "WKMenuItemIdentifierShowWebInspector",
    }
    # 没有 identifier 时的兜底：按 action 选择器
    DROP_ACTIONS = {
        "reload:", "stopLoading:", "goBack:", "goForward:",
        "orderFrontStandardAboutPanel:", "_showWebInspector:",
    }
    # 最后兜底：按标题（只在中英文下覆盖最常见的几个导航词）
    DROP_TITLES = {
        "重新加载", "重载", "返回", "前进", "停止", "检查元素", "开发人员工具",
        "reload", "back", "forward", "stop", "inspect element",
    }

    def _should_drop(it):
        # identifier 优先
        try:
            ident = it.identifier()
        except Exception:
            ident = None
        if ident:
            if str(ident) in DROP_IDS:
                return True
            # identifier 认得出但不在名单里 → 是 WebKit 的编辑类条目，留着
            return False
        # 没有 identifier（自定义/分隔线）再看 action
        try:
            act = str(it.action() or "")
        except Exception:
            act = ""
        if act and act not in ("", "None"):
            return act in DROP_ACTIONS
        # 都没有就按标题兜底，认不出的一律留着
        try:
            title = str(it.title() or "").strip().lower()
        except Exception:
            title = ""
        return title in DROP_TITLES

    def _will_open_menu(self, menu, event):
        try:
            items = list(menu.itemArray())
        except Exception:
            return
        for it in items:
            try:
                if it.isSeparatorItem():
                    continue
            except Exception:
                pass
            try:
                if _should_drop(it):
                    menu.removeItem_(it)
            except Exception:
                # 单项判断出错就当它该留着 —— 宁可多一项也不能少「拷贝」
                pass
        # 顺手清掉摘完条目后可能出现的首尾分隔线
        try:
            items = list(menu.itemArray())
            for it in items:
                if it.isSeparatorItem():
                    menu.removeItem_(it)
                else:
                    break
            items = list(menu.itemArray())
            for it in reversed(items):
                if it.isSeparatorItem():
                    menu.removeItem_(it)
                else:
                    break
        except Exception:
            pass

    try:
        from webview.platforms import cocoa as _cocoa

        _cocoa.BrowserView.WebKitHost.willOpenMenu_withEvent_ = _will_open_menu
        return True
    except Exception as e:
        print("[launcher] 右键菜单补丁没装上：%s" % e)
        return False


def _install_panel_hooks(APP):
    """把「打开面板 / 隐藏面板 / 退出」三个口子交给 app.py 的 HTTP 路由。

    菜单栏图标是独立进程（tray_helper.py），它够不到我们的窗口对象，只能走
    本机 HTTP；命令行直接跑 app.py 时没人注册，那三个口子会老实回「只在桌面版
    里可用」。
    """
    try:
        APP.PANEL_HOOKS.update({
            "show": lambda: _from_tray(),
            "panel_toggle": _tray_toggle_panel,
            "hide": lambda: _to_tray(_QUIT.get("window")),
            "quit": lambda: _quit_now(_QUIT.get("window")),
            # 菜单栏「设置…」：把窗口带到设置页（MCP 那一条直接跳到 MCP 那一节）。
            "settings": lambda: _tray_open_settings(""),
            "settings_mcp": lambda: _tray_open_settings("?sec=sec-mcp"),
            # 「停止 / 启动服务器」：服务本身归 app.py 管，但窗口得跟着一起处理 ——
            # 服务一停窗口里那页就是死的，先收进菜单栏；起回来再刷新并还回来。
            "server_stop": _tray_server_stop,
            "server_start": _tray_server_start,
            # 原生「选择文件夹」/「导出配置」面板：替代 osascript（后者会起独立
            # 进程、在程序坞里多亮一个图标）。
            "pick_folder": _pick_folder_native,
            "export_file": _export_file_native,
            # ---- 工作模式 ----------------------------------------------------
            # app.py 只管「设置里 ui_mode 是什么」和「后台冻不冻」；
            # 窗口该藏该显全部落在这里。两边靠这几个钩子对接。
            "ui_mode": _work_apply_mode,
            "work_hotkey": _work_rebind_hotkey,
            "work_hotkey_pick": _work_hotkey_pick,
            "work_show": lambda: _work_show("界面调用"),
            "work_hide": lambda: _work_hide("界面调用"),
            "work_dismiss": lambda: _work_dismiss("界面调用"),
            # 光标位置。绳窗收不到 mousemove（它是穿透点击的），「鼠标划过明信片
            # 像拨铃铛」和「停 3 秒自动叫出搜索层」都得靠宿主转达。
            "work_pointer": _WORKWIN.pointer_info,
        })
    except Exception as e:
        print("[launcher] 注册面板钩子失败：%s" % e)

    # 麦克风：桌面版走原生录音（AVFoundation），因为从访达启动的 app 里
    # WKWebView 根本不暴露 navigator.mediaDevices（见 recorder.py 顶部说明）。
    try:
        import recorder as REC
        mic_dir = os.path.join(_data_dir(), "mic")
        APP.PANEL_HOOKS.update({
            "mic_status": REC.status,
            "mic_request": REC.request_access,
            "mic_start": lambda: REC.start(mic_dir),
            "mic_stop": REC.stop,
            "mic_cancel": REC.cancel,
            "mic_level": REC.level,
            "mic_open_settings": REC.open_settings,
        })
        print("[launcher] 原生录音已就绪（目录 %s）" % mic_dir)
    except Exception as e:
        print("[launcher] 注册录音钩子失败：%s" % e)


def _work_cfg():
    """读工作模式的设置（转发到 app.py，别在这儿再解析一遍）。"""
    try:
        import app as APP
        return APP._work_cfg()
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 读工作模式设置失败：%s" % e)
        return {"mode": "leisure", "hotkey": "", "out_dir": ""}


def _work_show(reason=""):
    if _WORKWIN.window() is None:
        # 浏览器里跑 app.py 时没人注册这块窗。**这不是错误**，如实回一句，
        # 让调用方（设置页 / 托盘）能区分「做成了」和「这里根本没有窗口」。
        return {"ok": True, "window": False, "note": "窗口层不在（不是桌面版）"}
    return _WORKWIN.show(reason)


def _work_hide(reason=""):
    if _WORKWIN.window() is None:
        return {"ok": True, "window": False, "note": "窗口层不在（不是桌面版）"}
    return _WORKWIN.hide(reason)


def _work_dismiss(reason=""):
    """只收**搜索层**，绳窗和上面的明信片留着（那是暂存区）。

    和 `_work_hide`（退出整个工作模式）是两件事：Es​c / 点空白 / 1 分钟没动
    都走这条，收完按唤醒键又能把同一批明信片叫回来接着收。
    """
    if _WORKWIN.window() is None:
        return {"ok": True, "window": False, "note": "窗口层不在（不是桌面版）"}
    return _WORKWIN.dismiss(reason)


def _tray_toggle_panel():
    """菜单栏图标那一下：面板开着就收进菜单栏，收着就还回来。

    ★ 判据只用 `_QUIT["hidden"]` 这个**我们自己在维护**的标记，不去问
      pywebview 的 `window.shown` —— 那个字段的语义是「**曾经**显示过没有」
      （`first_show()` 里才 set），拿它当「此刻可见吗」用，结果就是第一次点
      图标时 `shown` 还是 False、于是又走「显示」那一支，看着像点了没反应。
    """
    # 工作模式的覆盖窗在屏上：那一下先收它（它是迭在最上面的那层）。
    try:
        import workwin as _W
        if _W.is_open():
            print("[launcher] 图标左键：先收工作模式覆盖窗")
            return _W.dismiss("图标左键")
    except Exception:
        pass
    if _QUIT.get("hidden"):
        print("[launcher] 图标左键：面板收着 → 还回来")
        return _from_tray()
    w = _QUIT.get("window")
    if w is None:
        print("[launcher] 图标左键：没有窗口句柄，改走显示")
        return _from_tray()
    print("[launcher] 图标左键：面板开着 → 收进菜单栏")
    return _to_tray(w)


def _work_apply_mode():
    """app.py 写完 ui_mode 之后回调这里：把窗口摆成对应的样子。

    app.py 那边负责「设置 + 记模式」，这里只负责窗口。分这么清楚是因为
    命令行跑的 app.py 没有窗口，它照样要能改这个值。
    """
    mode = (_work_cfg().get("mode") or "leisure")
    res = (_work_show("切到工作模式") if mode == "work"
           else _work_hide("切回休闲模式"))
    # ★★ 范老板 2026-10-16 第①条：「休闲模式关掉窗口 → 切工作模式 → 切回休闲，
    #    点程序坞 FXseek 唤不醒窗口」。
    #    根因：pywebview 每 create_window 一次都会
    #    `app.setDelegate_(BrowserView._shared_app_delegate)`（cocoa.py:615），
    #    **把我们那个管 applicationShouldHandleReopen 的 AppDelegate 顶掉**。
    #    而工作模式那两层窗是**懒创建**的（第一次切工作模式才建）—— 正好在这一刻
    #    把代理换走。此后 App 已经在前台，点 Dock 图标系统只发 reopen 这一路，
    #    而它已经不在我们手上，于是「切过工作模式之后就再也唤不回窗口」。
    #    修法：每次模式切换完重装一遍钩子。幂等：类只定义一次、observer 复用。
    try:
        _install_reopen_hook()
        # 装完立刻验一遍：`NSApp().delegate()` 到底是不是我们那个子类。
        # 只打「已重装」不算数 —— 代理被顶掉时 setDelegate_ 是不报错的。
        ok = None
        try:
            import AppKit as _ak
            ok = isinstance(_ak.NSApp().delegate(), _reopen_delegate_class())
        except Exception:                                   # noqa: BLE001
            ok = None
        print("[launcher] 模式切换后重装 Dock 唤回钩子（%s）· 代理已归位=%s"
              % (mode, ok), flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 重装 Dock 唤回钩子失败：%s" % e, flush=True)
    return res


def _work_hotkey_fired():
    """唤醒键被按下（Carbon 事件，跑在**主线程**上）。

    只有工作模式下才响应 —— 用户的原话是「切换『工作模式』后，根据设置里的
    自定义键唤醒」。休闲模式下按它什么都不该发生，否则等于给全系统加了个
    什么都不做的热键，还白占一个组合。
    """
    if (_work_cfg().get("mode") or "leisure") != "work":
        return
    # ★ 这行是「唤醒键到底有没有收到」的唯一证据：Carbon 热键收不到合成按键
    #   （见 hotkey.py 顶部），自动化测不了，只能靠真人按一次 + 这行日志。
    #   flush=True 同理，不能让它卡在缓冲区里。
    print("[launcher] ★ 唤醒键命中（人按的）", flush=True)
    try:
        # ★ 只「唤醒」，不「切换」：范老板 2026-10-14 明确要求
        #   「快捷键只负责唤醒搜索框，搜索框已存在的情况下按快捷键无效」。
        #   做成切换的话，这个键就有两种含义，按下去之前不知道会得到哪个；
        #   而「收起来」已经有 Esc / 点空白 / 1 分钟自动收三条路，不缺这一个。
        _WORKWIN.wake("唤醒键")
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 唤醒键处理失败：%s" % e)


def _work_rebind_hotkey():
    """设置里改了唤醒键之后重注册。返回 `{ok, error?}` 给设置页。

    ★ 这条是从 **HTTP 线程**打上来的（设置页 POST /v1/work/hotkey），而
      `InstallEventHandler` 必须挂在主线程的派发器上 —— 从 HTTP 线程直接挂
      不会报错、只会永远收不到。所以转主线程**并等返回值**（设置页要靠它
      显示「这个组合被别的软件占了」这类失败原因）。
    """
    if _WORKWIN.window() is None:
        return {"ok": True, "window": False, "note": "窗口层不在（不是桌面版）"}
    combo = _work_cfg().get("hotkey") or ""
    r = _on_main_sync(_WORKWIN.bind_hotkey, combo, _work_hotkey_fired)
    if isinstance(r, dict) and "ok" in r:
        return r
    ok, why = r
    if not ok:
        return {"ok": False, "error": why}
    return {"ok": True, "hotkey": combo}


# --------------------------------------------------------------------------
# 录唤醒键（必须在**主进程**里录）
# --------------------------------------------------------------------------
# ★ 为什么录键不能放在菜单栏那个 tray_helper 进程里：
#   tray_helper 是个没有窗口的辅助进程，它的 NSApplication 激活策略是
#   **Prohibited**（实测 `activationPolicy()` == 2）。这种进程
#   `activateIgnoringOtherApps_(True)` 是空操作、`isActive()` 永远是 False，
#   它的窗口因此永远当不了 key window —— 键盘事件一条都收不到。之前那版把
#   提示条和监听器都放在 tray 里，表现就是「胶囊弹出来了、按什么都没反应、
#   也不会自己消失」。主进程本来就是有窗口的 GUI 进程（关闭确认框、关于面板
#   都在这里 `runModalForWindow_`），录键放这儿一次就通。
def _work_hotkey_pick():
    """让用户按一下新的唤醒键。返回 `{ok, hotkey}` 或 `{ok:False, cancelled:True}`。"""
    try:
        return _on_main_sync(_work_hotkey_pick_main, timeout=40.0)
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _work_hotkey_pick_main():
    import AppKit as AK
    from PyObjCTools import AppHelper
    import hotkey as HK

    W, H = 560, 62
    win = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AK.NSMakeRect(0, 0, W, H), AK.NSWindowStyleMaskTitled,
        AK.NSBackingStoreBuffered, False)
    win.setTitleVisibility_(AK.NSWindowTitleHidden)
    win.setTitlebarAppearsTransparent_(True)
    win.setMovableByWindowBackground_(True)
    win.setReleasedWhenClosed_(False)
    win.setOpaque_(False)
    win.setBackgroundColor_(AK.NSColor.clearColor())
    win.setLevel_(AK.NSModalPanelWindowLevel)
    try:
        sf = AK.NSScreen.mainScreen().visibleFrame()
        win.setFrameOrigin_(AK.NSMakePoint(
            sf.origin.x + (sf.size.width - W) / 2.0, sf.origin.y + 92.0))
    except Exception:
        win.center()
    cv = win.contentView()
    cv.setWantsLayer_(True)
    host = cv
    try:
        fx = AK.NSVisualEffectView.alloc().initWithFrame_(cv.bounds())
        fx.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
        fx.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
        fx.setState_(AK.NSVisualEffectStateActive)
        fx.setWantsLayer_(True)
        fx.layer().setCornerRadius_(31.0)
        fx.layer().setMasksToBounds_(True)
        fx.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
        cv.addSubview_(fx)
        host = fx
    except Exception:
        pass
    lbl = AK.NSTextField.alloc().initWithFrame_(AK.NSMakeRect(14, 21, W - 28, 20))
    lbl.setStringValue_(_tr("按一下新的唤醒键（单键也可以）· Esc 取消",
                            "Press the new wake key (a bare key is fine) · Esc to cancel"))
    lbl.setFont_(AK.NSFont.boldSystemFontOfSize_(13))
    lbl.setTextColor_(AK.NSColor.labelColor())
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    lbl.setAlignment_(AK.NSTextAlignmentCenter)
    lbl.setAutoresizingMask_(AK.NSViewWidthSizable)
    host.addSubview_(lbl)

    st = {"combo": "", "done": False, "timers": []}

    def _timer(sec, fn):
        """★ 定时器必须挂在 **modal 运行模式** 上：modal 循环里默认模式的
        NSTimer 不会触发，用 AppHelper.callLater 会静默失效。"""
        try:
            t = AK.NSTimer.timerWithTimeInterval_repeats_block_(sec, False,
                                                               lambda _t: fn())
            AK.NSRunLoop.currentRunLoop().addTimer_forMode_(
                t, AK.NSModalPanelRunLoopMode)
            st["timers"].append(t)
        except Exception:
            pass

    def _kill_timers():
        """★ 收尾一定要把没触发的定时器**作废**：上一轮那个 8 秒兜底如果还挂在
        运行循环上，等下一轮 modal 一开始它就会立刻 `stopModal()` —— 实测表现
        就是「第二次点设置唤醒键，面板一闪而过、什么也没录到」（20ms 就返回）。"""
        for t in st["timers"]:
            try:
                t.invalidate()
            except Exception:
                pass
        st["timers"] = []

    def _close():
        try:
            AK.NSApp().stopModal()
        except Exception:
            pass

    def on_key(ev):
        if st["done"]:
            return ev
        try:
            kc, fl = int(ev.keyCode()), int(ev.modifierFlags())
        except Exception:                                   # noqa: BLE001
            return None
        name = HK.NAME_BY_KEYCODE.get(kc)
        if name is None:
            return None                    # 不认识的键：吞掉，别漏给别的程序
        mods = fl & ((1 << 20) | (1 << 19) | (1 << 18) | (1 << 17))
        if not mods and name in ("esc", "escape"):
            st["done"] = True
            _close()
            return None
        combo = HK.from_keycode(kc, fl)
        if not combo:
            return None
        st["combo"] = combo
        st["done"] = True
        try:
            lbl.setStringValue_("已改成 " + (HK.display(combo) or combo))
            lbl.setTextColor_(AK.NSColor.systemGreenColor())
        except Exception:
            pass
        _timer(0.9, _close)                # 让确认文案停一下再收走
        return None

    mon = AK.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(1 << 10, on_key)
    try:
        # ★ 先把自己激活**并且真的等到激活完成**再 makeKey：这个 App 平时可能
        #   根本不是活跃 App（用户是从别的前台程序点菜单栏进来的）。实测「第一次
        #   录键必超时、第二次就好」就是这个原因 —— 第一次调用时 `isActive()` 还是
        #   False，窗口 orderFront 了但成不了 key window，键盘根本送不进来；
        #   等激活落地（或最多 1.2 秒）再抢焦点，第一次就能录到。
        AK.NSApp().activateIgnoringOtherApps_(True)
        try:
            for _ in range(24):
                if AK.NSApp().isActive():
                    break
                AK.NSApp().nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AK.NSEventMaskAny,
                    AK.NSDate.dateWithTimeIntervalSinceNow_(0.05),
                    AK.NSDefaultRunLoopMode, True)
        except Exception:
            pass
        win.makeKeyAndOrderFront_(None)
        try:
            win.makeKeyWindow()
        except Exception:
            pass
        _timer(8.0, _close)                # 8 秒没按就自己收走
        AK.NSApp().runModalForWindow_(win)
    finally:
        try:
            AK.NSEvent.removeMonitor_(mon)
        except Exception:
            pass
        _kill_timers()
        try:
            win.orderOut_(None)
        except Exception:
            pass
    if st["combo"]:
        return {"ok": True, "hotkey": st["combo"]}
    return {"ok": False, "cancelled": True}


def _work_boot():
    """启动时把工作模式摆到位：注册唤醒键 + 一律先按休闲模式起来。

    必须在**主线程、应用循环已经跑起来之后**调（见 hotkey.py 顶部）。
    """
    if _WORKWIN.window() is None:
        return
    combo = _work_cfg().get("hotkey") or ""
    ok, why = _WORKWIN.bind_hotkey(combo, _work_hotkey_fired)
    # ★ flush=True：这行是排查「按唤醒键没反应」的唯一线索，而 print 到文件是
    #   块缓冲的 —— 上次没加，日志里整行都没落盘，害我以为 _work_boot 压根没跑
    #   （真相是 SIGTERM 下 stdio 不刷缓冲）。这种诊断信息不能靠运气。
    print("[launcher] 唤醒键 %s：%s" % (combo or "(未设)", "已注册" if ok else why),
          flush=True)
    # ★ 每次启动**一律进休闲模式**，不看上次停在哪儿（范老板 2026-10-14：
    #   「工作模式是可选模式，每次重新启动还是默认进休闲模式」）。
    #   工作模式是一副「戴上去干活、干完摘掉」的眼镜，不是默认住处；而且它是
    #   一块盖住整屏的透明窗，忘了自己还在工作模式里、开机就撞上整屏浮层，
    #   比多按一次唤醒键糟糕得多。
    #   ★ 2026-10-18 起工作模式不再暂停后台，所以这里没有「闸门要松」这回事；
    #   仍然走 app.py 的 work_set_mode 而不是自己改设置：写盘、记模式、回调
    #   本函数这一套必须一起发生，散在两边早晚会漏一个。
    if (_work_cfg().get("mode") or "leisure") != "leisure":
        try:
            import app as APP                              # 和 _work_cfg 一样就近 import
            APP.work_set_mode("leisure")
        except Exception as e:                              # noqa: BLE001
            print("[launcher] 启动归位到休闲模式失败：%s" % e, flush=True)
    print("[launcher] 启动一律进休闲模式（ui_mode 已归位）", flush=True)

def _work_reload(port):
    """服务通了之后，确认覆盖窗的页面真加载出来了；没有就补一发。

    `port` 显式传进来，不从窗口的 `original_url` 里反推 —— 端口被占时
    `_free_port` 会换一个，两边一旦对不上，覆盖窗就指着一个没人听的端口，
    表现是「窗口出来了、一片空白」，查起来极费劲。
    """
    try:
        time.sleep(1.0)
        # ensure_loaded 会两扇窗都查一遍（搜索窗 + 绳窗）
        r = _WORKWIN.ensure_loaded("http://127.0.0.1:%d/work" % port)
        print("[launcher] 窗口补加载：%r" % (r,), flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 覆盖窗补加载失败：%s" % e, flush=True)


def _tray_open_settings(qs=""):
    """菜单栏「设置…」：把面板还回来并跳到设置页。

    `?sec=sec-mcp` 这种带上就是「直接翻到某一节」，settings.html 认这个参数。
    """
    w = _QUIT.get("window")
    api = _QUIT.get("api") or ""
    if not api:
        return {"error": "服务地址还没就绪"}
    _from_tray()
    if w is None:
        return {"error": "窗口还没建出来"}
    try:
        w.load_url("http://%s/settings%s" % (api, qs))
        return {"ok": True, "url": "/settings" + qs}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _tray_server_stop():
    """停服务 + 收窗口。页面是服务端给的，服务停了它只会是一片空白，
    所以先收进菜单栏，等「启动服务器」时再刷新出来。"""
    import app as APP
    res = APP.server_stop()
    try:
        _to_tray(_QUIT.get("window"))
    except Exception as e:
        print("[launcher] 停服务后收窗口失败：%s" % e)
    return res


def _tray_server_start():
    """把服务架回来，刷新窗口并还回来。"""
    import app as APP
    res = APP.server_start()
    w = _QUIT.get("window")
    api = _QUIT.get("api") or ""
    if w is not None and api:
        try:
            w.load_url("http://%s/" % api)
        except Exception as e:
            print("[launcher] 起服务后刷新窗口失败：%s" % e)
    try:
        _from_tray()
    except Exception:
        pass
    return res


def _pick_folder_native():
    """原生「选择文件夹」面板（NSOpenPanel），在主线程跑。

    为什么弃用 osascript 的 `choose folder`：那玩意儿是起一个**独立的 osascript
    进程**去弹系统面板，弹出来那一刻系统会把它当成一个新应用在程序坞里短暂
    亮一个图标（用户报「只要弹窗面板就在程序坞新增一个图标」）。用 NSOpenPanel
    在自己进程主线程跑，面板属于 FXseek 自己，不会多出图标。
    """
    try:
        import AppKit
    except Exception:
        return {"cancelled": True, "error": "缺少 AppKit"}
    res = {"cancelled": True}

    def _run():
        try:
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setTitle_(_tr("选择要建立索引的文件夹", "Choose a folder to index"))
            panel.setPrompt_(_tr("选择", "Choose"))
            panel.setCanCreateDirectories_(True)
            if panel.runModal() == AppKit.NSModalResponseOK:
                url = panel.URL()
                if url is not None:
                    res["path"] = url.path()
                    res["cancelled"] = False
        except Exception as e:
            res["error"] = str(e)

    try:
        from PyObjCTools import AppHelper
        if AppKit.NSThread.isMainThread():
            _run()
        else:
            # 后台线程（HTTP handler）进来时，同步阻塞到主线程跑完再取结果
            import threading
            done = threading.Event()
            def _wrap():
                _run()
                done.set()
            AppHelper.callAfter(_wrap)
            done.wait(300)
    except Exception:
        _run()
    return res


def _export_file_native():
    """原生「另存为」面板（NSSavePanel），在主线程跑。理由同 _pick_folder_native。"""
    try:
        import AppKit
    except Exception:
        return {"cancelled": True, "error": "缺少 AppKit"}
    res = {"cancelled": True}

    def _run():
        try:
            import app as APP
            payload = APP.export_config()
            default_name = "fxseek-config-%s.json" % time.strftime("%Y%m%d-%H%M%S")
            panel = AppKit.NSSavePanel.savePanel()
            panel.setTitle_(_tr("导出配置到…", "Export configuration to…"))
            panel.setNameFieldStringValue_(default_name)
            panel.setAllowedFileTypes_(["json"])
            panel.setCanCreateDirectories_(True)
            if panel.runModal() == AppKit.NSModalResponseOK:
                url = panel.URL()
                if url is not None:
                    path = url.path()
                    if not path.lower().endswith(".json"):
                        path += ".json"
                    with open(path, "w", encoding="utf-8") as f:
                        import json as _json
                        _json.dump(payload, f, ensure_ascii=False, indent=2)
                    res["ok"] = True
                    res["path"] = path
                    res["cancelled"] = False
        except Exception as e:
            res["error"] = str(e)

    try:
        from PyObjCTools import AppHelper
        if AppKit.NSThread.isMainThread():
            _run()
        else:
            import threading
            done = threading.Event()
            def _wrap():
                _run()
                done.set()
            AppHelper.callAfter(_wrap)
            done.wait(300)
    except Exception:
        _run()
    return res


def _tray_cmd_path():
    return os.path.join(_data_dir(), "tray_cmd.json")


def _tray_cmd_watch():
    """菜单栏助手的第二条通道（命令文件）。

    为什么需要它：助手跟主程序说话一直走本机 HTTP，可**服务停掉以后这条线就断了**
    —— 而「启动服务器」恰恰要在这时候用。所以服务不可用时助手往用户数据目录写
    一份 {"cmd": "server_start"}，这边轮询 mtime，看到新命令就照做。
    正常（服务在跑）时走 HTTP，这条路只是兜底。
    """
    path = _tray_cmd_path()
    # ★ seen 必须用「当前 mtime」当基线，不能是 None。
    # None 意味着"还没看过"，所以启动后第一轮（sleep 1.0s 之后）就会把磁盘上
    # **上一次会话遗留的命令文件**当成新命令执行一遍 —— 于是「上次点了退出，
    # 这次一开 app 它自己又退出了」，而这正好是用户报「菜单栏图标有时候会在
    # 一段时间后消失」的另一个成因：主程序自己退出了，图标跟着走。
    try:
        seen = os.path.getmtime(path)      # 基线 = 启动前磁盘上已有的状态
    except OSError:
        seen = None
    if seen is None:
        seen = -1.0                       # 文件不存在 → 用一个永不等于 mtime 的值
    time.sleep(1.0)
    while not _QUIT.get("flag"):
        time.sleep(0.7)
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue
        if stamp == seen:
            continue
        seen = stamp
        try:
            with open(path, encoding="utf-8") as f:
                cmd = (json.load(f) or {}).get("cmd") or ""
        except Exception as e:
            print("[launcher] 读菜单命令失败：%s" % e)
            continue
        if not cmd:
            continue
        # 执行完就把命令抹掉（写回一个空命令 + 新的 mtime）：
        # 这样即便基线判断哪天又失灵，也不会重复执行同一条。
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cmd": "", "at": time.time()}, f)
            seen = os.path.getmtime(path)
        except Exception:
            pass
        print("[launcher] 收到菜单命令：%s" % cmd)
        import app as APP
        fn = APP.PANEL_HOOKS.get(cmd)
        if fn is None:
            print("[launcher] 不认识这条命令，忽略")
            continue
        try:
            print("[launcher] %s → %s" % (cmd, fn()))
        except Exception as e:
            print("[launcher] 执行菜单命令 %s 失败：%s" % (cmd, e))


def _free_port(preferred):
    """优先用 preferred；被占就往后找一个空闲的。
    用户可能自己已经开着一个 FXseek（开发机常见），不能因此起不来。"""
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    raise RuntimeError("找不到空闲端口（试过 %d 起连续 20 个）" % preferred)


def _wait_ready(port, timeout=900, on_tick=None):
    """等服务把端口监听起来。模型加载要 20~30 秒，冷启动耐心等。
    下载阶段不计入（下载在服务线程里发生在 bind 之前）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            s.close()
            return True
        except OSError:
            if on_tick:
                on_tick(int(time.time() - t0))
            time.sleep(0.4)
    return False


def main():
    ap = argparse.ArgumentParser(description="FXseek 媒体库桌面版")
    ap.add_argument("--port", type=int, default=8231)
    ap.add_argument("--model", default=None,
                    help="显式指定模型目录（默认自动找 / 下载）")
    ap.add_argument("--debug", action="store_true", help="打开 web 检查器")
    args = ap.parse_args()

    import model_dl
    import app as APP

    port = _free_port(args.port)
    print("[launcher] 选定端口 %d（--port 给的是 %d）" % (port, args.port), flush=True)
    # 记下实际监听的地址：托盘菜单里显示给用户看（MCP 连的就是这个），
    # 端口被占时 _free_port 会换一个，这时候也看得出来。
    _QUIT["api"] = "127.0.0.1:%d" % port
    # ★★ 范老板 2026-10-16 第⑤条：「菜单栏狒狒图标出现特别慢，正常应该一秒就出现」。
    #    原因：助手原来是在 **服务就绪之后**（`_wait_ready` 通过、窗口换到主界面）
    #    才起的，而服务冷启动要十几二十秒（要探模型、建索引）—— 图标就只能跟着等。
    #    但助手摆 NSStatusItem **根本不需要服务**：它只在 /health 探活和点菜单时
    #    才跟服务说话，而且探不到**绝不自杀**（见 tray_helper 的 _watch）。
    #    所以这里提前到「端口定下来」这一行就起，图标一秒内就位；后面那次 spawn
    #    （服务就绪后）因为进程还活着会直接 return，幂等无副作用。
    try:
        _spawn_tray_helper(port)
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 提前起菜单栏助手失败（不影响启动）：%s" % e, flush=True)
    prep = Prep()
    api = Api(prep)

    import webview
    window = webview.create_window(
        _tr("FXseek 媒体库", "FXseek Library"),
        html=_prep_html(),
        js_api=api,
        width=520, height=430,
        # 准备窗口是固定的小窗；主界面出来后再用 pyobjc 补上可缩放（见 _tune_main_window）
        resizable=False,
        # ★ text_select 必须显式打开：pywebview 的默认值是 False，它会给**每个**
        #   页面注入 `body{user-select:none}`（见 webview/js/customize.js），于是
        #   全应用文字都选不中、右键没有「拷贝」。用户报过「鼠标没法拷贝 app 上的
        #   文字」。界面里需要「不可选中」的地方由页面自己的 CSS 兜（按钮、胶囊、
        #   折叠块标题等），别依赖这个全局开关。
        text_select=True,
        background_color="#0f1115" if _dark() else "#f4f5f8",
    )
    prep.bind(window)
    _QUIT["window"] = window
    _arm_page_ready(prep)

    # 工作模式的覆盖窗：**必须在 webview.start() 之前登记**（pywebview 只认
    # start 之前 create_window 的窗口）。建出来是 hidden 的 —— 默认休闲模式，
    # 等唤醒键或设置里切过去再出现。
    try:
        _WORKWIN.attach_main(window)
        _WORKWIN.create(port)
    except Exception as e:                                  # noqa: BLE001
        print("[launcher] 工作模式覆盖窗没建起来：%s: %s" % (type(e).__name__, e))

    # 关闭按钮：先问「退出还是最小化到菜单栏」（见 _on_closing）。
    window.events.closing += _on_closing

    # 点 Dock 图标能把收起来的面板叫回来（装 AppDelegate 子类 + 激活通知）
    _install_reopen_hook()

    # 菜单栏图标由子进程（tray_helper.py）负责，等端口通了再起（见下面的 watch()）。
    _install_panel_hooks(APP)

    state = {"ready": False, "error": None, "model": args.model}

    def serve():
        try:
            model_path = args.model
            if not model_path:
                try:
                    found = model_dl.find_model()
                except Exception:
                    found = None
                if found:
                    model_path = found
                    # 模型已经在本地：这是热启动，压根没有「下载」这回事。
                    # 切 warm 模式把下载源按钮/百分比/字节数整块收起来，
                    # 免得用户以为又要下 1.9 GB。
                    prep.mode("warm")
                    prep.stage(_tr("正在启动", "Starting"),
                               _tr("模型已就位，正在载入（约 20 秒）",
                                   "Model in place, loading (~20s)"),
                               _tr("马上就好，不用管它", "Almost there — hang tight"))
                else:
                    prep.mode("")
                    prep.stage(_tr("正在下载模型", "Downloading model"),
                               _tr("首次启动需要下载模型（约 1.9 GB），只需一次",
                                   "First launch downloads the model (~1.9 GB) — once only"),
                               _tr("下载中可以先去忙别的，进度会自己走",
                                   "You can do other things; progress keeps running"))
                    data_dir = _data_dir()
                    model_path = model_dl.ensure_model(
                        data_dir, source=prep.source, progress=prep.progress)
                    prep.stage(_tr("正在加载模型", "Loading model"),
                               _tr("下载完成，正在读进内存（约 20 秒）",
                                   "Download complete, loading into memory (~20s)"),
                               "", pct=100)
            else:
                prep.stage(_tr("正在加载模型", "Loading model"),
                               _tr("使用指定的模型目录", "Using the specified model directory"), "")

            prep.stage(_tr("正在加载模型", "Loading model"),
                               _tr("模型就位，正在读进内存（约 20 秒）",
                                   "Model in place, loading into memory (~20s)"), "")

            # allow=[] 之外不额外放开目录：桌面包只索引用户自己加的来源。
            APP.run_server(model_path, host="127.0.0.1", port=port, allow=None)
        except Exception as e:
            state["error"] = "%s: %s" % (type(e).__name__, e)
            # 失败时保持下载模式：这时候「换个下载源」的按钮正是用户要用的
            prep.mode("")
            prep.stage(_tr("启动失败", "Startup failed"), str(e),
                       _tr("可以把上面这行发给开发者；或检查磁盘空间够不够（模型要 1.9 GB）",
                           "Send the line above to the developer; or check free disk space (model needs 1.9 GB)"))
            import traceback
            traceback.print_exc()

    th = threading.Thread(target=serve, daemon=True, name="fxseek-server")
    th.start()

    def on_loaded():
        """主线程：等端口起来后，把准备窗口换成真正的主界面。"""
        import webview as _wv

        # 窗口这会儿才由 pywebview 真正建出来，它的 create_window 会把 app delegate
        # 换成自己的那份；所以我们的 Dock 唤回钩子要在这里再装一次（覆盖它）。
        _on_main(_install_reopen_hook)
        # pywebview 的 app menu 也是这会儿才建好，把「关于」改接成我们的磨砂面板。
        _on_main(_rewire_about_menu, port)

        def watch():
            ok = _wait_ready(port)
            if not ok:
                prep.stage(_tr("启动超时", "Startup timed out"),
                           _tr("服务 15 分钟没有就绪，请重试",
                               "The server was not ready after 15 minutes, please retry"),
                           state["error"] or "")
                return
            # 端口通了，再把窗口换到 UI。此时页面本身立即可用，
            # /v1/browse 之类的首屏数据由页面自己拉。
            try:
                window.load_url("http://127.0.0.1:%d/" % port)
                window.set_title(_tr("FXseek 媒体库", "FXseek Library"))
                window.resize(WIN_W, WIN_H)
                # 主界面才允许自由缩放；顺手把最小尺寸和位置也摆正
                _tune_main_window(window)
                # 菜单栏图标：起个子进程去摆（主进程自己摆不上，见 _spawn_tray_helper）。
                # 等端口通了再起，助手才好用 /health 探活。
                _spawn_tray_helper(port)
                threading.Thread(target=_tray_keeper, args=(port,),
                                 daemon=True, name="fxseek-traykeeper").start()
                # 菜单栏助手 → 主程序的兜底通道（服务停了也要能用「启动服务器」）
                threading.Thread(target=_tray_cmd_watch,
                                 daemon=True, name="fxseek-traycmd").start()

                # 工作模式：注册全局唤醒键 + 按上次的模式把窗口摆好。
                # ★ 必须在这里（服务起来了、应用循环跑起来之后）调 ——
                #   Carbon 的 RegisterEventHotKey 在事件循环起来之前注册是收不到事件的。
                # ★ 而且必须**转回主线程**：watch 是本函数里另起的后台线程，而
                #   InstallEventHandler 是往「主线程的事件派发器」上挂回调，
                #   从别的线程挂不会报错、只是**永远收不到**（注册返回成功、
                #   按下去没反应 —— 这种沉默失败最难查）。_on_main 会判断当前
                #   是不是主线程，是就直接跑，不是才 callAfter。
                _on_main(_work_boot)

                # ★ 覆盖窗的**初次** load_url 会被 pywebview 静默丢掉，必须补一发：
                #   除主窗以外的窗口是 `_create_children` 那条后台线程建的，建完
                #   立刻 `AppHelper.callAfter(load)`，实测这一次不报错、不发请求、
                #   contentView 永远停在空 NSView —— 表现就是「唤醒键按了，窗口出来
                #   了，但一片空白」。等本地服务确实通了再补一次就正常。
                #   详见 workwin.ensure_loaded() 的注释。
                threading.Thread(target=_work_reload, args=(port,), daemon=True,
                                 name="fxseek-workreload").start()

            except Exception as e:
                print("[launcher] 切换主窗口失败：%s" % e)

        threading.Thread(target=watch, daemon=True, name="fxseek-watch").start()

    # private_mode=False 必须显式传！pywebview 的默认值是 True，而在 macOS 后端它
    # 的意思是「每次建窗都把 WebKit 的站点数据全清一遍」（cocoa.py:637 起会调
    # removeDataOfTypes…）。搜索历史、最近上传、各种界面开关都存在 localStorage 里，
    # 用默认值等于**每开一次应用就全丢**——用户看到的现象就是「示例记录删了、重启又回来」。
    #
    # 右键菜单补丁必须赶在 start() **之前**装：start() 一进去就会实例化 WebKitHost，
    # 之后再改类方法来不及（同 _install_reopen_hook 的时序要求）。
    _install_context_menu_hook()
    webview.start(on_loaded, debug=args.debug, private_mode=False)

    # 窗口关掉 = 用户退出（或者在关闭确认框里选了「退出」）。
    # 最小化到菜单栏时窗口只是 orderOut，进程还活着，不会走到这里。
    _kill_tray_helper()
    return 0


def _data_dir():
    import paths as P
    return P.describe()["data_dir"]


def _lang():
    """读设置里的界面语言（"zh"/"en"），给几个原生窗口做双语。

    准备窗口/关闭弹窗/关于面板都是原生 HTML 或 AppKit，吃不到 i18n.js，
    只能在这里读一次 settings.json 的 lang 键。读不到一律按中文。
    """
    try:
        import json
        import paths as P
        with open(P.SETTINGS_PATH, encoding="utf-8") as f:
            return (json.load(f).get("lang") or "zh")
    except Exception:
        return "zh"


def _tr(zh_s, en_s):
    """按 _lang() 二选一。"""
    return en_s if _lang() == "en" else zh_s


def _dark():
    """粗略判断系统是不是深色，用来给窗口底色定个不那么跳的初值
    （页面自己加载后会按设置里的主题重画，这里只求第一眼不闪白）。"""
    try:
        import subprocess
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2).stdout
        return "Dark" in out
    except Exception:
        return True


if __name__ == "__main__":
    sys.exit(main())
