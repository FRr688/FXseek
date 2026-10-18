#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""菜单栏图标助手 —— 独立小进程，只干一件事：在菜单栏摆一个「✦ FXseek」小牌子。

为什么非得单开一个进程？因为 macOS 对**由 LaunchServices 直接启动的进程**
（也就是用户双击 .app 启动的那种）不给排菜单栏图标：NSStatusItem 建出来窗口
高度永远是 0，看不见，也不报错。这一点用一个最小的纯 AppKit 程序验证过 ——
同一个程序，双击启动就排不上，从终端直接 exec 启动就正常。而**由别的进程
exec 出来的子进程**两种情况下都能排上（也实测过）。

所以：图标交给这个子进程，窗口的关/开留给主程序，两边用本机 HTTP 说一句话
（服务端在 app.py 里，路由见下面的 _ROUTES）。

菜单长这样（照 oMLX 那份排的）：

    服务器：运行中（端口 8231）        ← 绿色高亮，点不动
    ─────────────
    打开 Web 服务端                    ← 用系统默认浏览器开 http://127.0.0.1:8231/
    停止服务器 / 启动服务器            ← 一台机器上随时开关本地服务
    ─────────────
    MCP 状态 ▸                         ← 子菜单：开关、已接入几个 agent、逐行状态
    ─────────────
    打开服务面板                       ← 把桌面窗口还回来
    ─────────────
    设置…    ⌘,                        ← 窗口跳到设置页
    关于 FXseek                        ← 版本 / 地址 / 数据目录 / 索引规模
    ─────────────
    退出 FXseek  ⌘Q

★ 文案跟着「设置 → 外观 → 界面语言」走：中英两套都写在 TXT 里，每次开菜单
  现读一遍用户数据目录的 settings.json（服务停着也读得到），切了语言下次
  点开就是新的那一套。

★ 第二条通道：服务停掉以后 HTTP 就断了，可「启动服务器」恰恰要在这时候用。
  所以 _cmd() 在 HTTP 失败时改写用户数据目录里的 tray_cmd.json（主程序有线程
  在轮询 mtime，见 launcher.py 的 _tray_cmd_watch）。
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.request

# 菜单项 → 主程序的 HTTP 路由。命令文件那条路用的是同一套名字
# （launcher.py 拿它去查 APP.PANEL_HOOKS）。
_ROUTES = {
    "show": "/v1/panel/show",
    # ★ 路径最后一段会被 rsplit 出来当钩子名（"-"→"_"），所以这里必须写
    #   panel-toggle 而不是 toggle —— 写成 /v1/panel/toggle 的话钩子名变成
    #   "toggle"，PANEL_HOOKS 里没有它，直接回「只在桌面版里可用」。
    "panel_toggle": "/v1/panel/panel-toggle",
    "hide": "/v1/panel/hide",
    "settings": "/v1/panel/settings",
    "settings_mcp": "/v1/panel/settings-mcp",
    "server_start": "/v1/server/start",
    "server_stop": "/v1/server/stop",
    "quit": "/v1/app/quit",
    # 工作模式：菜单栏那一下单击。**单独一条「翻转」路由**，而不是让菜单先读
    # /v1/work/state 再自己判断方向 —— 那中间隔着一次往返，用户手快连点两下会
    # 两次都读到同一个旧值、于是同向翻两次，表现成「点了没反应」。
    # 判断和写入都在服务端同一个函数里，单线程串行，没有这个缝。
    "work_toggle": "/v1/work/toggle",
}

# 进程级状态：菜单每次打开都会按它重画一遍
STATE = {"port": 0, "data_dir": "", "running": True, "mcp": None, "about": None}
# 菜单项引用要一直拿着，否则刷新时找不到它们
ITEMS = {}
_KEEP = []
_STATUS = {}          # 状态栏图标和它的菜单（左键 action 要用）          # 替死鬼图标 + 菜单代理，都得有人引用着
_PENDING_SACS = []  # 建真图标时顺手记下替死鬼，等真图标排上后统一藏掉（防透明占位堆积）

# ---------------------------------------------------------------- 狒狒图标
# 菜单栏图标：白色狒狒剪影连续帧（assets/baboon_strip_f0..17.png，亮度已转 alpha，
# template 模式跟菜单栏深浅色自动反色）。
# 帧序列怎么来的：拿原 6 个姿势，先按「躯干重心 x + 脚底线」对齐去掉抖动，
# 再用距离矩阵挑出首尾衔接最顺的循环顺序（衔接跳变 20% → 6%），最后每两个
# 姿势之间交叉溶解插 3 小步 → 18 帧，相邻帧差异降到原来的 1/3，看着是连续在跑。
# 动效 = NSTimer 快速循环 setImage_（学 Mole/RunCat 的 runner-* sprite 条）。
# ★ 实测结论（/tmp/ducktest 两轮验证）：item 排上（按钮 window 高 30）之后，
#   setImage_ 运行期换图**实时生效**——前提是每帧是**独立文件切的 NSImage**；
#   用 lockFocus/drawInRect 从横条现切的不行（template 渲染后不随换图刷新）。
#   早先「只能重建 item 换帧」的结论只对没排上的 item 成立。
_FRAME_CACHE = {}
_ABOUT_CTL_CLS = None   # 「关于」面板按钮控制器类（pyobjc 类只能定义一次）
STRIP_N = 18         # 狒狒奔跑帧数（6 个姿势 × 3 个交叉溶解小步，见 assets 生成脚本）
STRIP_SEC = 0.036    # 每帧时长（秒）—— 18 帧 ≈ 0.65s 一轮，衔接顺、看着像一直在跑


def _res_dir():
    """资源目录：开发态是源码 assets/；打包态是 FXseek.app/Contents/Resources/app/assets/。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _baboon_frame(idx):
    """取第 idx 帧狒狒图标（NSImage，template 模式）。
    优先连续帧 assets/baboon_strip_f{0..17}.png；缺文件时回落到静态的
    baboon_menu_template.png，全缺再退系统符号（图标不能没有）。"""
    import AppKit
    p = os.path.join(_res_dir(), "baboon_strip_f%d.png" % (idx % STRIP_N))
    if os.path.exists(p):
        key = "strip_f%d" % (idx % STRIP_N)
        if key in _FRAME_CACHE:
            return _FRAME_CACHE[key]
        try:
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(p)
            if img is not None:
                img.setTemplate_(True)
                # 保持画布 208:126 的宽高比（≈1.65），别把奔跑的狒狒压扁
                img.setSize_((26, 16))
                _FRAME_CACHE[key] = img
                return img
        except Exception:
            pass
    names = ["baboon_menu_template.png"]
    name = names[idx % len(names)]
    if name in _FRAME_CACHE:
        return _FRAME_CACHE[name]
    p = os.path.join(_res_dir(), name)
    img = None
    try:
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(p)
        if img is not None:
            img.setTemplate_(True)      # 跟着菜单栏深浅色自动反色
            img.setSize_((22, 22))
    except Exception:
        img = None
    if img is None:                     # 图丢了就退回系统符号，图标不能没有
        try:
            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "sparkles", "FXseek")
            if img is not None:
                img.setTemplate_(True)
        except Exception:
            img = None
    _FRAME_CACHE[name] = img
    return img


def _baboon_anim_frames():
    """动效帧：连续帧 strip（STRIP_N 帧）；缺文件时回落成单帧静态图标。"""
    frames = []
    for idx in range(STRIP_N):
        f = _baboon_frame(idx)
        if f is not None:
            frames.append(f)
    return frames or [_baboon_frame(0)]


# 菜单文案（中英两套）。键名短的放前面，方便核对两边有没有漏。
TXT = {
    "zh": {
        "status_on": "服务器：运行中（端口 %d）",
        "status_off": "服务器：已停止（点下面那项即可启动）",
        "web": "打开 Web 服务端",
        "stop": "停止服务器",
        "start": "启动服务器",
        "mcp": "MCP 状态",
        "work_on": "切换工作模式",
        "work_off": "返回休闲模式",
        "work_note": "工作模式：唤醒键 %s",
        "wk_title": "工作模式",
        "wk_set": "设置唤醒键",
        "wk_reset": "恢复默认唤醒键",
        "wk_cap_title": "按一下新的唤醒键",
        "wk_cap_hint": "按一下你要的键（单键也可以）· Esc 取消",
        "wk_cap_ok": "已改成 %s",
        "wk_cap_bad": "这个键不能单独用：%s",
        "wk_cap_busy": "没换成：%s",
        "wk_idle": "搜索框自动隐藏",
        "wk_idle_0": "不自动隐藏",
        "wk_idle_s": "%d 秒",
        "wk_idle_m": "%d 分钟",
        "settings": "设置…",
        "about": "关于 FXseek",
        "quit": "退出 FXseek",
        "mcp_on": "MCP 服务：已开启",
        "mcp_off": "MCP 服务：未开启",
        "mcp_agents": "已接入 %d 个 agent（已装 %d 个）",
        "mcp_skills": "配套技能：已装 %d 个",
        "mcp_open": "打开 MCP 设置…",
        "mcp_noserver": "服务已停止，先「启动服务器」",
        "mcp_loading": "读取中…（服务还没响应）",
        "mcp_unavailable": "MCP 模块不可用：%s",
        "mark_noinstall": "未安装",
        "mark_connected": "已接入",
        "mark_offline": "未接入",
        "about_title": "FXseek 媒体库",
        "about_subtitle": "多模态快速检索智能管家",
        "about_ok": "好",
        "about_version": "版本",
        "about_engine": "引擎",
        "about_engine_unknown": "未知",
        "about_indexed": "已索引",
        "about_indexed_val": "%s 个素材 · %s 条向量",
        "about_stopped": "服务",
        "about_stopped_val": "本地服务已停止",
        "about_url": "服务地址",
        "about_data": "数据目录",
        "about_mcp": "MCP 地址",
        "about_copyright": "© %s FXseek · 本地多模态语义检索",
    },
    "en": {
        "status_on": "Server: running (port %d)",
        "status_off": "Server: stopped (start it from the item below)",
        "web": "Open web console",
        "stop": "Stop server",
        "start": "Start server",
        "mcp": "MCP status",
        "work_on": "Switch to Work Mode",
        "work_off": "Back to Leisure Mode",
        "work_note": "Work Mode: wake key %s",
        "wk_title": "Work Mode",
        "wk_set": "Set Wake Key",
        "wk_reset": "Reset Wake Key",
        "wk_cap_title": "Press the new wake key",
        "wk_cap_hint": "Press the key you want (single key is fine) · Esc to cancel",
        "wk_cap_ok": "Changed to %s",
        "wk_cap_bad": "That key can't be used alone: %s",
        "wk_cap_busy": "Failed: %s",
        "wk_idle": "Auto-hide Search Box",
        "wk_idle_0": "Never",
        "wk_idle_s": "%d sec",
        "wk_idle_m": "%d min",
        "settings": "Settings…",
        "about": "About FXseek",
        "quit": "Quit FXseek",
        "mcp_on": "MCP server: on",
        "mcp_off": "MCP server: off",
        "mcp_agents": "%d agent(s) connected (%d installed)",
        "mcp_skills": "Skills installed: %d",
        "mcp_open": "Open MCP settings…",
        "mcp_noserver": "Server is stopped — start it first",
        "mcp_loading": "Loading… (server not responding)",
        "mcp_unavailable": "MCP module unavailable: %s",
        "mark_noinstall": "not installed",
        "mark_connected": "connected",
        "mark_offline": "not connected",
        "about_title": "FXseek Library",
        "about_subtitle": "Multimodal Fast Retrieval Agent",
        "about_ok": "OK",
        "about_version": "Version",
        "about_engine": "Engine",
        "about_engine_unknown": "Unknown",
        "about_indexed": "Indexed",
        "about_indexed_val": "%s items · %s vectors",
        "about_stopped": "Service",
        "about_stopped_val": "Local server stopped",
        "about_url": "Server",
        "about_data": "Data dir",
        "about_mcp": "MCP",
        "about_copyright": "© %s FXseek · Local Multimodal Semantic Search",
    },
}


def _lang():
    """界面语言：跟设置页那个开关同源（<data_dir>/settings.json 的 lang）。

    走文件而不是 HTTP —— 服务被我们自己停掉时这个菜单还得能画出来。
    读不到、或者值不是 en，都当中文（跟 app.py 的 DEFAULT_SETTINGS 一致）。
    """
    d = STATE.get("data_dir") or ""
    if not d:
        return "zh"
    try:
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            return "en" if (json.load(f) or {}).get("lang") == "en" else "zh"
    except Exception:
        return "zh"


def _setting(key, default=None):
    """读一条设置。跟 `_lang()` 一样走文件 —— 服务停了菜单也得能画出来。

    单独读一次文件而不是缓存：菜单打开频率很低（人手点），而设置页就在隔壁，
    可能刚改过（比如刚切了模式、刚换了唤醒键），缓存反而会让菜单显示旧状态。
    """
    d = STATE.get("data_dir") or ""
    if not d:
        return default
    try:
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            v = (json.load(f) or {}).get(key)
        return default if v is None else v
    except Exception:
        return default


def _work_is_on():
    return str(_setting("ui_mode", "leisure")).strip().lower() == "work"


def _work_key_display():
    """把 `alt+cmd+space` 这种存法翻成 `⌥⌘Space` 给人看。

    app.py 里已经有一份 `_work_hotkey_display`，但那是主程序的函数 —— 托盘助手是
    **独立进程**，服务停掉时 import 不到它，所以这里得有一份自己的。只做显示，
    不做规范化（规范化只发生在设置页写入那一次）。
    """
    hk = str(_setting("work_hotkey", "") or "").strip().lower()
    if not hk:
        return ""
    names = {"cmd": "⌘", "alt": "⌥", "ctrl": "⌃", "shift": "⇧"}
    parts = [names.get(p, p) for p in hk.split("+") if p]
    if parts and parts[-1] in ("space", "空格"):
        parts[-1] = "Space"
    return "".join(parts)


# 自动收的候选秒数（0 = 不自动收）。放模块级，菜单构建和标签渲染都要用。
_IDLE_CHOICES = (0, 10, 30, 60, 180)


def _idle_label(sec):
    """0 → 不自动隐藏；<60 → N 秒；否则 N 分钟。"""
    sec = int(sec or 0)
    if sec <= 0:
        return _t("wk_idle_0")
    if sec < 60:
        return _t("wk_idle_s", sec)
    return _t("wk_idle_m", sec // 60)


def _apply_group(work_on, items=None):
    """按当前模式互斥显示两组菜单项。

    `items` 传 None 时用 `ITEMS` 里那份名单（菜单还没建完时会踩空，所以建菜单
    那次必须把两组的名单显式传进来）。
    """
    it = items or ITEMS
    try:
        for x in it.get("_work", []):
            x.setHidden_(not work_on)
        for x in it.get("_leisure", []):
            x.setHidden_(bool(work_on))
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 切菜单分组失败：%s" % e)


def _t(key, *a):
    """取一条文案。缺词条时回落到中文（宁可中文也不要在菜单里露出英文键名）。"""
    s = TXT.get(_lang(), TXT["zh"]).get(key) or TXT["zh"].get(key) or key
    return (s % a) if a else s


# ★ 本机请求必须绕开系统代理再发。
#
# 这台机器上装着 DevSidecar 这类加速工具（系统代理曾指向 127.0.0.1:31188，日志里
# 有 94 处 DevSidecar 痕迹），而 Python 的 urllib **默认连 127.0.0.1 也套代理**
# ——`getproxies()` 会返回它、`proxy_bypass("127.0.0.1")` 还是 False。
# 对探活来说这是致命的：代理多一跳、还带自己的超时，偶尔拒掉本机请求，
# /health 于是「探不到」→ _watch 判服务已死 → 图标自杀 → 菜单栏图标永久消失。
# （net_util.py 顶部记的就是这个代理坑，那次坑的是 oMLX 转写。）
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(port, path, data=None, method="GET", timeout=2.5):
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=(body if method == "POST" else None),
        headers=({"Content-Type": "application/json"} if method == "POST" else {}),
        method=method)
    with _OPENER_DIRECT.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw or "{}")


def _cmd(name):
    """让主程序干活。

    服务在跑：走本机 HTTP，快而且能看到返回值。
    服务停了：HTTP 这条路已经断了（「启动服务器」正是这种情况），退回命令文件。
    """
    path = _ROUTES.get(name)
    if path and STATE["running"]:
        try:
            return True, _http(STATE["port"], path, method="POST", timeout=3.0)
        except Exception as e:
            print("[tray] %s 走 HTTP 没成（%s），改用命令文件" % (name, e))
    d = STATE.get("data_dir") or ""
    if not d:
        return False, {"error": "不知道用户数据目录，命令文件也没法写"}
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "tray_cmd.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cmd": name, "at": time.time()}, f)
        os.replace(tmp, p)
        return True, {"via": "file"}
    except Exception as e:
        return False, {"error": str(e)}


def _open_url(url):
    """用系统默认浏览器打开（「打开 Web 服务端」）。"""
    try:
        import AppKit
        ok = AppKit.NSWorkspace.sharedWorkspace().openURL_(
            AppKit.NSURL.URLWithString_(url))
        print("[tray] 打开 %s → %s" % (url, ok))
        return ok
    except Exception as e:
        print("[tray] 打开 %s 失败：%s" % (url, e))
        return False



# ---------------------------------------------------------------- 「关于」面板
# 手搓横向面板（跟 launcher 的关闭确认框同一套视觉：横向长方形、磨砂半透明、
# 标题 + 信息行 + 单个「好」按钮）。NSAlert 竖排按钮 + 大图标砍不掉，弃用。
ABOUT_TITLE = "FXseek 媒体库"   # 仅作默认值；面板标题实际走 _t("about_title")


def _AppKit():
    """按需取 AppKit（模块级延迟导入，_show_about 只在菜单回调里跑）。"""
    import AppKit
    return AppKit


def _app_version():
    """本地取应用版本号（服务没跑时「关于」面板也得显示对）。

    ★ 别再写死版本号。app.py 里那行 `"version": "x.y.z"` 是唯一来源，
    build_app.sh / release.sh 也是 grep 它；这里按同样的办法读，读不到再退到
    bundle 的 Info.plist（CFBundleShortVersionString）。
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "app.py"), encoding="utf-8") as f:
            for line in f:
                if '"version"' in line:
                    m = re.search(r'"version"\s*:\s*"([^"]+)"', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    try:
        # Contents/Resources/app/tray_helper.py → Contents/Info.plist
        import plistlib
        pl = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "Info.plist")
        with open(pl, "rb") as f:
            v = plistlib.load(f).get("CFBundleShortVersionString")
        if v:
            return str(v)
    except Exception:
        pass
    return ""


# ==========================================================================
# 唤醒键：菜单里点一下 → 直接开始录（**没有设置面板**）
# ==========================================================================
# ★ 范老板 2026-10-15 第⑦条：「快捷键设置不要单独设置面板，把功能做在菜单栏即可，
#   简单明了美观」；紧接着又补一句（m28310）：「快捷键是用户自定义啊，不是让你改成
#   默认几个」。
#   所以两头都得松：**不能**开一扇设置面板，也**不能**只给几个预设键。入口就留在
#   菜单栏（「设置唤醒键…」），点一下直接进入「录键」状态，用户按什么就是什么。
#
#   录键为什么不在菜单里做：NSMenu 打开时跑的是它自己的事件循环，键盘事件先被菜单
#   拿去（方向键导航 / type-select），局部监听器收不到几个键。所以点下菜单项后
#   **菜单自己收起**，我们在屏幕底部挂一条提示胶囊（不是窗口：无标题栏、点不到、
#   不抢焦点，几秒就走），同时挂 NSEvent 的局部 + 全局监听器：
#     · 局部监听器：本进程是活跃 App 时收键 —— 正常路径就是这条
#     · 全局监听器：焦点在别的 App 上也收得到（要辅助功能权限）。拿不到也不影响
#       主路径，只是少了这层便利。
#   Esc 取消，6 秒没按也取消。
_KC_NAMES = None


def _kc_names():
    """keycode → 键名。优先用 hotkey.py 那一张表（和服务端解析同一份，不会出现
    「这边录出 `return`、那边只认 `enter`」）；import 不到就退回本地小表。"""
    global _KC_NAMES
    if _KC_NAMES is not None:
        return _KC_NAMES
    tbl = {}
    try:
        import hotkey as _hk
        tbl = dict(getattr(_hk, "NAME_BY_KEYCODE", {}) or {})
    except Exception:
        pass
    if not tbl:
        tbl = {0: "a", 11: "b", 8: "c", 2: "d", 14: "e", 3: "f", 5: "g", 4: "h",
               34: "i", 38: "j", 40: "k", 37: "l", 46: "m", 45: "n", 31: "o",
               35: "p", 12: "q", 15: "r", 1: "s", 17: "t", 32: "u", 9: "v",
               13: "w", 7: "x", 16: "y", 6: "z",
               29: "0", 18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6",
               26: "7", 28: "8", 25: "9", 49: "space", 36: "return", 48: "tab",
               51: "delete", 53: "esc", 123: "left", 124: "right", 125: "down",
               126: "up", 122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5",
               97: "f6", 98: "f7", 100: "f8", 101: "f9", 109: "f10",
               103: "f11", 111: "f12", 105: "f13", 107: "f14", 113: "f15"}
    _KC_NAMES = tbl
    return tbl


# 这些键单独按（不带修饰键）没意义：注册了会把全系统这个键吞掉。
# 和服务端 hotkey.py 的 `_WEAK_ALONE` 保持同一份口径。
_WEAK_ALONE = (set("abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./ ") |
               {"space", "tab", "return", "enter", "delete", "backspace",
                "esc", "escape", "forwarddelete"})

# NSEventModifierFlag 的位 → 存储用的名字（次序按 Apple 的 ⌃⌥⇧⌘）。
_MOD_BITS = (("ctrl", 1 << 18), ("alt", 1 << 19),
             ("shift", 1 << 17), ("cmd", 1 << 20))


def _hotkey_label(combo):
    """`"alt+cmd+space"` → `"⌥⌘Space"`。优先用 hotkey.py 那份（跟服务端同一套符号
    次序），import 不到就退回本地小表 —— 托盘助手是独立进程，服务没起来时它也得
    能把提示画出来。"""
    raw = str(combo or "").strip().lower()
    try:
        import hotkey as _hk
        d = _hk.display(raw)
        if d:
            return d
    except Exception:
        pass
    sym = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
    parts = [sym.get(x, x) for x in raw.split("+") if x]
    if parts and parts[-1] in ("space", "空格"):
        parts[-1] = "Space"
    return "".join(parts) or combo


# ---- 录键时屏幕底部那条提示胶囊 -------------------------------------------
# ★ 它**不是**窗：无标题栏、不抢焦点、`setIgnoresMouseEvents_` 点不到，几秒自己走。
#   范老板要去掉的是「设置面板」，不是反馈 —— 录键时必须有东西告诉用户
#   「现在按键盘」和「录成了 / 没录成」。
_HUD = {"win": None, "label": None, "token": 0, "cls": None}


def _hud_cls():
    """能当 key window 的 NSWindow 子类。

    ★ 这是「快捷键设置不了」的根因，记牢：
      `NSEvent.addGlobalMonitorForEventsMatchingMask_` 想收到**别的 App 的**
      键盘事件，必须先拿到「辅助功能 / 输入监控」权限；没授权时它一枚按键都
      收不到（不报错，静悄悄地什么都不发生）。而 `addLocalMonitor…` 只看得见
      **送进本进程**的事件 —— tray 是个没有 key window 的常驻小进程，键盘
      全给了当前前台 App，所以它同样收不到。
      结果就是：菜单点了「设置唤醒键…」、提示条也弹出来了，用户按什么键都
      没反应，6 秒后提示条自己收走 —— 用户看到的就是「快捷键设置不了」。
      办法是录键期间把本 App 激活，并让提示条这个无边框窗能成为 key window，
      键盘事件就会送进我们进程，局部监听器立刻收得到（不需要任何系统权限）。
      NSWindow 默认 `canBecomeKeyWindow` 是 False（无边框窗返回 False），
      所以必须覆写。
      """
    import AppKit as AK
    if not _HUD.get("cls"):
        class _HudWindow(AK.NSWindow):
            def canBecomeKeyWindow(self):
                return True

            def canBecomeMainWindow(self):
                return True
        _HUD["cls"] = _HudWindow
    return _HUD["cls"]


def _hud_focus():
    """录键期间：激活本 App + 让提示条成为 key window，键盘才会送进来。"""
    import AppKit as AK
    try:
        AK.NSApp.activateIgnoringOtherApps_(True)
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 激活 App 失败：%s" % e)
    try:
        if _HUD["win"]:
            _HUD["win"].makeKeyAndOrderFront_(None)
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 提示条拿不到键盘焦点：%s" % e)


def _hud_make():
    import AppKit as AK
    # ★ 用 Titled 而不是 Borderless：无边框窗 `canBecomeKeyWindow` 天然是 False，
    #   拿不到键盘焦点 = 录键永远收不到按键。标题栏用「隐藏 + 透明」做掉，
    #   视觉上仍然是原来那枚干净胶囊。
    w = _hud_cls().alloc().initWithContentRect_styleMask_backing_defer_(
        AK.NSMakeRect(0, 0, 560, 62), AK.NSWindowStyleMaskTitled,
        AK.NSBackingStoreBuffered, False)
    w.setTitleVisibility_(AK.NSWindowTitleHidden)
    w.setTitlebarAppearsTransparent_(True)
    w.setMovableByWindowBackground_(True)
    w.setOpaque_(False)
    w.setBackgroundColor_(AK.NSColor.clearColor())
    w.setLevel_(AK.NSStatusWindowLevel)
    w.setIgnoresMouseEvents_(True)
    w.setReleasedWhenClosed_(False)
    cv = w.contentView()
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
    # ★ 范老板 2026-10-15 第①条：提示条位置要整、文字要在胶囊里居中。
    #   NSTextField 默认把单行文字画在 frame 的**顶部**，最初那版文字就贴着
    #   胶囊上沿、下面空一大截。这里给一个 20px 高、上下留白的 frame 把它居中。
    lb = AK.NSTextField.alloc().initWithFrame_(
        AK.NSMakeRect(14, 21, 560 - 28, 20))
    lb.setBezeled_(False)
    lb.setDrawsBackground_(False)
    lb.setEditable_(False)
    lb.setSelectable_(False)
    lb.setAlignment_(AK.NSTextAlignmentCenter)
    lb.setTextColor_(AK.NSColor.whiteColor())
    lb.setFont_(AK.NSFont.boldSystemFontOfSize_(14.5))
    lb.setAutoresizingMask_(AK.NSViewWidthSizable)
    host.addSubview_(lb)
    _HUD["win"] = w
    _HUD["label"] = lb
    _KEEP.extend([w, lb])
    return w, lb


def _hud_hide():
    try:
        if _HUD["win"]:
            _HUD["win"].orderOut_(None)
    except Exception:
        pass


def _hud_show(text, secs=0.0):
    """屏幕底部中间显示一条提示；`secs` > 0 时到点自动收（用 token 防止旧定时器
    把新提示收掉）。"""
    import AppKit as AK
    from PyObjCTools import AppHelper
    try:
        w, lb = (_HUD["win"], _HUD["label"]) if _HUD["win"] else _hud_make()
        lb.setStringValue_(str(text))
        scr = AK.NSScreen.mainScreen()
        f = scr.visibleFrame() if scr else AK.NSMakeRect(0, 0, 1440, 900)
        sz = w.frame().size
        w.setFrameOrigin_(AK.NSMakePoint(
            f.origin.x + (f.size.width - sz.width) / 2.0, f.origin.y + 92.0))
        w.orderFrontRegardless()
        _HUD["token"] += 1
        if secs:
            _tok = _HUD["token"]
            AppHelper.callLater(float(secs),
                                lambda: _hud_hide() if _HUD["token"] == _tok else None)
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 提示没画出来：%s" % e)


def _combo_from(kc, flags):
    """`(keycode, NSEventModifierFlag)` → `(combo, err)`。

    `err` 为 `None` 表示可用；`"unknown"` 是这个键不好认。抽成纯函数是为了能单独
    测（合成按键打不进 NSEvent 监听器，只能把这段逻辑拿开测）。
    """
    names = [n for n, b in _MOD_BITS if flags & b]
    name = _kc_names().get(int(kc))
    if not name:
        return None, "unknown"
    # ★ 范老板 2026-10-15：「快捷键不要做任何限制」。单键（含单个字母/数字/空格）
    # 一律放行 —— 用户按什么就是什么。代价是注册单键后那个键会被全系统吞掉，
    # 这是用户自己的选择，不由我们替他挡。
    return "+".join(names + [name]), None


def _hotkey_record():
    """开始录唤醒键：用户按什么就是什么，录到就 POST 给主程序。

    局部 + 全局两个监听器都挂，第一个被按下的组合胜出（`done` 上锁）。
    `_hotkey_record._busy` 防重入：连点两次菜单项不会挂两套监听器。
    """
    import AppKit as AK
    from PyObjCTools import AppHelper

    if _hotkey_record._busy:
        return
    _hotkey_record._busy = True
    st = {"done": False, "mons": []}

    def stop():
        for m in st["mons"]:
            try:
                AK.NSEvent.removeMonitor_(m)
            except Exception:
                pass
        st["mons"] = []
        _hotkey_record._busy = False

    def finish(combo=None, tip=None):
        if st["done"]:
            return
        st["done"] = True
        stop()
        if combo:
            try:
                r = _http(STATE["port"], "/v1/work/hotkey",
                          {"hotkey": combo}, method="POST", timeout=4.0)
            except Exception as e:                          # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            if isinstance(r, dict) and r.get("ok"):
                st["ok"] = True
                _hud_show(_t("wk_cap_ok", r.get("hotkey_display")
                             or _hotkey_label(combo)), 0)
            else:
                msg = (r or {}).get("error") if isinstance(r, dict) else ""
                _hud_show(_t("wk_cap_busy", msg or "?"), 0)
        elif tip:
            _hud_show(tip, 0)
        try:
            AK.NSApp().stopModal()          # 放 runModalForWindow_ 回来
        except Exception:
            pass
        cb = _STATUS.get("paint")
        if cb:
            cb()

    def on_key(ev):
        try:
            kc = int(ev.keyCode())
            flags = int(ev.modifierFlags())
            if kc == 53 and not (flags & (1 << 17 | 1 << 18 | 1 << 19 | 1 << 20)):
                finish()                                    # Esc 取消
                return ev
            combo, err = _combo_from(kc, flags)
            if err == "unknown":
                return ev
            finish(combo)
            return ev
        except Exception as e:                              # noqa: BLE001
            print("[tray] 录键出错：%s" % e)
            finish()
            return ev

    try:
        st["mons"].append(AK.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AK.NSEventMaskKeyDown, on_key))
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 局部监听器没挂上：%s" % e)
    try:
        st["mons"].append(AK.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AK.NSEventMaskKeyDown, on_key))
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 全局监听器没挂上（不影响主路径）：%s" % e)
    _hud_show("%s：%s" % (_t("wk_cap_title"), _t("wk_cap_hint")), 0)
    win = _HUD["win"]
    try:
        win.setLevel_(AK.NSModalPanelWindowLevel)
        AK.NSApp().activateIgnoringOtherApps_(True)
        win.makeKeyAndOrderFront_(None)
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 录键窗没能拿到焦点：%s" % e)
    # ★ 超时也得跑在 **modal 模式** 下：modal 循环里默认模式的 NSTimer 根本不
    #   会触发，`AppHelper.callLater(6.0, finish)` 会静默失效（这正是上一版
    #   「按了没反应、面板也不自己走」的一半原因）。
    try:
        t = AK.NSTimer.timerWithTimeInterval_repeats_block_(
            8.0, False, lambda _t: finish())
        AK.NSRunLoop.currentRunLoop().addTimer_forMode_(
            t, AK.NSModalPanelRunLoopMode)
    except Exception as e:                                  # noqa: BLE001
        print("[tray] 录键超时定时器没挂上：%s" % e)
    try:
        AK.NSApp().runModalForWindow_(win)      # 阻塞到 stopModal
    except Exception as e:                                  # noqa: BLE001
        print("[tray] modal 循环没跑起来：%s" % e)
    finally:
        try:
            win.setLevel_(AK.NSStatusWindowLevel)
        except Exception:
            pass
    # 录成功就让「已改成 ⌥⌘J」停一下再收走；取消/失败立刻收。
    if st.get("ok"):
        AppHelper.callLater(1.1, _hud_hide)
    else:
        _hud_hide()


_hotkey_record._busy = False



def _about_rows():
    """现算「关于」面板的键值行（label, value 成对），返回 (version_str, rows)。"""
    info = None
    if STATE["running"]:
        try:
            info = _http(STATE["port"], "/v1/about", timeout=2.0)
            STATE["about"] = info
        except Exception:
            info = STATE.get("about")
    else:
        info = STATE.get("about")

    rows = []
    version = _app_version()          # 服务探不到时用本地读到的版本，别写死
    if info:
        version = info.get("version") or version
        rows.append((_t("about_version"), version))
        rows.append((_t("about_engine"), info.get("engine") or _t("about_engine_unknown")))
        if info.get("files") is not None:
            rows.append((_t("about_indexed"), _t("about_indexed_val",
                         info.get("files"), info.get("vectors"))))
        d = info.get("data_dir")
    else:
        rows.append((_t("about_stopped"), _t("about_stopped_val")))
        d = STATE.get("data_dir")
    rows.append((_t("about_url"), "127.0.0.1:%d" % STATE["port"]))
    if d:
        rows.append((_t("about_data"), d))
    rows.append((_t("about_mcp"), "127.0.0.1:%d/mcp" % STATE["port"]))
    return version, rows


def _about_logo_image():
    """取 App 图标做面板大图标；优先 AppIcon.icns，其次 baboon_white.png。"""
    here = _res_dir()
    for name in ("AppIcon.icns", "baboon_white.png"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            img = _AppKit().NSImage.alloc().initWithContentsOfFile_(p)
            if img is not None:
                return img
    return None


def _show_about():
    """居中卡片式「关于」面板（参考主流 App 的 About 弹窗）。

    布局（自上而下居中）：应用大图标 → 标题（加粗）→ 副标题（灰字）→ 分隔线
    → 键值行（左 label 灰字、右 value 等宽）→ 页脚 copyright（居中灰字），
    右上角一个 × 关闭按钮。整面板磨砂半透明 + 圆角。
    """
    try:
        AK = _AppKit()
        app = AK.NSApplication.sharedApplication()
        version, rows = _about_rows()

        # 按钮控制器（类只能定义一次，进程级缓存——同 launcher._CLOSE_CTL_CLS 的坑）。
        # ★ 类名必须**全局唯一**：b575 起菜单栏「关于」由 launcher 在主进程里
        #   import 本模块直接调 _show_about()，而 launcher 自己的关闭弹窗也定义过
        #   一个 `class _Ctl` —— pyobjc 按 __name__ 注册 ObjC 类，两边同名就互相
        #   炸（日志实测 171 次 _Ctl is overriding…：关于/关闭谁后弹谁失败）。
        #   本类改名 _AboutCtl，launcher 那边是 _CloseCtl。
        global _ABOUT_CTL_CLS
        if _ABOUT_CTL_CLS is None:
            class _AboutCtl(AK.NSObject):
                def closeClick_(self, sender):
                    AK.NSApp().stopModalWithCode_(0)
            _ABOUT_CTL_CLS = _AboutCtl
        ctl = _ABOUT_CTL_CLS.alloc().init()

        # 尺寸：纵向 图标 76 + 标题 28 + 副标题 20 + 分隔 14 + 行*24 + 页脚 40 + 边距
        W = 360
        pad_top = 24
        icon_h = 76
        title_h = 28
        sub_h = 20
        sep_h = 16
        row_h = 24
        foot_h = 40
        H = pad_top + icon_h + title_h + sub_h + sep_h + row_h * len(rows) + foot_h

        panel = AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AK.NSMakeRect(0, 0, W, H),
            AK.NSWindowStyleMaskTitled,
            AK.NSBackingStoreBuffered, False)
        panel.setTitleVisibility_(AK.NSWindowTitleHidden)
        panel.setTitlebarAppearsTransparent_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setReleasedWhenClosed_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AK.NSColor.clearColor())
        panel.setLevel_(AK.NSModalPanelWindowLevel)
        panel.center()
        cv = panel.contentView()
        cv.setWantsLayer_(True)

        # 磨砂半透明底
        host = cv
        try:
            fx = AK.NSVisualEffectView.alloc().initWithFrame_(cv.bounds())
            fx.setMaterial_(AK.NSVisualEffectMaterialHUDWindow)
            fx.setBlendingMode_(AK.NSVisualEffectBlendingModeBehindWindow)
            fx.setState_(AK.NSVisualEffectStateActive)
            fx.setWantsLayer_(True)
            fx.layer().setCornerRadius_(14.0)
            fx.layer().setMasksToBounds_(True)
            fx.setAutoresizingMask_(AK.NSViewWidthSizable | AK.NSViewHeightSizable)
            cv.addSubview_(fx)
            host = fx
        except Exception:
            pass

        # ---- 顶部应用图标（居中，72px 圆角）----
        logo = _about_logo_image()
        if logo is not None:
            iv = AK.NSImageView.alloc().initWithFrame_(
                AK.NSMakeRect((W - 72) / 2.0, H - pad_top - 72, 72, 72))
            iv.setImage_(logo)
            iv.setImageScaling_(AK.NSImageScaleProportionallyUpOrDown)
            iv.setWantsLayer_(True)
            iv.layer().setCornerRadius_(16.0)
            iv.layer().setMasksToBounds_(True)
            host.addSubview_(iv)

        # ---- 标题（居中加粗）----
        title = AK.NSTextField.alloc().initWithFrame_(
            AK.NSMakeRect(20, H - pad_top - icon_h - title_h, W - 40, title_h))
        title.setStringValue_(_t("about_title"))
        title.setFont_(AK.NSFont.boldSystemFontOfSize_(17))
        title.setAlignment_(AK.NSTextAlignmentCenter)
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        host.addSubview_(title)

        # ---- 副标题（居中灰字：版本 + 广告语）----
        sub = AK.NSTextField.alloc().initWithFrame_(
            AK.NSMakeRect(20, H - pad_top - icon_h - title_h - sub_h, W - 40, sub_h))
        sub.setStringValue_("%s %s · %s" % (_t("about_version"), version, _t("about_subtitle")))
        sub.setFont_(AK.NSFont.systemFontOfSize_(12))
        sub.setTextColor_(AK.NSColor.secondaryLabelColor())
        sub.setAlignment_(AK.NSTextAlignmentCenter)
        sub.setBezeled_(False)
        sub.setDrawsBackground_(False)
        sub.setEditable_(False)
        sub.setSelectable_(False)
        host.addSubview_(sub)

        # ---- 分隔线 ----
        sep_y = H - pad_top - icon_h - title_h - sub_h - sep_h
        sep = AK.NSBox.alloc().initWithFrame_(AK.NSMakeRect(20, sep_y, W - 40, 1))
        sep.setBoxType_(AK.NSBoxSeparator)
        host.addSubview_(sep)

        # ---- 键值行（label 灰字左、value 等宽右对齐——同参考样式）----
        rows_top = sep_y - 6
        y = rows_top
        for label, value in rows:
            y -= row_h
            lab = AK.NSTextField.alloc().initWithFrame_(
                AK.NSMakeRect(20, y, 90, row_h))
            lab.setStringValue_(label)
            lab.setFont_(AK.NSFont.systemFontOfSize_(12))
            lab.setTextColor_(AK.NSColor.secondaryLabelColor())
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setEditable_(False)
            lab.setSelectable_(False)
            host.addSubview_(lab)

            val = AK.NSTextField.alloc().initWithFrame_(
                AK.NSMakeRect(114, y, W - 114 - 20, row_h))
            val.setStringValue_(value)
            val.setFont_(AK.NSFont.monospacedSystemFontOfSize_weight_(11.5, AK.NSFontWeightRegular))
            val.setTextColor_(AK.NSColor.labelColor())
            val.setLineBreakMode_(AK.NSLineBreakByTruncatingMiddle)
            val.setAlignment_(AK.NSTextAlignmentRight)
            val.setBezeled_(False)
            val.setDrawsBackground_(False)
            val.setEditable_(False)
            val.setSelectable_(True)        # 允许复制路径/地址
            host.addSubview_(val)

        # ---- 页脚 copyright（居中灰字）----
        import datetime
        year = datetime.date.today().year
        foot = AK.NSTextField.alloc().initWithFrame_(
            AK.NSMakeRect(20, 14, W - 40, 20))
        foot.setStringValue_(_t("about_copyright", year))
        foot.setFont_(AK.NSFont.systemFontOfSize_(11))
        foot.setTextColor_(AK.NSColor.tertiaryLabelColor())
        foot.setAlignment_(AK.NSTextAlignmentCenter)
        foot.setBezeled_(False)
        foot.setDrawsBackground_(False)
        foot.setEditable_(False)
        foot.setSelectable_(False)
        host.addSubview_(foot)

        # ---- 右上角 × 关闭按钮 ----
        close_btn = AK.NSButton.alloc().initWithFrame_(
            AK.NSMakeRect(W - 34, H - 34, 24, 24))
        close_btn.setTitle_("×")
        close_btn.setFont_(AK.NSFont.systemFontOfSize_(18))
        close_btn.setBezelStyle_(AK.NSBezelStyleRegularSquare)
        close_btn.setBordered_(False)
        close_btn.setTarget_(ctl)
        close_btn.setAction_("closeClick:")
        host.addSubview_(close_btn)

        try:
            AK.NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        app.runModalForWindow_(panel)
        panel.orderOut_(None)
    except Exception as e:
        print("[tray] 关于窗口弹不出来：%s" % e)



def main():
    ap = argparse.ArgumentParser(description="FXseek 菜单栏图标助手")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--parent", type=int, default=0,
                    help="主程序 pid；它没了这个助手也退出")
    ap.add_argument("--data-dir", default="",
                    help="用户数据目录；服务停掉时用命令文件跟主程序说话")
    args = ap.parse_args()
    STATE["port"] = args.port
    STATE["data_dir"] = args.data_dir

    try:
        import AppKit
    except Exception as e:                       # 没有 pyobjc 就没辙了
        print("[tray] 缺少 AppKit：%s" % e)
        return 1
    from PyObjCTools import AppHelper

    app = AppKit.NSApplication.sharedApplication()
    # Accessory＝不占程序坞、不抢焦点，只在菜单栏露个脸。
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    MI = AppKit.NSMenuItem

    # ---------------------------------------------------------------- 状态查询
    def _sync():
        """菜单每次打开时刷新一遍。

        状态现读 /v1/server/state；读不到就**沿用上次的记忆** —— 刚点完
        「停止服务器」的那一瞬间 HTTP 已经断了，不能因此又把标签写回「运行中」。
        """
        try:
            st = _http(STATE["port"], "/v1/server/state", timeout=1.2)
            if isinstance(st, dict) and "running" in st:
                STATE["running"] = bool(st["running"])
        except Exception:
            pass
        if STATE["running"]:
            try:
                STATE["mcp"] = _http(STATE["port"], "/v1/mcp/status", timeout=1.5)
            except Exception:
                STATE["mcp"] = None
        else:
            STATE["mcp"] = None
        _paint()

    # ---------------------------------------------------------------- 菜单绘制
    def _title_item(text, color=None, bold=False):
        """禁用（点不动）的标题项，可以带颜色 / 加粗 —— 状态行就是靠它高亮的。

        加粗必须**跟随菜单字号**：`boldSystemFontOfSize_(0)` 会返回 13pt，而
        `menuFontOfSize_(0)` 是 14pt，直接用会让高亮项比邻居小一号。
        """
        mi = MI.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
        mi.setEnabled_(False)
        if color is not None or bold:
            try:
                base = AppKit.NSFont.menuFontOfSize_(0)
                font = (AppKit.NSFont.boldSystemFontOfSize_(base.pointSize())
                        if bold else base)
                attrs = {AppKit.NSFontAttributeName: font}
                if color is not None:
                    attrs[AppKit.NSForegroundColorAttributeName] = color
                mi.setAttributedTitle_(AppKit.NSAttributedString.alloc()
                                       .initWithString_attributes_(text, attrs))
            except Exception:
                pass
        return mi

    def _dim(color, alpha=0.66):
        """把系统语义色压淡一档（保留色相，只降分量）。

        ★ 为什么需要它：_strong_item 是自定义视图，不参与菜单的「禁用压暗」，
          所以 systemGreen 这种高饱和色渲出来会比周围亮一大截，看起来像加粗。
          直接换次要灰又丢了「运行中 / 已停止」的色相信号，所以折中成半透明。
          colorWithAlphaComponent_ 拿不到时原样返回 —— 宁可不淡，也别崩。
        """
        try:
            return color.colorWithAlphaComponent_(alpha)
        except Exception:
            return color

    def _strong_item(text, color=None, bold=True):
        """「点不动，但颜色说了算」的标题项（自定义视图，绕开禁用压暗）。

        ★ 为什么不能直接 _title_item(text, labelColor)：
        AppKit 对**所有禁用项**统一压暗绘制，连显式写进 attributed title 里的
        颜色也盖不过去。实测同一条 labelColor：禁用态渲染成灰色，启用态才是
        纯黑。所以之前「已接入的 agent 用 labelColor」在菜单里一直还是灰的。

        这里改用**自定义视图**（一个不可编辑的 NSTextField 当菜单项）：它不
        参与菜单那套「禁用压暗」绘制，于是颜色能如实显示，同时仍然是点不动、
        悬停不泛蓝的状态行（对比过「改成 setEnabled_(True) + 关掉
        autoenablesItems」那条路，那样虽然也黑，但整行会变得像能点的按钮）。

        bold 默认 True，但 MCP 面板现在**全部传 False** —— 用户反馈整块加粗
        太压眼，正文粗细更耐看。这里保留参数是为了 `_set_strong` 的调用方
        （状态行）也能按需切换。
        """
        mi = MI.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        mi.setEnabled_(False)
        try:
            base = AppKit.NSFont.menuFontOfSize_(0)
            tf = AppKit.NSTextField.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, 200, 20))
            tf.setStringValue_(text)
            tf.setBezeled_(False)
            tf.setDrawsBackground_(False)
            tf.setEditable_(False)
            tf.setSelectable_(False)
            tf.setFont_(AppKit.NSFont.boldSystemFontOfSize_(base.pointSize()) if bold
                        else base)
            tf.setTextColor_(color if color is not None else AppKit.NSColor.labelColor())
            tf.sizeToFit()                    # 按文本量宽度，长 agent 名不会被截断
            f = tf.frame()
            tf.setFrame_(AppKit.NSMakeRect(0, 0, f.size.width + 2, 20))
            mi.setView_(tf)
        except Exception as e:
            print("[tray] 加粗菜单项失败，退化成普通标题：%s" % e)
            mi.setTitle_(text)
        return mi

    def _set_strong(mi, text, color=None):
        """改 _strong_item 那行的文字 / 颜色（它是个自定义视图，不能走 _set_title）。

        宽度要跟着文字重量一遍，否则「服务器：运行中（端口 8231）」切到
        「服务器：已停止（点下面那项即可启动）」这种更长的文案会被截断。
        """
        tf = mi.view()
        if tf is None:
            _set_title(mi, text, color if color is not None else AppKit.NSColor.labelColor())
            return
        try:
            tf.setStringValue_(text)
            tf.setTextColor_(color if color is not None else AppKit.NSColor.labelColor())
            tf.sizeToFit()
            f = tf.frame()
            tf.setFrame_(AppKit.NSMakeRect(0, 0, f.size.width + 2, 20))
        except Exception as e:
            print("[tray] 刷新加粗菜单项失败：%s" % e)

    def _action(title, sel, target, key=""):
        mi = MI.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
        mi.setTarget_(target)
        return mi

    def _style_work_item(mi, title, symbol=""):
        """工作模式那两项（切换 / 返回）的统一外观：**主色 + 加粗 + 图标**。

        ★ 范老板 2026-10-15 第⑤条：「『切换工作模式』『返回休闲模式』按钮采用
          合理的样式高亮醒目显示」。这两项干的是「整个 App 换一套界面」的大事，
          原来却和「关于 FXseek」长得一模一样，混在菜单里根本找不到。
        ★ 为什么是 attributed title 而不是 _strong_item 那种自定义视图：
          自定义视图是给**点不动**的状态行用的（还得自己把点击转成动作）；
          这两项必须能点。而**启用态**的菜单项会照 attributed title 里的前景色
          如实渲染（只有禁用态才被系统统一压暗，见 _strong_item 那段教训），
          所以主色 + 加粗对可点项是有效的。
        ★ 标题会随模式变，而 `setTitle_` 会把 attributed title 顶掉，所以每次
          重设标题都得重新套一遍这个函数 —— 别在别处直接 setTitle_ 它俩。
        """
        try:
            base = AppKit.NSFont.menuFontOfSize_(0)
            mi.setAttributedTitle_(AppKit.NSAttributedString.alloc()
                                   .initWithString_attributes_(title, {
                                       AppKit.NSFontAttributeName:
                                           AppKit.NSFont.boldSystemFontOfSize_(
                                               base.pointSize()),
                                       AppKit.NSForegroundColorAttributeName:
                                           AppKit.NSColor.controlAccentColor(),
                                   }))
        except Exception:                                   # noqa: BLE001
            try:
                mi.setTitle_(title)
            except Exception:                               # noqa: BLE001
                pass
        if symbol:
            try:
                img = (AppKit.NSImage
                       .imageWithSystemSymbolName_accessibilityDescription_(
                           symbol, title))
                if img is not None:
                    mi.setImage_(img)
            except Exception:                               # noqa: BLE001
                pass
        return mi

    def _paint():
        """按当前状态把菜单各项的标题 / 颜色 / 可用性刷一遍。

        文案也在这里重设 —— 语言是设置页那个开关，用户切完下次点开菜单就换过来。
        """
        running = bool(STATE["running"])
        # 状态行也是自定义视图（_strong_item），同样不参与菜单的「禁用压暗」，
        # 直接给 systemGreen 会比邻居亮一大截、看着发粗。用户要求「不要加粗，
        # 淡化」—— 这里压低不透明度：饱和度还在（一眼能分出运行中/已停止），
        # 但分量降下来，不再抢菜单里真正能点的那些项。
        if running:
            _set_strong(ITEMS["status"], _t("status_on", STATE["port"]),
                        _dim(AppKit.NSColor.systemGreenColor()))
        else:
            _set_strong(ITEMS["status"], _t("status_off"),
                        _dim(AppKit.NSColor.systemOrangeColor()))
        ITEMS["toggle"].setTitle_(_t("stop") if running else _t("start"))
        ITEMS["web"].setTitle_(_t("web"))
        ITEMS["mcp"].setTitle_(_t("mcp"))
        # 工作模式那两项：模式可能在别处改的（快捷键、设置页、甚至命令行），
        # 所以每次开菜单都按磁盘上的真值重画一遍，不信任内存里的旧状态。
        # ★ 第⑤条：这两项是高亮项（主色+加粗+图标），而 `setTitle_` 会把
        #   attributed title 顶掉，所以标题变了必须走 _style_work_item 重套一遍，
        #   不能直接 setTitle_。图标跟着状态走：能「返回休闲」时给房子，
        #   能「切进工作」时给公文包。
        _on = _work_is_on()
        _style_work_item(ITEMS["work"],
                         _t("work_off") if _on else _t("work_on"),
                         "house" if _on else "briefcase")
        ITEMS["work"].setEnabled_(running)   # 服务没跑：切模式发不出请求，先禁用
        ITEMS["work_back"].setEnabled_(running)
        # 面板整组换（工作模式那套只有：设置唤醒键 / 恢复默认 / 返回休闲 / 退出）
        _apply_group(_on)
        ITEMS["wk_title"].setTitle_(_t("wk_title"))
        # ★ 唤醒键这一项现在只显示**当前键**（点了就直接进录键态，见 setWorkKey_）。
        #   把当前值写在标题里，用户不点开也知道现在用的是哪个 —— 菜单栏里就这点
        #   地方，比再拆一层子菜单干净。
        _hcur = (str(_setting("work_hotkey", "") or "").strip().lower()
                 or "alt+cmd+space")
        ITEMS["wk_set"].setTitle_("%s…   %s" % (_t("wk_set"), _hotkey_label(_hcur)))
        ITEMS["wk_reset"].setTitle_(_t("wk_reset"))
        _style_work_item(ITEMS["work_back"], _t("work_off"), "house")
        ITEMS["wk_idle"].setTitle_(_t("wk_idle"))
        _cur = int(_setting("work_idle_sec", 60) or 0)
        _sub = ITEMS["wk_idle"].submenu()
        if _sub:
            for _it in _sub.itemArray():
                _it.setState_(1 if int(_it.representedObject() or 0) == _cur else 0)
        ITEMS["settings"].setTitle_(_t("settings"))
        ITEMS["about"].setTitle_(_t("about"))
        ITEMS["quit"].setTitle_(_t("quit"))
        ITEMS["web"].setEnabled_(running)
        _fill_mcp(ITEMS["mcp"].submenu())

    def _set_title(item, text, color): 
        item.setTitle_(text)
        try:
            item.setAttributedTitle_(AppKit.NSAttributedString.alloc()
                                     .initWithString_attributes_(text, {
                                         AppKit.NSForegroundColorAttributeName: color,
                                         AppKit.NSFontAttributeName:
                                             AppKit.NSFont.menuFontOfSize_(0),
                                     }))
        except Exception:
            pass

    def _fill_mcp(sub):
        """MCP 子菜单：开关状态 + 已接入几个 + 逐行 agent + 打开设置。"""
        sub.removeAllItems()
        if not STATE["running"]:
            sub.addItem_(_title_item(_t("mcp_noserver")))
            return
        st = STATE.get("mcp")
        if not st:
            sub.addItem_(_title_item(_t("mcp_loading")))
            return
        if st.get("available") is False:
            sub.addItem_(_title_item(_t("mcp_unavailable", st.get("error") or "")))
            return
        connected = int(st.get("connected") or 0)
        # 汇总三行用 _strong_item，但**不加粗、且必须给次要灰**。
        # ★ 为什么非得是次要灰，不能留 labelColor：
        #   菜单里别的项都是禁用状态，AppKit 会把它们统一压暗成灰色；而
        #   _strong_item 走的是自定义视图，不参与那套压暗绘制 —— 结果它顶着
        #   一身纯黑（labelColor）跟一片灰邻居并排，**看起来就像加粗了**。
        #   用户两次反馈「太粗/太丑」，其实字重早就是正文粗细了（bold=False），
        #   重的是颜色。这里换成 secondaryLabelColor()，让汇总行的视觉分量
        #   跟邻居对齐。
        sub.addItem_(_strong_item(_t("mcp_on") if st.get("enabled") else _t("mcp_off"),
                                  AppKit.NSColor.secondaryLabelColor(), bold=False))
        sub.addItem_(_strong_item(_t("mcp_agents", connected, st.get("installed", 0)),
                                  AppKit.NSColor.secondaryLabelColor(), bold=False))
        if st.get("skilled") is not None:
            sub.addItem_(_strong_item(_t("mcp_skills", st.get("skilled", 0)),
                                      AppKit.NSColor.secondaryLabelColor(), bold=False))
        sub.addItem_(MI.separatorItem())
        for r in (st.get("agents") or [])[:8]:
            name = r.get("name") or r.get("id") or "?"
            mark = (_t("mark_noinstall") if not r.get("installed")
                    else _t("mark_connected") if r.get("connected")
                    else _t("mark_offline"))
            text = ("  %s：%s" % (name, mark)) if _lang() == "zh" else ("  %s: %s" % (name, mark))
            if r.get("connected"):
                # ★ 已接入的：正文色（黑/白），但不加粗
                sub.addItem_(_strong_item(text, bold=False))
            elif r.get("installed"):
                # 装了但没接：次要灰，别跟已接入的抢注意力
                sub.addItem_(_title_item(text, AppKit.NSColor.secondaryLabelColor()))
            else:
                # 没装：最淡
                sub.addItem_(_title_item(text, AppKit.NSColor.tertiaryLabelColor()))
        sub.addItem_(MI.separatorItem())
        sub.addItem_(_action(_t("mcp_open"), b"openMcp:", ITEMS["target"]))

    class Target(AppKit.NSObject):
        """菜单项回调。必须有人一直引用着它，否则一点就悬空。"""

        def openWeb_(self, sender):
            _open_url("http://127.0.0.1:%d/" % STATE["port"])

        def toggleServer_(self, sender):
            """停止 → 启动 一台机器上的开关。改完立刻重画，不等下次开菜单。"""
            if STATE["running"]:
                ok, res = _cmd("server_stop")
                if ok:
                    STATE["running"] = False
                print("[tray] 停止服务器 → %s" % res)
            else:
                ok, res = _cmd("server_start")
                if ok:
                    STATE["running"] = True
                print("[tray] 启动服务器 → %s" % res)
            _paint()

        def iconClick_(self, sender):
            """状态栏图标被点：**左键切面板、右键弹菜单**。

            左键走的是「面板开关」而不是「打开 web 页」：休闲模式切主窗口，
            工作模式切那块覆盖窗（`work_toggle` 自己认当前模式）。
            """
            try:
                ev = AppKit.NSApp.currentEvent()
                right = bool(ev and (
                    ev.type() == AppKit.NSEventTypeRightMouseUp or
                    (ev.type() == AppKit.NSEventTypeLeftMouseUp and
                     (ev.modifierFlags() & AppKit.NSEventModifierFlagControl))))
            except Exception:
                right = False
            if right:
                m = _STATUS.get("menu")
                it = _STATUS.get("item")
                if m is not None and it is not None:
                    _paint()
                    it.popUpMenu_(m)
                return
            # 左键：工作模式切覆盖窗，休闲模式切主窗口。
            # ★ 兜底：万一这条路走不通（服务没起、路由没接上），就把菜单弹出来 ——
            #   宁可多一步，也不能让用户点了图标却什么都不发生（那看着就是「坏了」）。
            key = "work_toggle" if _work_is_on() else "panel_toggle"
            ok, res = _cmd(key)
            print("[tray] 图标左键 → %s %s" % (key, res))
            if not ok:
                m = _STATUS.get("menu"); it = _STATUS.get("item")
                if m is not None and it is not None:
                    _paint()
                    it.popUpMenu_(m)

        def showPanel_(self, sender):
            print("[tray] 打开服务面板 → %s" % (_cmd("show")[1],))

        def toggleWork_(self, sender):
            """切模式。主程序那边会顺手把窗口换掉（后台不再暂停）。

            ★ 这里**不去猜切完之后是什么模式**、也不乐观地改 STATE —— 翻转是在
              服务端定的（见 app.py 的 work_toggle），猜错就会让菜单显示反的。
              `_paint()` 本来就每次重读磁盘，所以让它自己去读就行。
            """
            ok, res = _cmd("work_toggle")
            print("[tray] 切换工作模式 → %s" % (res,))
            if ok:
                _paint()

        def setWorkKey_(self, sender):
            """「设置唤醒键…」—— 菜单收起后**直接开始录**，按什么键就是什么键。

            没有设置面板，也没有固定候选（范老板 m28310）。屏幕底部会先浮一条
            提示胶囊，键盘事件由 `_hotkey_record()` 里挂的局部 + 全局监听器接。
            """
            # ★ 录键**不在这个进程里做**：tray_helper 是没有窗口的辅助进程，
            #   激活策略是 Prohibited，窗口当不了 key window，键盘事件一条都
            #   收不到（踩过：胶囊弹出来、按什么都没反应）。改成让主进程录。
            def _go():
                try:
                    r = _http(STATE["port"], "/v1/work/hotkey/pick",
                              {}, method="POST", timeout=45.0)
                except Exception as e:                      # noqa: BLE001
                    print("[tray] 录唤醒键失败：%s" % e)
                    return
                if isinstance(r, dict) and r.get("ok"):
                    print("[tray] 唤醒键已改成 %s" % r.get("hotkey_display")
                          or r.get("hotkey"))
                elif isinstance(r, dict) and r.get("cancelled"):
                    print("[tray] 录唤醒键：用户取消")
                else:
                    print("[tray] 录唤醒键没成：%s"
                          % ((r or {}).get("error") if isinstance(r, dict) else r))
                _paint()
            threading.Thread(target=_go, daemon=True).start()

        def setIdle_(self, sender):
            """把「搜索框自动隐藏」的秒数写回设置。"""
            try:
                sec = int(sender.representedObject() or 0)
            except Exception:                           # noqa: BLE001
                sec = 60
            try:
                r = _http(STATE["port"], "/v1/work/idle", {"sec": sec},
                          method="POST", timeout=4.0)
                print("[tray] 自动隐藏 → %ss：%s" % (sec, r))
            except Exception as e:                      # noqa: BLE001
                print("[tray] 改自动隐藏没成：%s" % e)
            _paint()

        def resetWorkKey_(self, sender):
            """恢复默认唤醒键（⌥⌘Space）。"""
            try:
                r = _http(STATE["port"], "/v1/work/hotkey",
                          {"hotkey": "alt+cmd+space"}, method="POST", timeout=4.0)
                print("[tray] 恢复默认唤醒键 → %s" % (r,))
            except Exception as e:                          # noqa: BLE001
                print("[tray] 恢复默认唤醒键没成：%s" % e)
            _paint()

        def openSettings_(self, sender):
            print("[tray] 设置 → %s" % (_cmd("settings")[1],))

        def openMcp_(self, sender):
            print("[tray] MCP 设置 → %s" % (_cmd("settings_mcp")[1],))

        def aboutApp_(self, sender):
            _show_about()

        def quitApp_(self, sender):
            # 先让主程序按它自己的流程退出（不会漏掉后台任务收尾），
            # 再把自己收掉；主程序退出时也会顺手杀掉这个助手。
            _cmd("quit")
            AppHelper.callLater(1.5, _stop)

    class MenuDelegate(AppKit.NSObject):
        """菜单每次打开时先刷新数据再铺出来。"""

        def menuNeedsUpdate_(self, menu):
            try:
                _sync()
            except Exception as e:
                print("[tray] 刷新菜单失败：%s" % e)

    target = Target.alloc().init()
    delegate = MenuDelegate.alloc().init()
    _KEEP.extend([target, delegate])
    ITEMS["target"] = target
    # ★ `_paint`（在上面，比这里早定义）要在菜单打开时**重建**唤醒键二级菜单，
    #   重建就得重新绑 target。它够不着这个局部变量，所以顺手放一份到 _STATUS 里。
    _STATUS["target"] = target
    # ★ 录键是异步的（等用户按键），录完得重画菜单标题。`_hotkey_record()` 在模块
    #   层，够不着这里的 `_paint`，所以也放一份进 _STATUS。
    _STATUS["paint"] = _paint


    def _make_item(frame_idx=0):
        """建一个狒狒菜单栏图标（连菜单一起装好）并返回它。

        ★ 踩了很久的坑：这个系统（macOS 26）上 NSStatusItem 排不排得上**是随机的**，
        而且**前几个几乎必然排不上** —— 按钮窗口高度 0、isVisible() 直接是 False，
        不报错也不提示。三个最小 AppKit 探针实测（/tmp/traytest/probe*.py）：
          · 连建 5 个          → 只有第 4 个（下标 3）排上（高度 30）
          · 每 2.5 秒建一个（6 个）→ 下标 3、5 排上，0/1/2/4 全是 0
          · 每次重建前先 setVisible_(False) 旧的（8 个）→ 下标 3、5、6 排上，其余 0
        即约 1/3 概率成功、且下标 3 三次都成功；同时**活着的 item 可能不止一个**。
        所以策略是：每次建之前先放一个替死鬼占名额，排不上就换个 item 重试
        （见 _ensure_icon），绝不「建完就默认它还在」（见 _icon_alive）。

        frame_idx：初设帧（0=静止 1=抬手 2=高抬）。动效靠「重建 item 以新帧
        初设」实现——这台系统上 setImage_ 运行期换图不触发可见刷新（见下）。
        """
        sac = None
        try:
            sac = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength)
            sac.button().setTitle_("")
            _KEEP.append(sac)               # 别被回收，回收可能连名额一起还回去
            if sac is not None:
                _PENDING_SACS.append(sac)   # 真图标排上后再藏（见 _hide_pending_sacs）
        except Exception:
            pass
        it = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        b = it.button()
        try:
            img = _baboon_frame(frame_idx)  # 狒狒剪影（指定姿势）
            if img is not None:
                b.setImage_(img)
        except Exception:
            pass
        # 只留狒狒图标，不带文字（用户要求：右边的 FXseek 字样删掉）。
        b.setTitle_("")

        menu = AppKit.NSMenu.alloc().init()
        # ★ 这行也要走 _strong_item：它的绿色是显式指定的，而禁用项会被系统
        # 压暗 —— 之前它一直渲染成灰色，跟文件开头写的「绿色高亮，点不动」对不上。
        ITEMS["status"] = _strong_item(_t("status_on", args.port),
                                       _dim(AppKit.NSColor.systemGreenColor()), bold=False)
        menu.addItem_(ITEMS["status"])
        ITEMS["sep_head"] = MI.separatorItem()
        menu.addItem_(ITEMS["sep_head"])

        ITEMS["web"] = _action(_t("web"), b"openWeb:", target)
        ITEMS["toggle"] = _action(_t("stop"), b"toggleServer:", target)
        menu.addItem_(ITEMS["web"])
        menu.addItem_(ITEMS["toggle"])
        ITEMS["sep_mcp"] = MI.separatorItem()      # ← MCP 状态自成一组
        menu.addItem_(ITEMS["sep_mcp"])

        ITEMS["mcp"] = MI.alloc().initWithTitle_action_keyEquivalent_(_t("mcp"), None, "")
        ITEMS["mcp"].setSubmenu_(AppKit.NSMenu.alloc().init())
        menu.addItem_(ITEMS["mcp"])
        ITEMS["sep_work"] = MI.separatorItem()     # ← 工作模式自成一组
        menu.addItem_(ITEMS["sep_work"])
        # ── 工作模式（1.11 预览版）───────────────────────────────────────
        # 用户原话是「在菜单栏单击可以切换」。菜单栏图标左键必然弹菜单（系统行为，
        # 改不掉），所以那「一下单击」就落在菜单里这一项上 —— **一步到位**，不套
        # 二级菜单：反正整台机器上同一时刻只有一种模式，做成开关比做成选择器少一次
        # 点击，也更不容易点错。标题直接写成动作（「切换工作模式」/「返回休闲模式」）
        # 而不是状态 + 勾选，因为点完菜单就收了，勾选根本来不及看。
        ITEMS["work"] = _style_work_item(
            _action(_t("work_off") if _work_is_on() else _t("work_on"),
                    b"toggleWork:", target),
            _t("work_off") if _work_is_on() else _t("work_on"),
            "house" if _work_is_on() else "briefcase")
        menu.addItem_(ITEMS["work"])
        # ★ 2026-10-15 第⑤条：范老板「删除菜单栏『工作模式：唤醒键…』小字」。
        #   原来这里挂过一条灰色说明项 `work_note`（"工作模式：唤醒键 ⌘D"），
        #   工作面板里还有一条一模一样的 `wk_note`。两处都删干净了 —— 唤醒键是
        #   「设置一次就不用再管」的东西，常驻一行小字纯属噪音，真要看就打开
        #   「设置唤醒键…」那个二级面板，那里本来就写着当前键。
        ITEMS["sep_leisure_end"] = MI.separatorItem()
        menu.addItem_(ITEMS["sep_leisure_end"])

        ITEMS["settings"] = _action(_t("settings"), b"openSettings:", target, ",")
        ITEMS["about"] = _action(_t("about"), b"aboutApp:", target)
        menu.addItem_(ITEMS["settings"])
        menu.addItem_(ITEMS["about"])
        ITEMS["sep_common"] = MI.separatorItem()
        menu.addItem_(ITEMS["sep_common"])

        # ══════════════════════════════════════════════════════════════════
        # 工作模式那一套（范老板 2026-10-14 要求）
        #
        # 「点菜单栏 → 面板整个换成另一个新面板，只有：设置快捷键按钮、
        #   刷新还原圆钮、返回休闲模式、退出 FXseek」。
        #
        # ★ 做法是**同一个 NSMenu 里放两组 item，靠 setHidden_ 互斥**，而不是
        #   模式一变就 `setMenu_` 换一个。换菜单要重建全部 item，而状态行、
        #   MCP 子菜单这些是带自定义视图的（_strong_item），每次重建都要重新
        #   绑定 target/action，很容易漏掉一处；隐藏只是换个可见性，最稳。
        #   分隔线也得跟着藏 —— 系统只在**同一菜单里连续的**分隔线之间做合并，
        #   留一条孤立的分隔线在顶上会很难看。
        # ══════════════════════════════════════════════════════════════════
        ITEMS["wk_title"] = _title_item(_t("wk_title"),
                                        AppKit.NSColor.tertiaryLabelColor())
        # ★ 范老板 2026-10-15 第⑦条 + m28310（「快捷键是用户自定义啊，不是让你改成
        #   默认几个」）：入口就是这一项，点下去**菜单收起、直接开始录键** ——
        #   既不开设置面板，也不是一份固定候选。实现见 `_hotkey_record()`。
        ITEMS["wk_set"] = _action(_t("wk_set"), b"setWorkKey:", target)
        ITEMS["wk_reset"] = _action(_t("wk_reset"), b"resetWorkKey:", target)
        # 「搜索框多久没人动就自动收」—— 放二级菜单。用勾选态而不是一个数字输入框：
        # 菜单本来就只能点，让用户看到全部候选比让他填一个数猜边界舒服。
        ITEMS["wk_idle"] = MI.alloc().initWithTitle_action_keyEquivalent_(
            _t("wk_idle"), None, "")
        _isub = AppKit.NSMenu.alloc().init()
        for _sec in _IDLE_CHOICES:
            _it = _action(_idle_label(_sec), b"setIdle:", target)
            _it.setRepresentedObject_(_sec)
            _isub.addItem_(_it)
        ITEMS["wk_idle"].setSubmenu_(_isub)
        ITEMS["work_back"] = _style_work_item(
            _action(_t("work_off"), b"toggleWork:", target), _t("work_off"), "house")
        # ★ 2026-10-14 范老板第②条：「菜单栏『打开收工文件夹』删除」。
        #   以前收工文件夹被搬到数据目录藏起来了，菜单栏是唯一入口；现在它就摆在
        #   桌面上（还带红标签），用户直接点桌面那个就行，菜单里再挂一项反而多余。
        for _k in ("wk_title", "wk_set", "wk_reset"):
            menu.addItem_(ITEMS[_k])
        menu.addItem_(ITEMS["wk_idle"])
        ITEMS["sep_wk"] = MI.separatorItem()
        menu.addItem_(ITEMS["sep_wk"])
        menu.addItem_(ITEMS["work_back"])
        ITEMS["sep_wk2"] = MI.separatorItem()
        menu.addItem_(ITEMS["sep_wk2"])

        ITEMS["quit"] = _action(_t("quit"), b"quitApp:", target, "q")
        menu.addItem_(ITEMS["quit"])

        # 两组 item 的名单（_paint 按当前模式互斥显示）
        ITEMS["_leisure"] = [ITEMS["status"], ITEMS["sep_head"], ITEMS["web"],
                             ITEMS["toggle"], ITEMS["sep_mcp"], ITEMS["mcp"],
                             ITEMS["sep_work"], ITEMS["work"],
                             ITEMS["sep_leisure_end"], ITEMS["settings"],
                             ITEMS["about"], ITEMS["sep_common"]]
        ITEMS["_work"] = [ITEMS["wk_title"], ITEMS["wk_set"],
                          ITEMS["wk_reset"], ITEMS["wk_idle"],
                          ITEMS["sep_wk"], ITEMS["work_back"], ITEMS["sep_wk2"]]
        _apply_group(_work_is_on())
        menu.setDelegate_(delegate)
        # ★ 图标老老实实挂 setMenu_。
        #   曾经想改成「左键切面板、右键弹菜单」，结果右键那条在状态栏上根本
        #   不触发，菜单整个弹不出来 —— 比原来还糟。状态栏图标 + NSMenu 是
        #   系统管的行为：点一下开、再点一下收，本来就是要的那个手感。
        it.setMenu_(menu)
        _STATUS["menu"] = menu
        _STATUS["item"] = it
        return it

    item = _make_item()

    # ------------------------------------------------------------ 抓耳挠腮动效
    # ★ 终稿方案（duck/baboon 双验证）：item 一旦排上，setImage_ 快速换独立文件帧
    #   **实时生效**——所以不再用「每 3 秒重建 item」的笨办法（每次重建带一个新
    #   替死鬼，透明占位越堆越多，是用户报「图标间隔不合理」的元凶）。
    #   现在只建一次 item，排上后挂 NSTimer 以 STRIP_SEC 秒/帧循环 setImage_。
    #   NSTimer 必须在主 runloop 跑起来后加进去（_ensure_icon / _heartbeat 经
    #   AppHelper.callLater 在 runloop 内回调，正好满足）；定时器加 common modes，
    #   点开菜单也继续走。
    _SCRATCH = {"frames": None, "i": 0, "timer": None}

    def _item_laid_out(it):
        try:
            # 重建的 item 刚建完 window() 常还是旧的 0 高（系统异步布局），
            # 直接判 0 会把「还没轮到它布局」误判成「排不上」→ 帧永远切不动。
            # 改判：只要 item 有可见按钮窗口（非 None）且不明确是 0×0 就视为
            # 占位成功；真正的「排上」等系统异步补完后 height 会变 30。
            w = it.button().window()
            if w is None:
                return False
            h = w.frame().size.height
            return h > 0 or it.button().isVisible()
        except Exception:
            return False

    def _hide_pending_sacs():
        """把排队中的替死鬼藏掉（终稿只建一次 item，这里收尾用）。"""
        while _PENDING_SACS:
            sac = _PENDING_SACS.pop(0)
            try:
                sac.setVisible_(False)
            except Exception:
                pass

    class _AnimTarget(AppKit.NSObject):
        """NSTimer 心跳目标：主 runloop 里跑，setImage_ 换下一帧。"""

        def tick_(self, timer):
            try:
                fr = _SCRATCH["frames"]
                if not fr or _SCRATCH["timer"] is None:
                    return
                _SCRATCH["i"] = (_SCRATCH["i"] + 1) % len(fr)
                img = fr[_SCRATCH["i"]]
                if img is not None:
                    item.button().setImage_(img)
            except Exception:
                pass

    def _start_anim():
        """排上后启动帧循环（幂等；定时器加进 common modes，拖菜单时也继续走）。"""
        if _SCRATCH["timer"] is not None:
            return
        fr = _SCRATCH["frames"]
        if not fr:
            fr = _baboon_anim_frames()
            _SCRATCH["frames"] = fr
        if len(fr) < 2:
            return
        tgt = _AnimTarget.alloc().init()
        _KEEP.append(tgt)
        t = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            STRIP_SEC, tgt, b"tick:", None, True)
        AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(t, "kCFRunLoopCommonModes")
        _SCRATCH["timer"] = t
        print("[tray] 帧循环动画启动（%d 帧 × %.2fs）" % (len(fr), STRIP_SEC))
        sys.stdout.flush()


    def _stop():
        """收工：图标藏起来，进程退出。

        ★ 退出码要说清楚，因为 launcher 的 _tray_keeper 靠它判断要不要重开：
          app.terminate_() 让进程正常返回 main() → 退出码 **0**，keeper 看到
          非 2 就当「主程序要关了」，从此不再拉起 —— 所以**凡是「不是我们自己想
          关」的原因走到这里，都必须先置 _QUIT["going_away"]=False**（见 _watch
          里那两条调用），让 keeper 知道该重开。
        """
        try:
            item.setVisible_(False)          # removeStatusItem_ 在有些进程里会 SIGTRAP
        except Exception:
            pass
        try:
            app.terminate_(None)
        except Exception:
            os._exit(0)

    def _where():
        try:
            w = item.button().window()
            f = w.frame() if w is not None else None
            return "frame=%s vis=%s" % (f, item.isVisible())
        except Exception as e:
            return "查不到（%s）" % e

    # 探活容忍度：老版本是「3 次（约 12 秒）探不到就自杀」，太敏感了。
    _WATCH_DEAD_WARN = 8      # 8 次（约 32 秒）→ 记一条警告（每次故障只记一条）
    _BEAT_SEC = 4.0           # 主线程图标体检间隔
    # healing：当前有没有 _ensure_icon 重试链在排队（心跳看到就别再插手，
    #            否则两条链会各自再排一个重试 → 重建数量指数级膨胀）
    # tries：  这一轮已经失败了几次；>0 说明图标是「重建后才排上的」，才值得报一句
    _ICON = {"tries": 0, "healing": False, "alive": True, "dropped": 0}

    def _icon_alive():
        """图标此刻还真的挂在菜单栏上吗（按钮窗口高度 > 0）。

        ★ 这台系统（macOS 26）上 NSStatusItem 的按钮窗口会**自己掉下去** ——
        高度变 0、isVisible False，不报错也不提示。所以必须能主动查，
        绝不能「建完就默认它还在」。

        ★ 只能在主线程调（AppKit 不是线程安全的）。后台探活线程一律不碰它，
        改读 _ICON["alive"] 这个快照；真要动 AppKit 的都走主线程心跳
        _heartbeat（或者 AppHelper.callLater 排回主线程）。
        """
        try:
            w = item.button().window()
            ok = bool(w is not None and w.frame().size.height > 0)
        except Exception:
            ok = False
        _ICON["alive"] = ok
        return ok

    def _ensure_icon(why):
        """看一眼图标排上没有；没有就在**本进程内**重建，直到排上为止。

        为什么必须重建而不是退出：这台系统上 NSStatusItem 排不排得上是随机的
        （实测约 1/3 成功，见 _make_item 的探针数据），**换个 item 重试就能救回来**。
        试到第 5 次还不行才 os._exit(2)，交给主程序的 keeper 换个新进程
        （launcher.py 的 _tray_keeper 只认退出码 2，别的码一律当「主程序要关了」）。

        只在主线程跑（经 callLater / 心跳进来）。
        """
        nonlocal item
        _ICON["healing"] = True        # 本函数要接管重建了（心跳那边看到就不再插手）
        if _icon_alive():
            if _ICON["tries"]:
                print("[tray] 图标已排上（%s）" % _where())
                sys.stdout.flush()
            # ★ 不管试了几次都要起动画：`_start_anim` 是幂等的，
            #   而之前这个调用被塞在 `if _ICON["tries"]:` 里面 —— 于是
            #   **一次就排上（tries==0）时动画永远不会启动**，图标就"变静态"了。
            #   排不排得上是随机的（约 1/3 成功），所以时好时坏，很难复现。
            _hide_pending_sacs()          # 真图标排稳了，把替死鬼藏掉（防透明占位）
            try:
                _start_anim()             # 帧循环动画（幂等）
            except Exception:
                import traceback
                traceback.print_exc()
                sys.stdout.flush()
            _ICON["tries"] = 0
            _ICON["healing"] = False
            return
        _ICON["tries"] += 1
        if _ICON["tries"] >= 5:
            print("[tray] 试了 %d 次都排不上，退出让主程序重开一个助手（%s）"
                  % (_ICON["tries"], _where()))
            sys.stdout.flush()
            os._exit(2)
        print("[tray] %s → 还没排上，换一个再试（第 %d 次，%s）"
              % (why, _ICON["tries"], _where()))
        sys.stdout.flush()
        try:
            item.setVisible_(False)
        except Exception:
            pass
        # 替死鬼别堆太多：重建次数多了就把最早的收掉（透明占位会挤开菜单栏间隔）
        while len(_PENDING_SACS) > 12:
            try:
                _PENDING_SACS.pop(0).setVisible_(False)
            except Exception:
                pass
        try:
            item = _make_item()
        except Exception as e:
            print("[tray] 重建失败：%s" % e)
        AppHelper.callLater(2.5, _ensure_icon, why)

    def _heartbeat():
        """主线程图标体检：菜单栏窗口自己掉下去了就**当场**把它建回来。

        ★ 这是用户报「图标过一段时间就消失」最直接的解药：NSStatusItem 的按钮
        窗口会被系统自己回收（高度变 0、不报错），而图标一旦掉下去，这个进程
        就再也没人管了。每 4 秒查一次，掉了就重建 —— 用户基本感觉不到。
        排上之后不再重复打印，避免日志被刷。
        """
        try:
            if _icon_alive():
                _ICON["tries"] = 0
                _ICON["healing"] = False
            elif _ICON["healing"]:
                # ★ 已经有 _ensure_icon 的重试链在排队了（2.5 秒一次），别再叠加。
                # 心跳每 4 秒跑一次，比重试还慢：要是这里也建一个，就会同时有两条
                # 链在飞、每次各自再排一个 2.5 秒后的重试 → 重建数量指数级膨胀，
                # 而每个 item 都要跟系统抢菜单栏名额，越多越排不上（实测见 _make_item）。
                pass
            else:
                _ICON["dropped"] += 1
                print("[tray] 菜单栏图标掉下去了（累计第 %d 次），当场重建"
                      % _ICON["dropped"])
                sys.stdout.flush()
                _ICON["healing"] = True
                _ensure_icon("图标窗口掉下去了")
        except Exception as e:
            print("[tray] 图标体检异常：%s" % e)
            sys.stdout.flush()
        AppHelper.callLater(_BEAT_SEC, _heartbeat)

    def _watch():
        """后台心跳：主程序没了就跟着走；图标掉了就在主线程里把它救回来。

        ★ 这段改过一次（用户报「菜单栏图标过一段时间就消失，找不到图标」）。
        ~/Library/Logs/FXseek.log 里三次「[tray] 服务连着 3 次探不到，退出」的
        **紧邻上文全是 `[app] "GET /health HTTP/1.1" 200`** —— 服务好好的，
        托盘却判它死了。两个错叠在一起：
          A. _http 走了系统代理（本机 127.0.0.1 也被套上代理），偶发失败 → 已修
             （见文件里 _OPENER_DIRECT 的注释）。
          B. 探不到就自杀，且自杀走 _stop() → 退出码 **0**，而 launcher 的
             _tray_keeper 只认 **2**（`if rc != 2: return`）—— 于是「图标自己
             走了就再也没人拉回来」，永久消失。
        现在改成：
          · 服务探不到**绝不自杀**，只打日志（老版本 3 次就自杀，是丢图标的元凶）；
          · 图标窗口自己掉下去由**主线程心跳 _heartbeat** 每 4 秒查一次，当场重建
            （AppKit 不是线程安全的，所以绝不在这个探活线程里碰图标）；
          · 连续 5 次重建仍排不上才 os._exit(2)，交给 keeper 换进程 —— 这是
            keeper 唯一认的「自己认输」退出码。
        """
        dead = 0
        warned = False        # 这一轮「探不通」已经提醒过了吗（防止刷屏）
        while True:
            time.sleep(4.0)
            if args.parent:
                try:
                    os.kill(args.parent, 0)
                except OSError:
                    print("[tray] 主程序（pid %d）不在了，退出" % args.parent)
                    AppHelper.callLater(0.1, _stop)
                    return
            if not STATE["running"]:
                dead = 0
                continue
            try:
                _http(args.port, "/health", timeout=3.0)
                if warned:
                    # 只在「刚才确实报过警」时才记一条恢复；偶发一次探不通
                    # 就又通了的不记 —— 否则日志会被「又活了」刷爆。
                    print("[tray] 服务又活了（连着探了 %d 次之后）" % dead)
                    sys.stdout.flush()
                dead = 0
                warned = False
                continue
            except Exception:
                pass
            dead += 1
            if not warned and dead >= _WATCH_DEAD_WARN:
                warned = True
                print("[tray] 服务连着 %d 次（约 %d 秒）探不到 —— 不自杀，"
                      "老版本就是在这儿把图标弄丢的" % (dead, dead * 4))
                sys.stdout.flush()
            # ★ 这里**绝不自杀**。服务可能是忙：/health 每次都会跑一次
            # `ffmpeg -version`（indexer.py 的 ffmpeg_status，timeout=15），
            # 索引/转写时机器满载，偶尔就超时几十秒。老版本在这条路上直接
            # _stop() → 退出码 0 → keeper 当成「主程序要关了」→ 图标永久消失。
            # 图标自己掉下去由主线程 _heartbeat 每 4 秒查一次、当场重建。
            continue

    signal.signal(signal.SIGTERM, lambda *a: AppHelper.callLater(0.1, _stop))
    threading.Thread(target=_watch, daemon=True, name="tray-watch").start()

    print("[tray] 菜单栏图标已就位（127.0.0.1:%d，pid %d，%s）"
          % (args.port, os.getpid(), _where()))
    sys.stdout.flush()

    # 启动后先排一次图标，然后转入每 4 秒一次的主线程体检（掉了就当场重建）
    def _boot_icon():
        _ensure_icon("启动后首次检查")
        AppHelper.callLater(_BEAT_SEC, _heartbeat)

    AppHelper.callLater(2.5, _boot_icon)
    sys.stdout.flush()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
