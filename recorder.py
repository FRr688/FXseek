# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""桌面版原生录音 —— 绕开 WKWebView 的 getUserMedia。

为什么需要它：实测（同一份代码、同一个 .app，只换启动方式）

    终端里跑        → navigator.mediaDevices 存在，getUserMedia 可用
    双击/访达/程序坞 → navigator.mediaDevices 是 undefined

也就是说 macOS 不给「从访达启动的应用」开放 WebKit 的录音能力，
前端再怎么改也拿不到麦克风。所以桌面版改走原生：用 AVFoundation 的
AVAudioRecorder 录成 m4a，再把 base64 交回前端，喂给已有的
/v1/voice-search 链路（那条链路本来就认 data:audio/mp4;base64），
前端表现得跟浏览器录音一模一样。

权限：第一次录音会弹系统授权框（Info.plist 里必须有
NSMicrophoneUsageDescription，build_app.sh 已经加好了）。
授权状态可以用 status() 查：0 未决定 / 1 受限 / 2 拒绝 / 3 已授权。
"""

import base64
import os
import threading
import time

_LOCK = threading.Lock()
_state = {
    "rec": None,        # AVAudioRecorder，必须留着引用，否则会被 GC 掉
    "path": "",
    "t0": 0.0,
    "smooth": 0.0,      # 平滑后的输入电平 0..1（给前端波形用）
}


def _auth_status() -> int:
    """0 未决定 / 1 受限 / 2 拒绝 / 3 已授权"""
    import AVFoundation as AV
    return int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio))


def status() -> dict:
    """当前麦克风授权状态。前端启动时问一次，好提前提示用户。"""
    try:
        st = _auth_status()
    except Exception as e:
        return {"available": False, "error": str(e)}
    return {
        "available": True,
        "status": st,
        "granted": st == 3,
        "denied": st == 2 or st == 1,
        "undetermined": st == 0,
    }


def _request_on_main_thread() -> bool:
    """在主线程里发起 TCC 授权请求。

    ★ 必须走主线程：这个请求由 HTTP 处理线程发起时，macOS 只会把状态
    切成「已询问」却**不弹窗**（实测：status 停在 0、前端只看到一句错误
    提示，用户以为「点了麦克风没反应」）。launcher 的主线程跑着 AppKit
    事件循环，callAfter 过去才会真正弹框。
    """
    import AVFoundation as AV

    done = threading.Event()
    box = {"ok": False}

    def _fire():
        def _cb(ok):
            box["ok"] = bool(ok)
            print("[recorder] 麦克风授权：%s" % ("允许" if ok else "拒绝"))
            done.set()
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, _cb)

    queued = False
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_fire)      # 主线程执行
        queued = True
    except Exception as e:
        print("[recorder] 主线程排队失败，直接请求：%s" % e)

    if queued and done.wait(30.0):
        return bool(box["ok"])          # 用户点完了：返回「允许 / 拒绝」
    if not queued:
        try:
            _fire()
        except Exception as e:
            print("[recorder] 直接请求授权失败：%s" % e)
            return False
        done.wait(30.0)
        return bool(box["ok"])
    # 排了队但 30 秒没等到回调（runloop 没跑起来之类）→ 退回当前线程再试一次
    print("[recorder] 主线程请求超时，退回当前线程重试")
    try:
        _fire()
    except Exception:
        return False
    done.wait(5.0)
    return bool(box["ok"])


def request_access(wait: bool = True) -> dict:
    """触发系统麦克风授权弹窗（只在「未决定」时真的弹），并等用户点完。

    wait=True：等用户点「允许/不允许」（最多 ~30 秒），返回最新状态；
    wait=False：发出去就返回（老行为）。
    """
    try:
        st = _auth_status()
    except Exception as e:
        return {"available": False, "error": str(e)}
    if st != 0:
        return status()                 # 已经决定过了：系统不会再弹
    if not wait:
        # 不等结果：后台线程把请求发出去就返回（老行为，备用）
        threading.Thread(target=_request_on_main_thread, daemon=True).start()
        return status()
    ok = _request_on_main_thread()
    out = status()
    out["answered"] = True
    out["allowed"] = bool(ok) or out.get("status") == 3
    return out


def open_settings() -> dict:
    """打开「系统设置 → 隐私与安全性 → 麦克风」，让用户自己去勾。"""
    try:
        import AppKit
        url = ("x-apple.systempreferences:com.apple.preference.security"
               "?Privacy_Microphone")
        AppKit.NSWorkspace.sharedWorkspace().openURL_(
            AppKit.NSURL.URLWithString_(url))
        return {"ok": True}
    except Exception as e:
        return {"error": "打开系统设置失败：%s" % e}


def start(dst_dir: str) -> dict:
    """开始录音到 <dst_dir>/mic-<时间戳>.m4a。已在录就先停掉上一次。"""
    import AVFoundation as AV
    import Foundation

    st = _auth_status()
    if st == 0:
        # 还没问过：把系统弹窗叫出来并**等用户点完**，允许了就直接开始录
        # （老版本这里直接返回错误、要用户再点一次麦克风，用户以为「没反应」）
        r = request_access(wait=True)
        st = int(r.get("status", _auth_status()))
        if st != 3:
            return {"error": "需要麦克风权限才能录音：请在系统弹窗里点「允许」，"
                             "或到「系统设置 → 隐私与安全性 → 麦克风」里允许 FXseek",
                    "status": st, "permission": True}
        print("[recorder] 授权通过，继续开始录音")
    if st != 3:
        return {"error": "麦克风权限被拒绝：请到「系统设置 → 隐私与安全性 → 麦克风」里允许 FXseek",
                "status": st, "permission": True}

    with _LOCK:
        if _state["rec"] is not None:
            _stop_locked(delete=True)

        os.makedirs(dst_dir, exist_ok=True)
        path = os.path.join(dst_dir, "mic-%d.m4a" % int(time.time()))
        url = Foundation.NSURL.fileURLWithPath_(path)
        settings = {
            AV.AVFormatIDKey: int(AV.kAudioFormatMPEG4AAC),
            AV.AVSampleRateKey: 44100.0,
            AV.AVNumberOfChannelsKey: 1,
            AV.AVEncoderAudioQualityKey: int(AV.AVAudioQualityHigh),
        }
        rec = AV.AVAudioRecorder.alloc().initWithURL_settings_error_(url, settings, None)
        # pyobjc 对 NSError** 出参会返回 (对象, error) 元组，这里两种形态都兜住
        if isinstance(rec, tuple):
            rec, err = rec
            if err is not None:
                return {"error": "初始化录音失败：%s" % err}
        if rec is None:
            return {"error": "初始化录音失败（系统没给出原因）"}
        if not rec.record():
            return {"error": "录音启动失败（麦克风可能被别的程序占用）"}

        # 电平表：前端波形要「有声才动，静音平线」，靠 averagePowerForChannel_ 喂数据
        try:
            rec.setMeteringEnabled_(True)
        except Exception as e:
            print("[recorder] 电平表开不了（不影响录音）：%s" % e)

        _state.update({"rec": rec, "path": path, "t0": time.time(), "smooth": 0.0})
        print("[recorder] 开始录音 → %s" % path)
        return {"ok": True, "path": path}


def level() -> dict:
    """当前输入电平 0..1（已平滑）。没在录音时返回 0。

    AVAudioRecorder 的 averagePower 是 dBFS。实测这台机器（2024 Mac mini 内置麦）：
    安静房间底噪 avg ≈ -35dB，正常说话 -20..-5dB。所以静音线画在 -32dB
    （底噪之上留 3dB 余量），-12dB 以上算满格，中间线性 —— 静音时干净归零。
    一阶低通平滑（新值占 0.55）滤掉采样间隙的抖动，波形看起来是连续起伏的。
    """
    with _LOCK:
        rec = _state.get("rec")
        if rec is None:
            _state["smooth"] = 0.0
            return {"ok": True, "level": 0.0}
        try:
            rec.updateMeters()
            db = float(rec.averagePowerForChannel_(0))
        except Exception:
            db = -60.0
        if db >= -12.0:
            v = 1.0
        elif db <= -32.0:
            v = 0.0
        else:
            v = (-12.0 - db) / 20.0
        _state["smooth"] = _state.get("smooth", 0.0) * 0.45 + v * 0.55
        return {"ok": True, "level": round(_state["smooth"], 4)}


def _stop_locked(delete: bool = False) -> str:
    """停掉当前录音（调用方要自己持锁）。返回文件路径。"""
    rec = _state.get("rec")
    path = _state.get("path") or ""
    if rec is not None:
        try:
            rec.stop()
        except Exception as e:
            print("[recorder] 停止录音出错：%s" % e)
    _state.update({"rec": None, "path": "", "t0": 0.0, "smooth": 0.0})
    if delete and path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return ""
    return path


def stop() -> dict:
    """停止录音，返回 base64 data URL（直接喂 /v1/voice-search）。"""
    with _LOCK:
        t0 = _state.get("t0") or 0.0
        path = _stop_locked()
    seconds = round(max(0.0, time.time() - t0), 2) if t0 else 0.0

    if not path or not os.path.isfile(path):
        return {"error": "没有正在进行的录音"}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"error": "读取录音文件失败：%s" % e}
    finally:
        try:
            os.remove(path)      # 录完就删：它是临时的，检索用的字节都在内存里
        except OSError:
            pass

    if len(raw) < 800:
        return {"error": "录音太短，没有内容", "bytes": len(raw)}
    b64 = base64.b64encode(raw).decode("ascii")
    print("[recorder] 录音结束：%.1f 秒 / %d 字节" % (seconds, len(raw)))
    return {"ok": True, "audio": "data:audio/mp4;base64," + b64,
            "seconds": seconds, "bytes": len(raw)}


def cancel() -> dict:
    """丢弃这次录音。"""
    with _LOCK:
        path = _stop_locked(delete=True)
    return {"ok": True}
