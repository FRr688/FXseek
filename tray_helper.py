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
    "hide": "/v1/panel/hide",
    "settings": "/v1/panel/settings",
    "settings_mcp": "/v1/panel/settings-mcp",
    "server_start": "/v1/server/start",
    "server_stop": "/v1/server/stop",
    "quit": "/v1/app/quit",
}

# 进程级状态：菜单每次打开都会按它重画一遍
STATE = {"port": 0, "data_dir": "", "running": True, "mcp": None, "about": None}
# 菜单项引用要一直拿着，否则刷新时找不到它们
ITEMS = {}
_KEEP = []          # 替死鬼图标 + 菜单代理，都得有人引用着
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
        "panel": "打开服务面板",
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
        "panel": "Open app window",
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


def _t(key, *a):
    """取一条文案。缺词条时回落到中文（宁可中文也不要在菜单里露出英文键名）。"""
    s = TXT.get(_lang(), TXT["zh"]).get(key) or TXT["zh"].get(key) or key
    return (s % a) if a else s


def _http(port, path, data=None, method="GET", timeout=2.5):
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=(body if method == "POST" else None),
        headers=({"Content-Type": "application/json"} if method == "POST" else {}),
        method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    version = "1.0.0"
    if info:
        version = info.get("version", "1.0.0")
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
    def _title_item(text, color=None):
        """禁用（点不动）的标题项，可以带颜色 —— 状态行就是靠它高亮的。"""
        mi = MI.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
        mi.setEnabled_(False)
        if color is not None:
            try:
                mi.setAttributedTitle_(AppKit.NSAttributedString.alloc()
                                       .initWithString_attributes_(text, {
                                           AppKit.NSForegroundColorAttributeName: color,
                                           AppKit.NSFontAttributeName:
                                               AppKit.NSFont.menuFontOfSize_(0),
                                       }))
            except Exception:
                pass
        return mi

    def _action(title, sel, target, key=""):
        mi = MI.alloc().initWithTitle_action_keyEquivalent_(title, sel, key)
        mi.setTarget_(target)
        return mi

    def _paint():
        """按当前状态把菜单各项的标题 / 颜色 / 可用性刷一遍。

        文案也在这里重设 —— 语言是设置页那个开关，用户切完下次点开菜单就换过来。
        """
        running = bool(STATE["running"])
        if running:
            _set_title(ITEMS["status"], _t("status_on", STATE["port"]),
                       AppKit.NSColor.systemGreenColor())
        else:
            _set_title(ITEMS["status"], _t("status_off"),
                       AppKit.NSColor.systemOrangeColor())
        ITEMS["toggle"].setTitle_(_t("stop") if running else _t("start"))
        ITEMS["web"].setTitle_(_t("web"))
        ITEMS["mcp"].setTitle_(_t("mcp"))
        ITEMS["panel"].setTitle_(_t("panel"))
        ITEMS["settings"].setTitle_(_t("settings"))
        ITEMS["about"].setTitle_(_t("about"))
        ITEMS["quit"].setTitle_(_t("quit"))
        ITEMS["web"].setEnabled_(running)
        ITEMS["panel"].setEnabled_(running)
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
        sub.addItem_(_title_item(_t("mcp_on") if st.get("enabled") else _t("mcp_off")))
        sub.addItem_(_title_item(_t("mcp_agents", st.get("connected", 0),
                                   st.get("installed", 0))))
        if st.get("skilled") is not None:
            sub.addItem_(_title_item(_t("mcp_skills", st.get("skilled", 0))))
        sub.addItem_(MI.separatorItem())
        for r in (st.get("agents") or [])[:8]:
            name = r.get("name") or r.get("id") or "?"
            if not r.get("installed"):
                mark = _t("mark_noinstall")
            elif r.get("connected"):
                mark = _t("mark_connected")
            else:
                mark = _t("mark_offline")
            sub.addItem_(_title_item("  %s：%s" % (name, mark) if _lang() == "zh"
                                     else "  %s: %s" % (name, mark)))
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

        def showPanel_(self, sender):
            print("[tray] 打开服务面板 → %s" % (_cmd("show")[1],))

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


    def _make_item(frame_idx=0):
        """建一个狒狒菜单栏图标（连菜单一起装好）并返回它。

        ★ 踩了很久的坑：这个系统（macOS 26）上，**一个进程建的第一个
        NSStatusItem 永远不显示** —— 按钮窗口高度 0、isVisible() 直接是 False，
        不报错也不提示；同一进程里第二个起的就正常排上。最小 AppKit 程序验证过：
        连建两个，第一个 vis=False、第二个 vis=True（frame=(725,930,70,30)）。
        所以每次建之前先放一个替死鬼把「第一个」名额占掉。

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
        ITEMS["status"] = _title_item(_t("status_on", args.port),
                                      AppKit.NSColor.systemGreenColor())
        menu.addItem_(ITEMS["status"])
        menu.addItem_(MI.separatorItem())

        ITEMS["web"] = _action(_t("web"), b"openWeb:", target)
        ITEMS["toggle"] = _action(_t("stop"), b"toggleServer:", target)
        menu.addItem_(ITEMS["web"])
        menu.addItem_(ITEMS["toggle"])
        menu.addItem_(MI.separatorItem())          # ← MCP 状态自成一组

        ITEMS["mcp"] = MI.alloc().initWithTitle_action_keyEquivalent_(_t("mcp"), None, "")
        ITEMS["mcp"].setSubmenu_(AppKit.NSMenu.alloc().init())
        menu.addItem_(ITEMS["mcp"])
        menu.addItem_(MI.separatorItem())          # ← 打开服务面板自成一组

        ITEMS["panel"] = _action(_t("panel"), b"showPanel:", target)
        menu.addItem_(ITEMS["panel"])
        menu.addItem_(MI.separatorItem())

        ITEMS["settings"] = _action(_t("settings"), b"openSettings:", target, ",")
        ITEMS["about"] = _action(_t("about"), b"aboutApp:", target)
        menu.addItem_(ITEMS["settings"])
        menu.addItem_(ITEMS["about"])
        menu.addItem_(MI.separatorItem())

        ITEMS["quit"] = _action(_t("quit"), b"quitApp:", target, "q")
        menu.addItem_(ITEMS["quit"])
        menu.setDelegate_(delegate)
        it.setMenu_(menu)
        return it

    item = _make_item()

    # ------------------------------------------------------------ 抓耳挠腮动效
    # ★ 终稿方案（duck/baboon 双验证）：item 一旦排上，setImage_ 快速换独立文件帧
    #   **实时生效**——所以不再用「每 3 秒重建 item」的笨办法（每次重建带一个新
    #   替死鬼，透明占位越堆越多，是用户报「图标间隔不合理」的元凶）。
    #   现在只建一次 item，排上后挂 NSTimer 以 STRIP_SEC 秒/帧循环 setImage_。
    #   NSTimer 必须在主 runloop 跑起来后加进去（_verify 经 AppHelper.callLater
    #   在 runloop 内回调，正好满足）；定时器加 common modes，点开菜单也继续走。
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
        try:
            item.setVisible_(False)          # removeStatusItem_ 在有些进程里会 SIGTRAP
        except Exception:
            pass
        try:
            app.terminate_(None)
        except Exception:
            os._exit(0)

    def _watch():
        """主程序没了（或被强杀）就跟着走，别留个孤零零的图标点不动。

        ★ 服务是我们自己停的时候，_alive 当然探不到 —— 那不算异常，接着活着，
        用户随时能点「启动服务器」。所以这里用 STATE["running"] 区分一下。
        """
        dead = 0
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
                _http(args.port, "/health", timeout=2.0)
                dead = 0
                continue
            except Exception:
                pass
            dead += 1
            if dead >= 3:
                print("[tray] 服务连着 3 次探不到，退出")
                AppHelper.callLater(0.1, _stop)
                return

    signal.signal(signal.SIGTERM, lambda *a: AppHelper.callLater(0.1, _stop))
    threading.Thread(target=_watch, daemon=True, name="tray-watch").start()

    def _where():
        try:
            w = item.button().window()
            f = w.frame() if w is not None else None
            return "frame=%s vis=%s" % (f, item.isVisible())
        except Exception as e:
            return "查不到（%s）" % e

    print("[tray] 菜单栏图标已就位（127.0.0.1:%d，pid %d，%s）"
          % (args.port, os.getpid(), _where()))
    sys.stdout.flush()

    _tries = [0]

    def _verify():
        """看一眼排上没有；没有就**再建一个**（每次新建的都会变成「最后一个」，
        而系统只显示最后建的那个——这就是为什么重建能救回来）。

        试到第 5 次还不行就退出，让主程序重开一个助手进程：换了进程还有一次
        机会（这毛病看着跟进程当时的状态有关，光在同一个进程里重试不保险）。
        """
        nonlocal item
        try:
            w = item.button().window()
            h = w.frame().size.height if w is not None else 0
        except Exception:
            h = 0
        if h > 0:
            print("[tray] 图标已排上（%s）" % _where())
            sys.stdout.flush()
            # 真图标排稳了，把替死鬼藏掉（防透明占位挤开间隔，见 _hide_pending_sacs）
            _hide_pending_sacs()
            # 图标稳定显示后启动连续帧循环动画（幂等）
            try:
                _start_anim()
            except Exception:
                import traceback
                traceback.print_exc()
                sys.stdout.flush()
            return
        _tries[0] += 1
        if _tries[0] >= 5:
            print("[tray] 试了 %d 次都排不上，退出让主程序重开一个助手（%s）"
                  % (_tries[0], _where()))
            sys.stdout.flush()
            os._exit(2)
        print("[tray] 还没排上，换一个再试（第 %d 次，%s）" % (_tries[0], _where()))
        sys.stdout.flush()
        try:
            item.setVisible_(False)
        except Exception:
            pass
        try:
            item = _make_item()
        except Exception as e:
            print("[tray] 重建失败：%s" % e)
        AppHelper.callLater(2.5, _verify)

    AppHelper.callLater(2.5, _verify)
    sys.stdout.flush()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
