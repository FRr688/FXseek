#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""全局热键（系统级，应用在后台也收得到）—— 只在 macOS 上用。

## 为什么不走 pyobjc 的 Carbon 模块

`pip install pyobjc-framework-Carbon` 装出来的 12.2.2 是个**空壳**：包目录里只有
`__init__.py` 和 `_metadata.py`，`Carbon.CarbonEvents` / `Carbon._CarbonEvt` 全都
ModuleNotFoundError。装多少次都没用。

## 实际用的办法

ctypes 直接开 `/System/Library/Frameworks/Carbon.framework/Carbon`，调两个老 API：

    InstallEventHandler(GetApplicationEventTarget(), cb, 1, &kEventHotKeyPressed, ...)
    RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &ref)

这是 Carbon 时代的「注册全局热键」标准做法，今天在 macOS 上依然可用（系统自己要
用，不会拆）。**必须在应用事件循环跑起来之后调**，否则注册返回 0 也收不到事件。

## 两个很容易踩死的细节

1. 回调**必须**是 `CFUNCTYPE(...)` 的**实例**。直接传 Python 函数会
   `ctypes.ArgumentError: argument 2: TypeError: expected CFunctionType instance
   instead of function`。而且实例必须由本模块**存住引用**——交给 ctypes 之后
   Python 那边没人引用它，下一轮 GC 就崩。
2. `RegisterEventHotKey` 注册成功只说明**系统收下了这个组合键**，不代表按下去
   一定回调。这个没法自动测：`CGEventPost` 合成的按键（kCGHIDEventTap /
   kCGSessionEventTap 都试过）**一次都打不中** Carbon 热键——它是 WindowServer
   在更底层匹配的，合成事件绕不过去。所以「热键到底响不响」只能请真人按一次。
   （权限不是问题：`CGPreflightPostEventAccess()` 在这台机器上是 True。）
"""

import ctypes
import ctypes.util
import sys
import threading

# --------------------------------------------------------------------------
# Carbon 常量
# --------------------------------------------------------------------------

# 修饰键。注意 Carbon 这几个值和现代 NSEvent 的 NSEventModifierFlag* 不一样，
# 也和各处文章里抄来抄去的值容易混，别改。
MOD_CMD   = 1 << 8    # 256  ⌘
MOD_SHIFT = 1 << 9    # 512  ⇧
MOD_ALT   = 1 << 11   # 2048 ⌥
MOD_CTRL  = 1 << 12   # 4096 ⌃

_MOD_BY_NAME = {
    "cmd": MOD_CMD, "command": MOD_CMD, "⌘": MOD_CMD, "meta": MOD_CMD, "super": MOD_CMD,
    "shift": MOD_SHIFT, "⇧": MOD_SHIFT,
    "alt": MOD_ALT, "option": MOD_ALT, "opt": MOD_ALT, "⌥": MOD_ALT,
    "ctrl": MOD_CTRL, "control": MOD_CTRL, "⌃": MOD_CTRL,
}

# 显示用符号：Apple 的固定次序是 ⌃⌥⇧⌘，和设置页里存字符串的次序一致。
_SYMBOL = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}

# kVK_* 虚拟键码（来自 Carbon 的 Events.h）。
KEYCODE = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35,
    "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# 反向表：给「录制唤醒键」用（按下一个键 → keycode → 名字）。
# 同一个码有多个名字时（return/enter、esc/escape）取**先出现的那个**，
# 这样显示出来是 "return" 而不是 "enter"，和 Carbon 文档叫法一致。
NAME_BY_KEYCODE = {}
for _n, _c in KEYCODE.items():
    NAME_BY_KEYCODE.setdefault(_c, _n)

# 这些键**单独**当热键没意义：不带修饰键注册会把全系统这个键吞掉。
# 前半段是字符（`a`、`1`、空格…），后半段是**键名**——`space` / `tab` / `esc`
# 这些在 KEYCODE 表里是单词，不是单字符，只列字符表会漏掉它们，
# 结果就是「裸空格」也能注册成功、全系统的空格全没了。
_WEAK_ALONE = (set("abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./ ") |
               {"space", "tab", "return", "enter", "delete", "backspace",
                "esc", "escape", "forwarddelete"})

# 事件类型：kEventClassKeyboard / kEventHotKeyPressed
# 'keyb' = 0x6B657962，kEventHotKeyPressed = 5
#
# ★ 这两个必须是真的 ctypes.Structure。写成 `[("eventClass", c_uint32), ...]`
#   这种**字段清单列表**看着像，但 `POINTER(那个列表)` 会直接抛
#   `TypeError: unhashable type: 'list'` —— ctypes 要的是类型不是清单，
#   而且这个错发生在 CDLL 装载之后的 argtypes 设置里，报错位置离真正的原因很远。
class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_EVENT_CLASS_KEYBOARD = 0x6B657962
_EVENT_HOTKEY_PRESSED = 5

_SIG = 0x4658736B          # 'FXsk'，认得出来源就行，值本身不重要

_OSStatus = ctypes.c_int32
_HANDLER = ctypes.CFUNCTYPE(_OSStatus, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


# --------------------------------------------------------------------------
# 解析 / 显示
# --------------------------------------------------------------------------

def parse(combo):
    """`"alt+cmd+space"` → `(modifiers, keycode)`；认不出返回 `None`。

    `combo` 假定已经过 app.py 的 `_work_norm_hotkey()` 规范化（修饰键在前、
    小写、`+` 分隔）。这里仍然宽容地再归一一次，免得单独用这个模块时踩坑。
    """
    raw = (combo or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    mods = 0
    key = None
    for part in raw.split("+"):
        if not part:
            continue
        if part in _MOD_BY_NAME:
            mods |= _MOD_BY_NAME[part]
        elif key is None:
            key = part
        else:
            return None            # 两个主键，不合法
    if key is None or key not in KEYCODE:
        return None
    # ★ 范老板 2026-10-15：「快捷键不要做任何限制」——单键也放行。
    return mods, KEYCODE[key]


def display(combo):
    """`"alt+cmd+space"` → `"⌥⌘Space"`（Apple 官方写法：⌃⌥⇧⌘ 在前，主键首字母大写）。"""
    raw = (combo or "").strip().lower().replace(" ", "")
    if not raw:
        return ""
    order = ("ctrl", "alt", "shift", "cmd")
    seen, key = [], None
    for part in raw.split("+"):
        canon = {"command": "cmd", "option": "alt", "control": "ctrl", "meta": "cmd",
                 "super": "cmd", "opt": "alt"}.get(part, part)
        if canon in order:
            # 重复的修饰键直接丢掉，**不能**落到下面的 `elif key is None` ——
            # 那样 `cmd+cmd+space` 会把第二个 cmd 当成主键，显示成「⌘Cmd」。
            if canon not in seen:
                seen.append(canon)
        elif key is None:
            key = part
    out = "".join(_SYMBOL[m] for m in order if m in seen)
    if key:
        out += key.capitalize() if len(key) > 1 else key.upper()
    return out


def from_keycode(keycode, flags=0):
    """一次原生按键 → `"alt+cmd+space"`，给设置页的「录制」用；认不出返回 ""。

    `flags` 是 NSEvent 的 modifierFlags（`NSEventModifierFlagCommand` = 1<<20
    等）。这里只认设备无关的那几位，忽略 Fn / CapsLock / 左右区分等杂位。
    """
    name = NAME_BY_KEYCODE.get(int(keycode))
    if not name:
        return ""
    mods = []
    if flags & (1 << 18):      # NSEventModifierFlagControl
        mods.append("ctrl")
    if flags & (1 << 19):      # NSEventModifierFlagOption
        mods.append("alt")
    if flags & (1 << 17):      # NSEventModifierFlagShift
        mods.append("shift")
    if flags & (1 << 20):      # NSEventModifierFlagCommand
        mods.append("cmd")
    return "+".join(mods + [name])


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

class _Carbon:
    """把 Carbon 的几个符号捞出来。失败就是这台机器用不了（非 macOS / 库没了）。"""

    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("全局热键只在 macOS 上实现")
        path = ctypes.util.find_library("Carbon") or \
            "/System/Library/Frameworks/Carbon.framework/Carbon"
        self.lib = ctypes.CDLL(path)
        L = self.lib
        L.GetApplicationEventTarget.restype = ctypes.c_void_p
        L.GetApplicationEventTarget.argtypes = []
        L.InstallEventHandler.restype = _OSStatus
        L.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, ctypes.c_void_p]
        L.RegisterEventHotKey.restype = _OSStatus
        L.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        L.UnregisterEventHotKey.restype = _OSStatus
        L.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        L.RemoveEventHandler.restype = _OSStatus
        L.RemoveEventHandler.argtypes = [ctypes.c_void_p]


_CARBON = None
_CARBON_ERR = ""


def available():
    """能不能用（只探一次，结果缓存）。"""
    global _CARBON, _CARBON_ERR
    if _CARBON is not None:
        return True
    if _CARBON_ERR:
        return False
    try:
        _CARBON = _Carbon()
        return True
    except Exception as e:                                  # noqa: BLE001
        _CARBON_ERR = "%s: %s" % (type(e).__name__, e)
        print("[hotkey] 用不了：%s" % _CARBON_ERR)
        return False


class _Reg:
    """一次注册的全部原生句柄。**必须活到卸载为止**，见模块开头第 1 条。"""

    def __init__(self):
        self.cb = None        # CFUNCTYPE 实例，防 GC
        self.handler = None   # EventHandlerRef
        self.hotkey = None    # EventHotKeyRef
        self.combo = ""
        self.lock = threading.RLock()


_REG = _Reg()


def _make_callback(fn):
    """把普通 Python 函数包成 Carbon 能收的回调。

    ★ 返回值**必须**被调用方存住（放进 _REG.cb）。就地传进去的话，函数一返回
      Python 侧就没有引用了，之后回调时是一段已释放的内存 —— 表现是随机崩溃，
      而且崩在 Carbon 内部，堆栈里看不到我们自己的代码，极难查。
    """
    def _cb(_call_ref, _event, _user):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            print("[hotkey] 回调里出错：%s: %s" % (type(e).__name__, e))
        return 0        # noErr：不返回 0 的话系统会认为没处理，可能再弹一声「哔」
    return _HANDLER(_cb)


def register(combo, callback):
    """注册全局热键，成功返回 `(True, "")`，失败返回 `(False, 原因)`。

    同一时刻只保留**一个**热键（工作模式只需要叫醒键）。重复调用会把上一个解掉。
    """
    if not available():
        return False, (_CARBON_ERR or "这台机器不支持")
    parsed = parse(combo)
    if not parsed:
        return False, "这个组合键不能用（至少要有一个修饰键）"
    mods, keycode = parsed
    with _REG.lock:
        unregister()
        # ★ 先把回调实例建出来拿在手里，再传进 InstallEventHandler。
        #   如果写成 `InstallEventHandler(..., _make_callback(callback), ...)`，
        #   那个实例在函数返回后就没引用了 —— 之后按热键是一段已释放内存，
        #   崩在 Carbon 内部、堆栈里看不到我们的代码。所以这里绝不能内联。
        cb = _make_callback(callback)
        try:
            spec = (_EventTypeSpec * 1)(_EventTypeSpec(_EVENT_CLASS_KEYBOARD,
                                                 _EVENT_HOTKEY_PRESSED))
            handler = ctypes.c_void_p()
            st = _CARBON.lib.InstallEventHandler(
                _CARBON.lib.GetApplicationEventTarget(),
                cb, 1,
                ctypes.cast(spec, ctypes.POINTER(_EventTypeSpec)),
                None, ctypes.byref(handler))
            if st != 0:
                return False, "装事件处理器失败（OSStatus=%d）" % st
            ref = ctypes.c_void_p()
            hk_id = _EventHotKeyID(_SIG, 1)
            st = _CARBON.lib.RegisterEventHotKey(keycode, mods, hk_id,
                                                 _CARBON.lib.GetApplicationEventTarget(),
                                                 0, ctypes.byref(ref))
            if st != 0 or not ref.value:
                # -9868 = eventHotKeyExistsErr：这个组合已经被**别的应用**
                # （或本应用的上一次注册）占了。提示得说人话。
                why = ("这个组合键已经被别的程序占用了" if st == -9868
                       else "注册失败（OSStatus=%d）" % st)
                if handler.value:
                    # 注册没成，处理器得撤掉，否则多挂一个永远不响的
                    try:
                        _CARBON.lib.RemoveEventHandler(handler)
                    except Exception:                       # noqa: BLE001
                        pass
                return False, why
            # ★ 到这里才写回共享状态。写早了，中途失败会把「上一组回调引用」
            #   覆盖掉 —— 那个 CFUNCTYPE 一被 GC，还在生效的旧热键就会崩。
            _REG.cb = cb
            _REG.handler = handler
            _REG.hotkey = ref
            _REG.combo = combo
            print("[hotkey] 已注册 %s（%s，keycode=%d，mods=%d）"
                  % (combo, display(combo), keycode, mods))
            return True, ""
        except Exception as e:                              # noqa: BLE001
            print("[hotkey] 注册异常：%s: %s" % (type(e).__name__, e))
            return False, "%s: %s" % (type(e).__name__, e)


def unregister():
    """解掉当前热键。没注册时是空操作（可以随便调）。"""
    with _REG.lock:
        if _REG.hotkey is not None:
            try:
                _CARBON.lib.UnregisterEventHotKey(_REG.hotkey)
            except Exception as e:                          # noqa: BLE001
                print("[hotkey] 卸载热键失败：%s" % e)
        if _REG.handler is not None:
            try:
                _CARBON.lib.RemoveEventHandler(_REG.handler)
            except Exception as e:                          # noqa: BLE001
                print("[hotkey] 移除处理器失败：%s" % e)
        _REG.hotkey = None
        _REG.handler = None
        _REG.cb = None
        _REG.combo = ""


def current():
    """当前已注册的组合键（没注册返回 ""）。"""
    return _REG.combo
