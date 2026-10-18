#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""工作模式的**两层窗**（桌面版的第二块 UI）。

## 它是什么

工作模式要两扇窗，因为两者的「层级」要求正好相反：

| 窗 | 页面 | 层级 | 吃点击 | 什么时候在 |
|---|---|---|---|---|
| 绳窗（暂存区） | `rope.html` | 桌面层级（不打扰别的窗口） | **不吃**（穿透） | 进了工作模式就一直在 |
| 搜索窗 | `work.html` | 最高（NSStatusWindowLevel） | 吃 | 唤醒键叫出来，1 分钟没动自动收 |

绳窗只**显示**：绳子、挂在绳上的明信片、收工时的飞行动画。
搜索窗负责**交互**：搜、拖、悬停、摘除、收工按钮。
两扇窗用同一套版式常量（`ROPE_AT` 等）对齐，坐标一一对应 —— 改一个地方
两边都要改，否则明信片会和绳子错位。

## 为什么绳窗必须穿透点击

范老板的要求是「暂存区和桌面文件夹同层级，一直存在，不干扰其它窗口」。
一块铺满屏幕的窗要是不穿透，整个屏幕的点击都被它吃掉，别的程序就没法用了。
`setIgnoresMouseEvents_(True)` 之后它只画不接受事件，和桌面图标一个待遇。

## 为什么必须是透明无边框置顶

`work.html` 画的是「桌面菜单栏下面浮着一根绳子」，下面的桌面得透出来看见。
所以窗要 `transparent=True` + `frameless=True` + `on_top=True`：
透明让桌面透出来，无边框去掉标题栏和红绿灯，置顶免得被别的窗口压住。
（pywebview 的 cocoa 后端对 `transparent` 做的是 `setOpaque_(False)` +
`setBackgroundColor_(rgba,0)` + `drawsTransparentBackground`，见 cocoa.py:674。）

## 两个必须说清楚的限制

1. **窗口是按「矩形」吃点击的，透明区域照样吃。** AppKit 的命中测试只看 frame，
   不看像素 —— 透明的地方照样算在这块窗里。所以覆盖窗出来的期间，它盖住的桌面
   （包括整个屏幕）点不到。这不是偷懒，是 AppKit 的模型；想让点击穿过去得
   现场切 `ignoresMouseEvents`，那样网页自己也收不到鼠标了。
   对应的做法是：**Esc 或点空白处就收起来**（页面里做了），和 Spotlight 一样。
2. **不能靠 `CGEventPost` 自动验证热键。** 见 hotkey.py 顶部：合成的按键打不中
   Carbon 热键，只能真人按一次。

## 线程

`create()` 必须在 `webview.start()` **之前**、主线程上调（pywebview 要求所有窗口
在 start 前登记）。`show/hide/toggle` 会被热键回调和 HTTP 线程调到，pywebview 的
`show/hide` 内部走 `AppHelper.callAfter`，跨线程安全；`_tune()` 里的 pyobjc 调用
必须自己在主线程上跑，所以统一用 `AppHelper.callAfter` 包一层。
"""

import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

# 已建出来的搜索窗（全局单例；工作模式只会有一块）
_WIN = None
# 绳窗（暂存区）。和搜索窗同生共死，但显示/隐藏/层级各自独立。
_ROPE = None
_ROPE_URL = {"url": ""}
# 绳窗**想要**的层级（True=跟搜索层一起抬到最前）。★ 必须记住意图：
# `_tune_rope` 找不到 webview 时会每 0.25 秒重试一次，而每次重试都会重新
# 摆位。原来那里写死 `_set_rope_level(False)`，重试就把刚抬上去的层级又摁
# 回桌面层 —— 表现是「进工作模式后绳子怎么也不见了」，而日志一片正常。
_ROPE_FRONT = {"on": False}

# ── 搜索窗的「点得到的地方」 ──────────────────────────────────────────
#
# ★ 为什么要这个：这扇窗是**整屏大小**的（不这样拖拽出的幽灵图会被窗口边界
#   裁掉、动画也画不出去）。但整屏窗口连空白处一起吃掉点击 —— 用户在桌面
#   空白处点一下想选图标，结果什么也没发生，像系统卡了。
#
# 做法：页面把「现在真正有东西的那几块矩形」报上来，这里用 NSTimer 以
#   ~11Hz 追着鼠标位置，鼠标不在任何矩形里就把 ignoresMouseEvents 打开
#   （那一刻整窗对点击透明），进去就关掉。等于给窗口做了逐像素命中测试。
#   `None` = 页面还没报过，先**不要**忽略，否则一进工作模式什么都点不到。
_HIT = {"rects": None, "poll": None, "ignoring": None, "gmon": None, "lmon": None,
        "dmon_g": None, "dmon_l": None,
        # 光标：`ptr` = (x, y, ts)，绳窗 client 坐标；`edit` = 搜索层开着没有。
        # 两个都由 `_hit_tick()` 在主线程上刷新（AppKit 的东西不跨线程读）。
        "ptr": None, "edit": False,
        # ★ 新③：页面正在弹原生文件选择框（以图搜 / 以音搜）。见 set_picking()。
        "picking": False}
# 绳窗那套逐像素命中（只放行「收工」按钮）。`ignoring` 记当前状态，避免每
# 90ms 都往 AppKit 里写一次同样的值。
_ROPE_HIT = {"rects": [], "ignoring": None, "keyed": None}
# ★ 暂存区（绳窗）的两态（范老板 2026-10-15 第①条）：
#   待编辑态 = 压在桌面图标**下面**、鼠标完全点不到，只有狒狒和明信片在动；
#   编辑态   = 抬到桌面图标**上方**、明信片可以悬停/拖动/删除。
#   由 rope.html 自己按「鼠标在暂存区里停留满 3 秒」切换，这里只落层级。
_ROPE_EDIT = {"on": False}


def _record_pointer(nw):
    """把光标位置记下来（绳窗 client 坐标，左上原点）。

    ★ 位置**必须在 `_WIN` 的可见性判断之前**记：搜索层一收起，`_hit_tick()` 就
      在下面那行 `if not nw.isVisible(): return` 直接回去了 —— 可范老板要的两件
      事（④「停在暂存区 3 秒自动叫出搜索层」、⑤b「鼠标划过明信片像拨铃铛一样
      摆」）里，④ 恰恰只发生在**搜索层收着**的时候。以前这里只服务「逐像素点击
      穿透」，那段逻辑对隐藏的窗口毫无意义，所以早退是对的；现在多了两个消费者，
      就得把它提到前面来。
    ★ 只在绳窗可见时才有值：绳窗不在（没进工作模式）时光标位置对页面没有任何
      意义，报出去反而会让页面把桌面上一次随手的移动当成一次「拨弄」。
    """
    try:
        _HIT["edit"] = bool(nw is not None and nw.isVisible())
        nx = getattr(_ROPE, "native", None)
        if nx is None or not nx.isVisible():
            _HIT["ptr"] = None
            return
        import AppKit
        p = AppKit.NSEvent.mouseLocation()          # 屏幕坐标，原点在左下
        f = nx.frame()
        _HIT["ptr"] = (round(p.x - f.origin.x, 1),
                       round(f.origin.y + f.size.height - p.y, 1),   # 换成本窗左上原点
                       time.time())
    except Exception:                               # noqa: BLE001
        _HIT["ptr"] = None


def pointer_info():
    """页面问「光标在哪」。绳窗按 ~55ms 问一次，所以这里必须够轻。

    返回 `on` = 有可信坐标（绳窗在）、`edit` = 搜索层开着（暂存区可拖可删）。
    `age` 是这份坐标的年龄（秒）—— 页面拿它判断宿主是不是卡住了。
    """
    p = _HIT.get("ptr")
    if not p:
        return {"ok": True, "on": False, "edit": bool(_HIT.get("edit"))}
    return {"ok": True, "on": True, "x": p[0], "y": p[1],
            "age": round(max(0.0, time.time() - p[2]), 2),
            "edit": bool(_HIT.get("edit"))}


def set_rope_hit_rects(rects):
    """绳窗报上来的可点击矩形（页面上只有「收工」按钮那一条）。

    ★ 绳窗铺满整个 visibleFrame、又一直是 `setIgnoresMouseEvents_(True)`
      （不这么做它会把整个桌面的点击吞掉）。可范老板第⑤条要那个「收工」按钮
      **一直在桌面上、点得到** —— 于是给它单独做一套逐像素命中：光标落在按钮
      那一条上就临时放行，离开就立刻恢复穿透。跟搜索窗那套（set_hit_rects）
      完全同构，只是各管各的窗口。
    """
    out = []
    for r in (rects or []):
        try:
            x, y, w, h = (float(v) for v in list(r)[:4])
        except Exception:                               # noqa: BLE001
            continue
        if w > 0 and h > 0:
            out.append((x, y, w, h))
    _ROPE_HIT["rects"] = out


def _rope_hit_tick():
    """绳窗的逐像素点击穿透（只放行「收工」按钮那一块）。"""
    nx = getattr(_ROPE, "native", None)
    if nx is None:
        return
    try:
        import AppKit
        if not nx.isVisible():
            return
        rects = _ROPE_HIT.get("rects") or []
        eat = False
        if rects:
            p = AppKit.NSEvent.mouseLocation()
            f = nx.frame()
            x = p.x - f.origin.x
            y = f.origin.y + f.size.height - p.y
            M = 6.0                                     # 和搜索窗一样放宽一点
            for rx, ry, rw, rh in rects:
                if rx - M <= x <= rx + rw + M and ry - M <= y <= ry + rh + M:
                    eat = True
                    break
        if (not eat) != bool(_ROPE_HIT.get("ignoring")):
            _ROPE_HIT["ignoring"] = not eat
            nx.setIgnoresMouseEvents_(not eat)
            print("[workwin] 绳窗穿透 → %s（命中 %d 块）"
                  % ("吃点击" if eat else "穿透", len(rects)), flush=True)
        # ★★ 键盘焦点要跟着「鼠标在不在收工按钮上」走，这一步不能省：
        #   · 进「吃点击」时**必须**把绳窗设成 key window —— 光标停在按钮上
        #     只是第一步，点击能不能送到页面里是另一回事：macOS 对**非 key
        #     窗口**默认 `acceptsFirstMouse` = NO，落在它身上的**第一下点击会
        #     被整个吞掉**，按钮纹丝不动、日志里干干净净。绳窗不 key 的话
        #     「收工」永远点不动。
        #   · 离开时再把 key 还给搜索窗 —— 否则键盘焦点留在绳窗上，用户敲的
        #     字全进绳窗（它没有输入框），表现就是「唤醒了打不了字」。
        #   两边都是真实需求，只能按鼠标位置来回切，不能固定给谁。
        want_key = eat
        if want_key != bool(_ROPE_HIT.get("keyed")):
            _ROPE_HIT["keyed"] = want_key
            try:
                if want_key:
                    nx.makeKeyAndOrderFront_(None)
                else:
                    sw = getattr(_WIN, "native", None)
                    if sw is not None and sw.isVisible():
                        sw.makeKeyAndOrderFront_(None)
            except Exception as e:                      # noqa: BLE001
                print("[workwin] 切换键盘焦点失败：%s" % e, flush=True)
            # 键盘焦点变了，能收到事件的层级也跟着变，一起重算（见
            # _rope_level_value 里那段注释）。
            _call_main(_apply_rope_level)
    except Exception as e:                              # noqa: BLE001
        print("[workwin] 绳窗命中测试出错：%s" % e, flush=True)


def set_picking(on):
    """搜索页正在弹原生文件选择框（以图搜 / 以音搜）。

    ★ 范老板新③：这两个按钮以前「选完文件回来啥也没有」——因为用户在原生
    面板上点「打开」的那一记 mouseDown 会被 `_outside_click()` 判成「点了
    搜索区外面」，搜索层当场收走。
    `NSApp.modalWindow()` 兜不住它（NSOpenPanel 走的是 sheet / 异步模态，
    不是 `runModalForWindow`），所以由页面在弹框前明说一声。
    页面那边选完/取消、或者窗口重新拿到焦点时都会回一枪 `on:false`。

    ★ 范老板 2026-10-15 第②条：光「别收走」还不够 —— 搜索窗和绳窗都在
    NSStatusWindowLevel(25)，而 NSOpenPanel 是**普通窗**（实测 CGWindowList
    里是 L8）。层级一高一低，文件框整块被压在搜索区和暂存区后面，连点都点不
    到。所以弹框期间把这两扇一起压到普通层，关掉再抬回来（见 _apply_pick_level）。
    """
    on = bool(on)
    changed = (_HIT.get("picking") != on)
    _HIT["picking"] = on
    if changed:
        _call_main(_apply_pick_level)
    return {"ok": True, "picking": on}


def _apply_pick_level():
    """弹/收原生文件框时，切换「搜索窗 + 绳窗」的层级（见 set_picking 的注释）。"""
    picking = bool(_HIT.get("picking"))
    try:
        import AppKit
        nw = getattr(_WIN, "native", None) if _WIN is not None else None
        if nw is not None:
            lvl = 0 if picking else AppKit.NSStatusWindowLevel
            nw.setLevel_(lvl)
            print("[workwin] 文件框期间搜索窗层级 → %s"
                  % ("0（让位给文件框）" if picking else "NSStatusWindowLevel（抬回最前）"),
                  flush=True)
    except Exception as e:                              # noqa: BLE001
        print("[workwin] 设搜索窗层级失败：%s" % e, flush=True)
    _apply_rope_level()


def set_hit_rects(rects):
    """页面报上来的可点击区域（CSS 像素、窗口左上为原点的矩形列表）。

    传空列表 = 整窗都不吃点击（只显示不交互）。传 `None` 之外的任何东西都
    会打开命中测试；一直没报过就维持「全都吃」的保守默认。
    """
    out = []
    for r in (rects or []):
        try:
            x, y, w, h = (float(v) for v in list(r)[:4])
        except Exception:                               # noqa: BLE001
            continue
        if w > 0 and h > 0:
            out.append((x, y, w, h))
    prev = _HIT["rects"]
    _HIT["rects"] = out
    if prev is None:
        print("[workwin] 命中区已上报（%d 块），开始做逐像素点击穿透" % len(out),
              flush=True)


# ★★ 「用户算不算在活动」的探针（范老板 2026-10-18）
#
# 背景：工作模式原来是把后台整个冻住（打标/转写/对齐/纠偏全停）。现在改成
# **照跑**，唯一区别是「怎么判用户在用电脑」：
#   · 光标在「搜索区」（搜索框 + 结果面板那一整块）里动 → 算在用；
#   · 「暂存区」（绳窗挂明信片那一带）不参与判定；
#   · 其余地方（桌面、别的窗口）当然也不算。
# 为什么要按区域判：绳窗是**整屏**的透明覆盖层，光标划过它、哪怕停在那儿不动，
# 都不代表人在看这个 App。以前冻结是「一刀切」，现在换成「只看搜索区」——
# 用户在工作模式里搜素材时后台该补全，人一离开搜索区（去干别的）就立刻闲下来。
#
# 接线：app.py 启动时 `_work_init_activity()` 调 `set_activity_tap(回调)`，
# 回调只做一件事 —— 把 `_STATE["last_user_req"]` 刷成现在。
_ACT = {"tap": None, "search": None, "last": None}


def set_activity_tap(fn):
    """app 传进来的「刷新闲置计时」回调。传 None 可断开。"""
    _ACT["tap"] = fn


def set_search_rect(rect):
    """搜索区矩形（CSS 像素、搜索窗左上原点）。None = 还没有/不可用。"""
    r = None
    if rect:
        try:
            x, y, w, h = (float(v) for v in list(rect)[:4])
            if w > 0 and h > 0:
                r = (x, y, w, h)
        except Exception:                               # noqa: BLE001
            r = None
    _ACT["search"] = r


def _activity_probe(nw):
    """光标在搜索区里**动了** → 告诉 app「用户在用」。停着不动不算。

    ★ 「停着不动不算」是这段的关键：用户把鼠标随手搁在搜索框上就去倒水，
    那不算在用 —— 否则闲置计时被一秒刷一次，后台补全永远等不到空闲，
    又变回「怎么一直不跑」。所以要有位移（>= 2px 曼哈顿距离）才刷。
    ★ 用 `nw.frame()` 换算成窗口坐标（左上原点），跟页面报的 CSS 像素同一套；
    和 `_hit_tick` 里命中测试的算法保持一致，不另起一套坐标。
    """
    tap, sr = _ACT.get("tap"), _ACT.get("search")
    if not tap or not sr or nw is None:
        return
    try:
        import AppKit
        if not nw.isVisible():
            return
        p = AppKit.NSEvent.mouseLocation()              # 屏幕坐标，原点在左下
        f = nw.frame()
        x = p.x - f.origin.x
        y = f.origin.y + f.size.height - p.y            # 换成本窗左上原点
        x0, y0, w, h = sr
        if not (x0 <= x <= x0 + w and y0 <= y <= y0 + h):
            return
        last = _ACT.get("last")
        if last is not None and abs(x - last[0]) + abs(y - last[1]) < 2.0:
            return                                      # 停在原地：不算在用
        _ACT["last"] = (x, y)
        tap()
    except Exception:                                   # noqa: BLE001
        # 探针失败绝不能让 tick 抛出去（那会让点击穿透一起失灵）
        pass


def _hit_tick():
    """追鼠标：在可点击区里就吃点击，不在就整窗穿透。"""
    # ★ `_WIN` 是 pywebview 的 Window，不是 NSWindow —— 原生那层在 `.native`。
    #   直接拿它调 isVisible() 会 AttributeError，而异常被下面那层吞掉之后
    #   表现就是**命中测试一直没生效**（日志里每 90ms 刷一行，很容易漏看）。
    nw = getattr(_WIN, "native", None)
    # ★ 光标先记（见 _record_pointer 的注释）：搜索层收着的时候下面那行会早退，
    #   而「停 3 秒自动叫出搜索层」正好只发生在那一刻。
    _record_pointer(nw)
    # ★ 2026-10-18：工作模式下「用户算不算在活动」也在这里判（只看搜索区）。
    #   放在命中测试之前、且不受可见性早退影响之外的地方 —— 它自己会查可见性。
    _activity_probe(nw)
    # ★ 绳窗那一小块（「收工」按钮）走另一套矩形，且**不受搜索层可见性影响** ——
    #   范老板第⑤条要的恰恰是「搜索层收着的时候它也能点」。
    _rope_hit_tick()
    if nw is None or _HIT["rects"] is None:
        return
    try:
        import AppKit                               # 本模块一律延迟导入，别拖冷启动
        if not nw.isVisible():
            return
        p2 = AppKit.NSEvent.mouseLocation()         # 屏幕坐标，原点在左下
        f = nw.frame()
        x = p2.x - f.origin.x
        y = f.origin.y + f.size.height - p2.y       # 换成本窗的左上原点
        # 判定尺寸都比真实矩形**放宽 7px**：按钮边缘、图标描边这些地方用户
        # 觉得自己点在控件上了，实际差几个像素。放宽一点体感更准，也不至于
        # 误吞大片桌面。
        M = 7.0
        ignore = True
        for rx, ry, rw, rh in _HIT["rects"]:
            if rx - M <= x <= rx + rw + M and ry - M <= y <= ry + rh + M:
                ignore = False
                break
        if ignore != _HIT["ignoring"]:
            _HIT["ignoring"] = ignore
            nw.setIgnoresMouseEvents_(ignore)
    except Exception as e:                          # noqa: BLE001
        print("[workwin] 命中测试出错：%s" % e, flush=True)
_LOCK = threading.RLock()
# 主窗句柄，由 launcher 用 attach_main() 塞进来。本模块不 import launcher
# （launcher 反过来要 import 本模块），避免绕成环。
_MAIN = {"win": None}
# 进工作模式时主窗是不是我们收起来的。不是我们收的（用户自己收到了菜单栏），
# 退回休闲模式时就不该擅自弹回来 —— 用户会觉得「我没让它出来它自己蹦出来了」。
_HID_MAIN = {"by_us": False}

# ★★ 「工作模式是不是进入过」的**持久意愿**，跟搜索层可不可见无关（范老板
#   2026-10-15 第②条）。
#   踩过的坑：`is_open()` 问的是**搜索层**的 isVisible()。搜索层一收起（Esc、
#   点空白处、或者用户就是没唤醒），它就返回 False —— 于是 launcher 的
#   `_restore_panel()` 以为「不在工作模式」，一旦 App 因为别的原因被激活
#   （点收工按钮会 makeKeyAndOrderFront，正好会激活 App），就把休闲模式的主窗
#   又摆出来，两套 UI 同时出现。范老板报的「工作模式下前几次点收工会把主窗
#   唤醒」就是这个：第一次激活时 `_QUIT["hidden"]` 还是 True，主窗被弹出来，
#   弹出来之后那个旗子变 False，后面几次才不弹 —— 所以是「前几次」。
#   现在只要进过一次工作模式（没调 hide() 退出），这里就是 True，
#   主窗一律不许回来。
_WORK = {"on": False}


# --------------------------------------------------------------------------
# 屏幕
# --------------------------------------------------------------------------

def _visible_frame():
    """主屏去掉菜单栏和 Dock 之后的那块，返回 `(x, y, w, h)`（左上角原点）。

    ★ 用 `visibleFrame` 而不是 `frame`：铺满整屏的话窗口会盖住菜单栏，
      用户点不到右上角的时钟和 Wi-Fi，这在「只是想搜个图」的时候很恼人。
    """
    try:
        import AppKit
        f = AppKit.NSScreen.mainScreen().visibleFrame()
        # Cocoa 的原点在左下角，这里换算成上左原点，和网页坐标一致。
        full = AppKit.NSScreen.mainScreen().frame()
        x = int(f.origin.x)
        y = int(full.size.height - (f.origin.y + f.size.height))
        return x, y, int(f.size.width), int(f.size.height)
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 取屏幕尺寸失败（退回 1440×900）：%s" % e)
        return 0, 0, 1440, 900


# --------------------------------------------------------------------------
# 建窗 / 摆位
# --------------------------------------------------------------------------

def create(port, hidden=True):
    """登记覆盖窗。**必须在 `webview.start()` 之前调。**

    `port` 是本地服务的端口（`app.py` 的 run_server 听的那个），窗口直接指过去。
    建的时候 `hidden=True`：工作模式默认关着，等唤醒键或设置里切过去再出现。
    """
    global _WIN
    import webview
    x, y, w, h = _visible_frame()
    _WIN = webview.create_window(
        "FXseek 工作模式",
        url="http://127.0.0.1:%d/work" % port,
        x=x, y=y, width=w, height=h,
        resizable=False,
        hidden=hidden,
        frameless=True,          # 无标题栏、无红绿灯
        on_top=True,             # NSStatusWindowLevel：压住普通窗口
        transparent=True,        # 桌面透出来
        easy_drag=False,         # 别让 pywebview 给整页加拖拽手势
        text_select=True,        # 同主窗：默认 False 会注入 user-select:none
    )
    _WIN.events.loaded += _on_loaded
    print("[workwin] 搜索窗已登记（%d×%d @ %d,%d）port=%d → %s"
          % (w, h, x, y, port,
             _WIN.original_url if hasattr(_WIN, "original_url") else "?"),
          flush=True)

    # 绳窗：同样铺满、透明、无边框，但**不置顶**（进桌面层级）
    global _ROPE
    _ROPE_URL["url"] = "http://127.0.0.1:%d/rope" % port
    _ROPE = webview.create_window(
        "FXseek 暂存区",
        url=_ROPE_URL["url"],
        x=x, y=y, width=w, height=h,
        resizable=False,
        hidden=True,
        frameless=True,
        on_top=False,
        transparent=True,
        easy_drag=False,
        text_select=False,
        # ★★ 这条是「一唤醒就能打字」的**真正根因**，别删。
        #   pywebview 的窗口类是这样写的（platforms/cocoa.py:75）：
        #       class WindowHost(AppKit.NSWindow):
        #           def canBecomeKeyWindow(self):
        #               return self.focus
        #   而 `show()` 的实现是
        #       AppHelper.callAfter(lambda: window.makeKeyAndOrderFront_(window))
        #   —— 是**扔进主线程队列稍后执行**的。workwin 的 show() 先
        #   `_WIN.show()`、紧接着 `_ROPE_show()`，两个 callAfter 依序落队，
        #   于是**绳窗的 makeKeyAndOrderFront 最后跑**，键盘焦点被它抢走。
        #   绳窗铺满整屏、页面里又没有输入框，用户按唤醒键后敲的字全进了
        #   绳窗，搜索框一个字符都收不到 —— 表现就是「唤醒了但打不了字」，
        #   而且 App 明明是 frontmost、窗口明明都可见，查日志一点错都没有。
        #   ★ 试过 `focus=False`（让 canBecomeKeyWindow 恒为 False）——**不行**：
        #   那样绳窗永远不是 key window，macOS 会把落在它身上的**第一次点击
        #   直接吞掉**（NSView.acceptsFirstMouse 默认 NO），「收工」按钮就点不动了。
        #   所以绳窗的 focus 保持 True，改用 `_ROPE_show()` 里那条
        #   「只 orderFront、绝不 makeKey」来防它抢键盘。
    )
    _ROPE.events.loaded += _on_loaded
    print("[workwin] 绳窗已登记（%d×%d @ %d,%d）→ %s"
          % (w, h, x, y, _ROPE_URL["url"]), flush=True)
    return _WIN


def _on_loaded():
    """页面第一次加载完：把几何和层级按原生方式再钉一遍。"""
    _tune()


def _find_webview(view, depth=0):
    """在视图树里找那个 WKWebView。

    pywebview 的 `Window.native` 只给到 NSWindow；WKWebView 是它 contentView 的
    子视图，中间还隔着 pywebview 自己包的几层。所以只能递归找。
    （`BrowserView` 实例上也存着 `self.webview`，但那是 pywebview 的内部字段，
    版本一换就可能改名；按类名找稳一些。）

    ★★ 判类名**不能**只写 `"WKWebView" in className()`：pywebview 用的不是原生
    WKWebView，而是它自己的子类 `WebKitHost`（cocoa.py:417
    `class WebKitHost(WebKit.WKWebView)`），`className()` 返回的就是
    `"WebKitHost"` —— 一个 "WKWebView" 子串都不含。实测表现：视图树里明明白白
    躺着 WebKitHost/WKFlippedView，这函数却一路返回 None，于是打印
    「没找到 WKWebView，桌面可能透不出来」，然后把 `drawsBackground` 该关的
    没关 —— 整个覆盖窗变成一块不透明的灰板。子类名也一并认。
    """
    if view is None or depth > 6:
        return None
    try:
        cls = str(view.className() or "")
        if "WKWebView" in cls or "WebKitHost" in cls:
            return view
        for sub in (view.subviews() or []):
            found = _find_webview(sub, depth + 1)
            if found is not None:
                return found
    except Exception:                                       # noqa: BLE001
        pass
    return None


def _tune_rope(retry=0):
    """绳窗的原生微调：**桌面层级 + 穿透点击**。

    进工作模式时它要一直在，但绝不能挡住别的窗口、更不能吃掉鼠标点击。
    `ignoresMouseEvents` 是关键：不加的话一块铺满屏幕的窗会把整个桌面的
    点击全吞了，用户连别的程序都点不动。
    """
    def _do():
        try:
            import AppKit
            if _ROPE is None or getattr(_ROPE, "native", None) is None:
                return
            nw = _ROPE.native
            f = AppKit.NSScreen.mainScreen().visibleFrame()
            nw.setFrame_display_(f, True)
            nw.setOpaque_(False)
            nw.setHasShadow_(False)
            nw.setBackgroundColor_(AppKit.NSColor.clearColor())
            nw.setIgnoresMouseEvents_(True)      # ★ 穿透：只画，不吃点击
            nw.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                AppKit.NSWindowCollectionBehaviorStationary |
                AppKit.NSWindowCollectionBehaviorIgnoresCycle)
            _set_rope_level(_ROPE_FRONT["on"])   # 重放「想要的」层级，不是写死 False
            wv = _find_webview(nw.contentView())
            if wv is not None:
                try:
                    wv.setValue_forKey_(False, "drawsBackground")
                    wv.setOpaque_(False)
                    wv.setNeedsDisplay_(True)
                except Exception as e:                      # noqa: BLE001
                    print("[workwin] 绳窗透明设置失败：%s" % e, flush=True)
            elif retry < 10:
                _call_later(0.25, lambda: _tune_rope(retry + 1))
        except Exception as e:                              # noqa: BLE001
            print("[workwin] 绳窗摆位失败：%s: %s" % (type(e).__name__, e))
    _call_main(_do)


def _rope_level_value():
    """按当前意愿算出绳窗该在的层级。

    三档（范老板 2026-10-15 第①条）：
      · 搜索层开着 → 最前（NSStatusWindowLevel）：拖卡片的时候绳子必须看得见。
      · 搜索层收着 + 编辑态 → 桌面图标层**上方**：明信片要能悬停/拖动/删除。
      · 搜索层收着 + 待编辑态 → 桌面层（图标**下面**）：鼠标完全碰不到，
        只有狒狒在走、明信片在摆。

    `kCGDesktopWindowLevel` / `kCGDesktopIconWindowLevel` AppKit 都没给具名
    常量，只有 Quartz 的 Key；本机实测值分别是 -2147483623 / -2147483603，
    都 +1 是「贴着那一层、但比它高 1」的位置。取不到就用实测值兜底 —— 宁可
    偶尔挡一下，也不能让绳窗整个看不见。
    """
    try:
        import AppKit
    except Exception:                                       # noqa: BLE001
        return 0
    # ★ 弹原生文件框期间：整窗压到普通层，否则文件框（L8）被我们盖住点不着。
    #   范老板 2026-10-15 第②条。
    if _HIT.get("picking"):
        return 0
    if _ROPE_FRONT["on"]:
        return AppKit.NSStatusWindowLevel
    # ★ 本机实测（`CGWindowListCopyWindowInfo` 读出来的真值）：
    #     壁纸 -2147483623 / Dynamic Wallpaper -2147483623
    #     桌面图标（访达）-2147483603
    #     Window Server 的 "underbelly" -2147483602
    #   所以「图标下面」= -2147483622，「图标上面」得**比 underbelly 还高**：
    #   实测停在 -2147483602 时窗口虽然 setIgnoresMouseEvents_(False) 了，
    #   鼠标事件仍旧被同层的 underbelly 吃掉（收工按钮都点不动）。
    #   取 -2147483601 就压过它，同时仍然贴着桌面图标层。
    if _ROPE_EDIT["on"]:
        return -2147483601
    # ★★ 待编辑态下鼠标停在「收工」按钮上时，必须**临时把整窗抬一层**。
    #   原因已实测：-2147483622 这一层（紧贴桌面图标之下）窗口即使
    #   `setIgnoresMouseEvents_(False)`，鼠标事件照样不进来 —— 页面的
    #   `mouseenter` 不触发、按钮不滑出、点击也到不了 `#collect`
    #   （证据：`/v1/work/hitrects` 的上报次数一动不动）。
    #   抬到 -2147483601（压在 Window Server 的 underbelly 之上）就通了。
    #   代价只是「鼠标停在左下角那枚按钮上时，绳子会短暂浮到桌面图标之上」——
    #   这会儿用户的注意力就在按钮上，看不出来。
    if _ROPE_HIT.get("keyed"):
        return -2147483601
    return -2147483622


def _apply_rope_level():
    if _ROPE is None or getattr(_ROPE, "native", None) is None:
        return
    try:
        lvl = _rope_level_value()
        _ROPE.native.setLevel_(lvl)
        print("[workwin] 绳窗层级 → %d（搜索层=%s，编辑态=%s）"
              % (lvl, "开" if _ROPE_FRONT["on"] else "关",
                 "开" if _ROPE_EDIT["on"] else "关"), flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 设绳窗层级失败：%s" % e, flush=True)


def set_rope_edit(on):
    """绳窗切「编辑态 / 待编辑态」。由 rope.html 按 3 秒停留判定后调进来。

    ★ 只记意愿，真正的 setLevel_ 交给主线程 —— 这个函数是被 app.py 的 HTTP
      线程调到的，AppKit 的窗口操作必须在主线程。`_call_main` 就是干这个的。
    """
    on = bool(on)
    if bool(_ROPE_EDIT["on"]) == on:
        return {"ok": True, "edit": on, "changed": False}
    _ROPE_EDIT["on"] = on
    _call_main(_apply_rope_level)
    return {"ok": True, "edit": on, "changed": True}


def _set_rope_level(front):
    """搜索层开/收时调：只看 `front`，编辑态那一路由 set_rope_edit 管。"""
    _ROPE_FRONT["on"] = bool(front)
    _call_main(_apply_rope_level)


def _tune(retry=0):
    """主线程上做的原生微调。

    为什么建窗时给过 x/y/w/h 还要再来一遍：pywebview 的 `initial_x/initial_y`
    走它自己的坐标系换算（见 cocoa.py 的 `move()`），多屏 / 缩放组合下不一定和
    `visibleFrame` 对齐；而覆盖窗对位置**很**敏感 —— 差一条菜单栏的高度，
    页面里「绳子挂在菜单栏下面多少」那套计算就全偏。直接用 NSWindow 的
    `setFrame_display_` 最稳。

    `retry` 是内部用的重试计数：WKWebView 装得比窗口晚，见下面那段。
    """
    def _do():
        try:
            import AppKit
            if _WIN is None or getattr(_WIN, "native", None) is None:
                return
            nw = _WIN.native
            f = AppKit.NSScreen.mainScreen().visibleFrame()
            nw.setFrame_display_(f, True)
            nw.setLevel_(AppKit.NSStatusWindowLevel)
            nw.setOpaque_(False)
            nw.setHasShadow_(False)
            nw.setBackgroundColor_(AppKit.NSColor.clearColor())
            # 不进 Mission Control / 不被「显示桌面」带走：它是个浮层，
            # 归到「辅助」那一类最贴近实际用法。
            nw.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                AppKit.NSWindowCollectionBehaviorStationary |
                AppKit.NSWindowCollectionBehaviorIgnoresCycle)

            # ★★ 让网页本身的底色也透出去，**这一步才是「桌面看得见」的真正开关**。
            #   只把 NSWindow 弄透明是不够的：WKWebView 自己会刷一层不透明的
            #   底色，于是整块窗变成一块均匀的灰板（实机截图确认过 —— 桌面完全
            #   看不见，只剩四个圆角露一点）。
            #
            #   pywebview 走的是 `setValue:forKey:@"drawsTransparentBackground"`，
            #   而这个键现在**已经废弃**了：日志里明确报
            #       -[WKWebView _setDrawsTransparentBackground:] is deprecated
            #   也就是它实际上不生效。现行写法是把 `drawsBackground` 设成 False
            #   （KVC 私有键，但 Apple 自己一直在用，稳定）。
            wv = _find_webview(nw.contentView())
            if wv is not None:
                try:
                    wv.setValue_forKey_(False, "drawsBackground")
                    # 读回来确认。KVC 私有键「设了不报错但不生效」是常态，
                    # 光看有没有抛异常等于没验 —— 实测就吃过一次：日志干干净净，
                    # 屏幕上一整块 #D0D0D0 不透明灰板。
                    try:
                        back = wv.valueForKey_("drawsBackground")
                    except Exception:                       # noqa: BLE001
                        back = "读不到"
                    print("[workwin] 覆盖窗 webview=%s drawsBackground 设完读回=%r"
                          % (wv.className(), back), flush=True)
                except Exception as e:                      # noqa: BLE001
                    print("[workwin] 设 drawsBackground 失败：%s" % e, flush=True)
                # ★★★ 别再加 `underPageBackgroundColor`！
                #   网上「让 WKWebView 透明」的帖子里常见这么一条：
                #       wv.setValue(NSColor.clearColor(), forKey:"underPageBackgroundColor")
                #   在这儿加上它，覆盖窗立刻变成**整整一块不透明浅灰**
                #   （实测单窗抓屏 RGBA=(208,208,208,255)，alpha 满的）。
                #   而 `drawsBackground=False` 读回来明明是 False —— 于是看起来
                #   像「drawsBackground 失效了」，其实是被这个键覆盖掉了。
                #   排查方式：用 ovl_probe3/4 那种「逐步加一条、每步抓一次屏」的
                #   探针逐层剥，别在一个函数里一次改五处再猜。
                try:
                    wv.setOpaque_(False)
                    wv.setNeedsDisplay_(True)
                except Exception as e:                      # noqa: BLE001
                    print("[workwin] 重绘 webview 失败：%s" % e, flush=True)
            elif retry < 10:
                # ★ 建窗的头几百毫秒里 contentView 底下**还没有** WKWebView：
                #   pywebview 是先摆窗口、后装 web view。只试一次的话，「启动时就
                #   停在工作模式」这条路径上桌面永远透不出来 —— 实测过，启动即
                #   工作模式时打的正是这行「没找到 WKWebView」（后来手动按唤醒键
                #   开关一次就好了，因为那时候 web view 已经在了）。这种「第一次
                #   不行、第二次就行」的 bug 最容易被当成偶发。等一会儿重试。
                _call_later(0.25, lambda: _tune(retry + 1))
            else:
                print("[workwin] 没找到 WKWebView，桌面可能透不出来", flush=True)
                # 把视图树打出来：光说「没找到」查不出是「还没建」还是「建在别处」。
                def _dump(v, d=0):
                    if v is None or d > 6:
                        return
                    try:
                        print("[workwin]   %s%s" % ("  " * d, v.className()), flush=True)
                        for s in (v.subviews() or []):
                            _dump(s, d + 1)
                    except Exception:                       # noqa: BLE001
                        pass
                _dump(nw.contentView())
            _start_hit_poll()
        except Exception as e:                              # noqa: BLE001
            print("[workwin] 摆位失败：%s: %s" % (type(e).__name__, e))
    _call_main(_do)
    _tune_rope()


def _outside_click():
    """范老板第②条（2026-10-15）：「搜索框隐藏只有两种方式：按 esc 键或单击
    『非搜索区』范围」。

    Esc 页面自己处理；「点别处」得在这儿拦 —— 搜索窗整窗都是透明的，鼠标不在
    可点区里的时候窗口整体穿透（事件给了别的 app），页面**根本收不到那次点击**，
    指望页面里的 `document` 点击监听是不行的。所以挂一个全局鼠标按下监听：
    按下时若光标不在上报过的可点区里，就把搜索层收起。

    本地监听也要挂：点「收工」按钮时事件是**送到我们窗口**的（绳窗那一小块不
    穿透），全局监听只收别的 app 的事件，只挂全局会漏掉这一半。
    """
    nw = getattr(_WIN, "native", None)
    if nw is None or _HIT["rects"] is None or not nw.isVisible():
        return
    # ★ 范老板新③：「以图搜 / 以音搜有问题」的真凶之一就在这里。
    #   「以图搜图」会弹一个原生文件选择框（NSPanel，属于本 app）。用户在那个
    #   框里点「打开」时，这一发 mouseDown 的坐标当然**不在搜索区的矩形里** ——
    #   于是搜索层当场被判定为「点了外面」收起来，用户选完文件回来什么都没有，
    #   只看到搜索框凭空消失。表现就是「以图搜有毛病」。
    #   运行中只要还有模态窗（模态文件框、确认框）挂着，就一律不当成「点外面」。
    #   ⚠️ 但 `NSApp.modalWindow()` 对 NSOpenPanel 是**不管用**的（它走 sheet /
    #   异步模态，不是 runModalForWindow）——实测点「打开」照样把搜索层收走。
    #   所以真正的闸门是下面这一行：由页面在弹框前明说「我正在选文件」。
    if _HIT.get("picking"):
        return
    try:
        import AppKit
        if AppKit.NSApp.modalWindow() is not None:
            return
    except Exception:
        pass
    try:
        import AppKit
        p2 = AppKit.NSEvent.mouseLocation()
        f = nw.frame()
        x = p2.x - f.origin.x
        y = f.origin.y + f.size.height - p2.y
        M = 7.0
        for rx, ry, rw, rh in _HIT["rects"]:
            if rx - M <= x <= rx + rw + M and ry - M <= y <= ry + rh + M:
                return
        dismiss("点了搜索区外面")
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 点外收起判定出错：%s" % e, flush=True)


def _start_hit_poll():
    """追鼠标改 ignoresMouseEvents：**事件驱动**，不靠定时器轮询。

    ★ 为什么从定时器改成监听器：定时器最快也就 ~11Hz（90ms 一轮），而用户
      「把鼠标甩到搜索框上、立刻点下去」整个动作可以短于 90ms —— 那一下点击
      到达时窗口还处在「穿透」状态，于是**点到了底下的 FXseek 主窗口**，设置页
      就弹出来了。这不是偶发，是每一步都踩中的竞态。

      `addGlobalMonitorForEventsMatchingMask` 收的是**别的 app** 拿到的鼠标事件
      （我们的窗口在穿透时就是这种情况），`addLocalMonitor…` 收的是送到我们这
      儿的；两个都挂上，鼠标一动就立刻重算，竞态窗口从 90ms 降到「零」（事件
      本身就在同一轮 runloop 里）。

      （鼠标事件的全局监听不需要辅助功能权限，键盘才需要。）
    """
    if _HIT["poll"] is not None:
        return
    try:
        import AppKit
        mask = (AppKit.NSEventMaskMouseMoved |
                AppKit.NSEventMaskLeftMouseDragged |
                AppKit.NSEventMaskRightMouseDragged)
        # 全局：我们的窗口正在穿透，事件给了别的 app —— 也要跟着更新
        _HIT["gmon"] = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, lambda ev: _hit_tick())
        # 本地：事件送到我们这儿（没穿透时）
        _HIT["lmon"] = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, lambda ev: (_hit_tick(), ev)[1])
        # 兜底：极慢的轮询。监听器在某些情况下会漏（比如鼠标没动、窗口刚显示），
        # 每 0.5s 补一次，代价可以忽略。
        _HIT["poll"] = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.5, True, lambda t: _hit_tick())
        # 点「非搜索区」自动收起（范老板第②条）
        dmask = AppKit.NSEventMaskLeftMouseDown
        _HIT["dmon_g"] = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            dmask, lambda ev: _outside_click())
        _HIT["dmon_l"] = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            dmask, lambda ev: (_outside_click(), ev)[1])
        print("[workwin] 命中测试：已挂全局+本地鼠标监听 + 0.5s 兜底", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 挂命中测试监听失败：%s" % e, flush=True)


def _call_later(delay, fn):
    """`delay` 秒后在主线程上跑 `fn`（走 Cocoa 的定时器，不占线程）。"""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callLater(delay, fn)
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 排延时调用失败：%s" % e)


def _call_main(fn):
    """把一段 pyobjc 调用送到主线程跑（app 还没起来就直接跑）。"""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn)
    except Exception:                                       # noqa: BLE001
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            print("[workwin] 主线程调用失败：%s" % e)


def _on_main_thread():
    try:
        import threading
        return threading.current_thread() is threading.main_thread()
    except Exception:                                       # noqa: BLE001
        return False


def _call_main_sync(fn, timeout=3.0):
    """在主线程上跑 `fn` 并**等它跑完**，返回它的返回值（拿不到就返回 None）。

    `_call_main` 是 `callAfter`，发完就走；但「页面加载出来没有」这种问句必须
    当场拿到答案，否则只能靠 sleep 猜。已经在主线程上就直接跑 —— 往自己身上
    `callAfter` 再等，就是自己等自己，直接死锁（这正是 `get_current_url()` 干的事）。
    """
    if _on_main_thread():
        try:
            return fn()
        except Exception:                                   # noqa: BLE001
            return None
    box = {}
    done = threading.Event()

    def _wrap():
        try:
            box["v"] = fn()
        except Exception as e:                              # noqa: BLE001
            box["e"] = e
        finally:
            done.set()

    _call_main(_wrap)
    if not done.wait(timeout):
        return None
    return box.get("v")


# --------------------------------------------------------------------------
# 显示 / 隐藏
# --------------------------------------------------------------------------

def loaded_ok(which="work"):
    """页面到底加载出来没有（contentView 变成 WebKitHost 才算）。

    只看 `events.loaded` 不够：那个事件是 pywebview 在导航完成时 set 的，而
    「导航压根没发出去」时它同样不响 —— 「发出了没回来」和「根本没发」的现场
    长得一模一样，光看事件分不出来。
    """
    def _do():
        try:
            w = _ROPE if which == "rope" else _WIN
            nw = getattr(w, "native", None)
            if nw is not None:
                return _find_webview(nw.contentView()) is not None
        except Exception:                                   # noqa: BLE001
            pass
        return False

    return bool(_call_main_sync(_do))


def ensure_loaded(url=""):
    """把覆盖窗的页面**再**加载一次；已经加载出来就什么都不做。

    ★★ 为什么非要有这一步：pywebview 的 `start()` 里，除主窗以外的窗口是由一条
    后台线程「等主窗 `shown` 之后」才建的（webview/__init__.py:290
    `_create_children`），而 `BrowserView.__init__` 末尾那次 `load_url` 走的是
    `AppHelper.callAfter`。实测这一次**会静默丢掉**：不报错、不发 HTTP 请求
    （服务端日志里干干净净），`contentView` 永远停在空 `NSView`（正常加载完
    会变成 `WebKitHost`），页面就是不出来 —— 表现成「唤醒键按了没反应」。
    等本地服务确实起来了再补一次 `load_url` 就正常。主窗没这毛病，所以只有
    覆盖窗需要补。
    """
    out = {"ok": True, "reloaded": []}
    if _WIN is None:
        return {"ok": False, "error": "窗口层不在（不是桌面版）"}
    # ★ 两扇窗都要补。pywebview 的 `_create_children` 只对**第一个**子窗可靠
    #   （见本函数上面那段），第二个子窗丢加载的概率一样高 —— 绳窗要是没加载
    #   出来，表现是「搜索框出来了但绳子不见了」，很容易被当成样式问题查半天。
    for which, win in (("work", _WIN), ("rope", _ROPE)):
        if win is None or loaded_ok(which):
            continue

        def _do(w=win, tag=which):
            try:
                # ★ 传进来的 `url` 只对**搜索窗**有意义（launcher 传的是
                #   `…/work`）。绳窗必须用自己的 `original_url`（`…/rope`），
                #   否则两扇窗都去加载搜索页 —— 表现是「绳子不见了，屏幕上
                #   多出一个搜索框」，而且多出来的那个还会抢焦点。
                target = (url if tag == "work" else "") or getattr(w, "original_url", "") or ""
                if not target:
                    return
                from webview.platforms import cocoa as _cocoa
                print("[workwin] %s 窗初次加载丢了，补发 load_url：%s" % (tag, target),
                      flush=True)
                _cocoa.load_url(target, w.uid)
            except Exception as e:                          # noqa: BLE001
                print("[workwin] %s 窗补加载失败：%s" % (tag, e), flush=True)

        _call_main(_do)
        out["reloaded"].append(which)
    return out


def wake(reason="唤醒键"):
    """唤醒键：**只负责把搜索层叫出来**，已经出来了就什么都不做。

    ★ 不做切换（按一下出来、再按一下收）—— 范老板明确要求「快捷键只负责
      唤醒搜索框，搜索框已存在的情况下按快捷键无效」。切换会让这个键有两种
      含义，用户按下去之前不知道会得到哪个；而「收起来」这件事已经有
      Esc、点空白、1 分钟自动收三条路了，不缺这一个。
    """
    with _LOCK:
        if _WIN is None:
            return {"ok": False, "error": "窗口层不在（不是桌面版）"}
        if _is_shown():
            return {"ok": True, "shown": True, "already": True, "reason": reason}
    # 页面没加载出来的话，这一下按下去就是一片空白（见 ensure_loaded 的注释：
    # 覆盖窗那次初次 load_url 会静默丢掉）。唤醒是低频动作，顺手补一发。
    try:
        ensure_loaded()
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 唤醒时补加载失败：%s" % e, flush=True)
    return show(reason)


def toggle():
    """菜单栏「切换工作模式」：进/出整个工作模式（两层窗一起）。"""
    with _LOCK:
        if _WIN is None:
            return {"ok": False, "error": "窗口层不在（不是桌面版）"}
        if _is_shown():
            return hide("菜单栏收起")
        return show("菜单栏唤出")


def _poke_focus(retry=0, soft=False):
    """让搜索窗页面自己把输入框聚焦（范老板第①条）。

    ★ 为什么不能只靠页面里的 `window focus` 监听：这扇窗是 pywebview 的**原生
      覆盖窗**，被 makeKeyAndOrderFront_ 叫出来的时候页面根本收不到 DOM focus
      事件 —— workwin 自己上面那段注释就写着「页面里监听 focus 自动聚焦输入框
      的代码也收不到事件（覆盖窗是原生窗口，show() 之前页面根本不知道）」。
      实测 1.1.2 装上去之后范老板仍然说「一唤醒就是聚焦状态，你还是没修改」，
      正是这个原因。
    ★ 为什么用 `evaluateJavaScript_completionHandler_` 而不是 pywebview 的
      `evaluate_js()`：后者是 `callAfter(...) + semaphore.acquire()`，而 show()
      本来就跑在主线程上（唤醒键回调 / _on_main）—— 主线程等一个只有主线程才
      能跑的活儿，直接死锁（下面那段长注释记的就是这个坑）。前者是异步派发，
      发完立刻返回，一个字都不等。
    """
    # ★ 找 WKWebView 有**两条**路，别只走一条：
    #   `_WIN.gui.webview` 是 pywebview 自己记的句柄，某些时序下是 None；
    #   `_find_webview(native.contentView())` 是顺着视图树找，`_tune()` 一直
    #   在用、日志里实证能拿到。只走前者的话拿不到就**静默 return**，日志干干
    #   净净、范老板那边表现就是「按了唤醒键还是不能打字」—— 1.1.5 之前查了
    #   半天就是这个原因。
    wv = None
    try:
        nw0 = getattr(_WIN, "native", None)
        if nw0 is not None:
            wv = _find_webview(nw0.contentView())
    except Exception as e:                              # noqa: BLE001
        print("[workwin] 唤醒聚焦：视图树找 webview 失败：%s" % e, flush=True)
    if wv is None:
        try:
            wv = getattr(getattr(_WIN, "gui", None), "webview", None)
        except Exception:                               # noqa: BLE001
            wv = None
    if wv is None:
        # 建窗头几百毫秒里视图树还没装好（和 _tune 里那段同一个原因），重试。
        if retry < 6:
            _call_later(0.12 + retry * 0.12, lambda: _poke_focus(retry + 1, soft))
        else:
            print("[workwin] 唤醒聚焦：怎么都找不到 WKWebView，键盘焦点没抢到",
                  flush=True)
        return
    # ★ 先 makeFirstResponder：App 激活 + 窗口 key 都做到了，键盘事件仍然可能
    #   落在 webview 之外的那个视图上（覆盖窗里挂过别的子视图）。不设的话
    #   JS 里的 input.focus() 只是 DOM 层面的光标，敲键盘还是进不去。
    try:
        nw1 = getattr(_WIN, "native", None)
        if nw1 is not None:
            nw1.makeFirstResponder_(wv)
    except Exception as e:                              # noqa: BLE001
        print("[workwin] 唤醒聚焦：makeFirstResponder 失败：%s" % e, flush=True)
    try:
        # soft：只把光标放回输入框，不重置界面（见 work.html 里那段注释）
        arg = '"soft"' if soft else ""
        wv.evaluateJavaScript_completionHandler_(
            "window.__fxseekFocus && window.__fxseekFocus(%s);" % arg, None)
        print("[workwin] 唤醒聚焦：指令已发（soft=%s，retry=%d）"
              % (soft, retry), flush=True)
    except Exception as e:                              # noqa: BLE001
        print("[workwin] 唤醒聚焦指令没发出去：%s" % e, flush=True)


def show(reason="唤醒键"):
    """叫出搜索层 + 绳层，并把主窗收起来（两种 UI 不能同时出现）。"""
    global _HID_MAIN
    with _LOCK:
        if _WIN is None:
            return {"ok": False, "error": "窗口层不在（不是桌面版）"}
        # ★ 一进工作模式就把「意愿」钉死：之后无论搜索层收没收起，
        #   休闲模式的主窗都不许再自己冒出来（见 _WORK 的注释）。
        _WORK["on"] = True
        _WIN.show()
        _ROPE_show()
        _tune()
        # 唤醒键的意义就是「按一下就能打字」。窗口光「浮出来」不够：这个 App
        # 可能根本不是当前活跃 App，键盘焦点还在用户原来那个程序里，敲的字会
        # 全跑到人家那儿去；而且页面里监听 focus 自动聚焦输入框的代码也收不到
        # 事件（覆盖窗是原生窗口，show() 之前页面根本不知道）。
        # show() 只会在主线程被调用（唤醒键回调 / _on_main），所以这里直接调。
        try:
            import AppKit
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            nw2 = getattr(_WIN, "native", None)
            if nw2 is not None:
                nw2.makeKeyAndOrderFront_(None)
        except Exception as e:                              # noqa: BLE001
            print("[workwin] 抢键盘焦点失败：%s" % e, flush=True)
        # ★ 绳窗虽然已经 focus=False 抢不走焦点了，但 show() 是排队的、
        #   保险起见再补一拍，等两条 callAfter 都跑完，把搜索窗重新钉成 key。
        def _rekey():
            try:
                nw3 = getattr(_WIN, "native", None)
                if nw3 is not None and not nw3.isKeyWindow():
                    nw3.makeKeyAndOrderFront_(None)
            except Exception:                               # noqa: BLE001
                pass
        _call_later(0.05, _rekey)
        # ★ 光抢到 App 级键盘焦点不够 —— 页面要自己把光标放进输入框（见 _poke_focus）。
        _poke_focus()
        # ★ 补两拍：WKWebView 成为 key window 常常比 JS 慢一拍，第一发打空是
        #   常态。这两发走 soft（不重置界面），免得把用户已经敲进去的字清掉。
        _call_later(0.30, lambda: _poke_focus(99, True))
        _call_later(0.85, lambda: _poke_focus(99, True))
        # ★★ 这里**绝对不能**调 `_WIN.get_current_url()`！
        #   pywebview 的实现（cocoa.py:869）是
        #       AppHelper.callAfter(get) → semaphore.acquire()
        #   也就是「把取 URL 的活儿丢回主线程，然后**阻塞等结果**」。而 show()
        #   本来就是跑在主线程上的（唤醒键回调 / _on_main）—— 主线程在等一个
        #   只有主线程才能跑的活儿，直接死锁，整个 App 卡住（实测过：日志停在
        #   这一行，之后连一个 HTTP 请求都没有了，看着像「覆盖窗没加载页面」，
        #   其实整个进程都僵住了）。
        #   要看 URL 就看 `_WIN.original_url`（纯 Python 属性，不跨线程）。
        print("[workwin] show(%s) 已发出，visible=%s，url=%s"
              % (reason, _is_shown(),
                 getattr(_WIN, "original_url", "?")), flush=True)
        main = _MAIN.get("win")
        if main is not None:
            try:
                # 只在主窗**确实看得见**的时候才收，并记「是我们收的」。
                # 用户可能早就把主窗收到菜单栏了，那就什么都不用做 ——
                # 更关键的是不要把它标成「我们收的」，否则退回休闲模式时
                # 会擅自把它弹出来（用户会觉得莫名其妙）。
                nw = getattr(main, "native", None)
                vis = bool(nw.isVisible()) if nw is not None else False
                print("[workwin] 进工作模式：主窗可见=%s" % vis, flush=True)
                if vis:
                    main.hide()
                    _HID_MAIN["by_us"] = True
                # ★ `main.hide()` 走的是 pywebview 的 `AppHelper.callAfter`，
                #   是**异步**的：排进主队列就返回了。而紧接着上面的
                #   `activateIgnoringOtherApps_` 会发激活通知，launcher 那边的
                #   唤回逻辑（`_restore_panel`）若在这个缝里跑，就会把主窗又
                #   摆出来 —— 表现是「主窗去而复返」。这里补一发主线程上的
                #   延迟复核：只要工作模式还开着、主窗又可见了，就再收一次。
                def _recheck():
                    try:
                        import AppKit
                        if not _is_shown():
                            return
                        n2 = getattr(main, "native", None)
                        if n2 is not None and n2.isVisible():
                            print("[workwin] 主窗又冒出来了，再收一次", flush=True)
                            main.hide()
                            _HID_MAIN["by_us"] = True
                    except Exception as e:                  # noqa: BLE001
                        print("[workwin] 复核主窗失败：%s" % e, flush=True)
                try:
                    import AppKit
                    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                        0.35, False, lambda t: _recheck())
                except Exception:                           # noqa: BLE001
                    pass
            except Exception as e:                          # noqa: BLE001
                print("[workwin] 收主窗失败：%s" % e)
        return {"ok": True, "shown": True, "reason": reason}


def _ROPE_show():
    """把绳窗显示出来（进工作模式时调一次，之后它一直在）。"""
    try:
        if _ROPE is None:
            print("[workwin] 绳窗没建（_ROPE is None）", flush=True)
            return
        # ★★ 这里**不能**用 `_ROPE.show()`：pywebview 的 show() 是
        #       AppHelper.callAfter(lambda: window.makeKeyAndOrderFront_(window))
        #   —— 它是「排进主线程队列稍后执行」的，而 workwin.show() 里
        #   `_WIN.show()` 在前、`_ROPE_show()` 在后，两个 callAfter 依序落队，
        #   于是**绳窗最后跑 makeKeyAndOrderFront**，把键盘焦点从搜索窗抢走。
        #   绳窗铺满整屏、页面里没有输入框，用户按唤醒键后敲的字全进了绳窗 ——
        #   表现就是「唤醒了但打不了字」，而 App 明明是 frontmost、窗口都可见、
        #   代码一行错都查不出来。改用 `orderFrontRegardless()`：**只把窗排到
        #   最前，绝不改 key window**，搜索窗的键盘焦点就保住了。
        def _order_front_only():
            try:
                import AppKit
                # ★ 激活 App 这一步**不能省**：非活跃 App 的窗口收不到鼠标
                #   点击（第一下只会去激活 App），收工按钮和明信片拖动就全废了。
                #   pywebview 的 show() 里正是这一句 + makeKeyAndOrderFront，
                #   我们只把后半句换成不改 key window 的 orderFrontRegardless。
                AppKit.NSApp.activateIgnoringOtherApps_(True)
                nx = getattr(_ROPE, "native", None)
                if nx is not None:
                    nx.orderFrontRegardless()
            except Exception as e:                          # noqa: BLE001
                print("[workwin] 绳窗 orderFront 失败：%s" % e, flush=True)
        _call_main(_order_front_only)
        _tune_rope()
        _call_main(lambda: _set_rope_level(True))
        print("[workwin] 绳窗 show 已发出，native=%s visible=%s"
              % (getattr(_ROPE, "native", None) is not None,
                 _rope_visible()), flush=True)
    except Exception as e:                                  # noqa: BLE001
        print("[workwin] 显示绳窗失败：%s: %s" % (type(e).__name__, e), flush=True)


def _rope_visible():
    """绳窗原生可见性。show() 是异步的（callAfter），马上读多半还是 False，
    所以只当参考值打日志用，不拿来判断。"""
    try:
        nw = getattr(_ROPE, "native", None)
        return bool(nw.isVisible()) if nw is not None else None
    except Exception as e:                                  # noqa: BLE001
        return "err:%s" % e


def dismiss(reason="收起"):
    """只收**搜索层**。绳窗照旧挂着东西 —— 那是暂存区，不该被一次 Esc 清空。

    唤醒键的「唤醒」就是相对它而言的：收起来之后按唤醒键，出来的还是原来
    那批明信片，用户接着收。
    """
    with _LOCK:
        if _WIN is None:
            return {"ok": False, "error": "窗口层不在（不是桌面版）"}
        try:
            _WIN.hide()
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        # 搜索层收了，绳窗沉回桌面层级：别再压着别的窗口。
        _call_main(lambda: _set_rope_level(False))
        print("[workwin] 搜索层已收起（%s），绳窗仍在" % reason, flush=True)
        return {"ok": True, "shown": False, "reason": reason}


def hide(reason="退出工作模式"):
    """退出整个工作模式：两层窗都收，主窗还回来。"""
    global _HID_MAIN
    with _LOCK:
        if _WIN is None:
            return {"ok": False, "error": "窗口层不在（不是桌面版）"}
        # ★ 退出工作模式：意愿也跟着清掉，主窗才允许被唤回休闲模式。
        _WORK["on"] = False
        try:
            _WIN.hide()
            if _ROPE is not None:
                _ROPE.hide()
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        # ★ 只有「确实是我们在进工作模式时收起来的」才还回去。
        #   用户自己按了「收进菜单栏」的话，这时候弹出来是越权。
        if _HID_MAIN["by_us"]:
            _HID_MAIN["by_us"] = False
            main = _MAIN.get("win")
            if main is not None:
                try:
                    main.show()
                except Exception as e:                      # noqa: BLE001
                    print("[workwin] 还回主窗失败：%s" % e)
        return {"ok": True, "shown": False, "reason": reason}


def attach_main(win):
    """告诉覆盖窗「主窗是哪个」，进出工作模式时好互相让位。"""
    _MAIN["win"] = win


def main_is_hidden_by_us():
    return bool(_HID_MAIN["by_us"])


def _is_shown():
    """覆盖窗现在是不是可见的。

    `_WIN.shown` 只是「**曾经**显示过没有」（pywebview 的 first_show 里 set 的），
    不能拿它当可见性用 —— 用它判断会导致第二下唤醒键以为「已经开着」而直接收起，
    实际根本没开。只能问原生 NSWindow 的 `isVisible`。
    """
    try:
        nw = getattr(_WIN, "native", None)
        if nw is None:
            return False
        return bool(nw.isVisible())
    except Exception:                                       # noqa: BLE001
        return False


def window():
    return _WIN


def shown():
    return _is_shown()


# --------------------------------------------------------------------------
# 唤醒键
# --------------------------------------------------------------------------

_HK = {"combo": "", "why": ""}


def bind_hotkey(combo, on_fire):
    """注册唤醒键。返回 `(ok, 说明)`；`combo` 为空表示解绑。"""
    import hotkey
    with _LOCK:
        if not (combo or "").strip():
            hotkey.unregister()
            _HK["combo"], _HK["why"] = "", ""
            return True, ""
        ok, why = hotkey.register(combo, on_fire)
        _HK["combo"] = combo if ok else ""
        _HK["why"] = "" if ok else why
        return ok, why


def hotkey_state():
    """`(当前组合键, 上次失败原因)`，给设置页展示。"""
    return _HK["combo"], _HK["why"]


def hotkey_available():
    import hotkey
    return hotkey.available()


def is_open():
    """工作模式的搜索窗在屏上就算「开着」。"""
    return _is_shown()


def engaged():
    """工作模式**进入过且没退出**（不看搜索层可不可见）。

    ★ 判断「闲不闲、要不要让主窗回来」必须用这个，不能用 `is_open()`：
      搜索层收起来（Esc / 点空白处 / 干脆没唤醒）的时候 `is_open()` 是 False，
      主窗就会趁 App 被激活的时机自己冒出来，两套 UI 叠在一起。
    """
    return bool(_WORK["on"])
