#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
语音意图识别（说话 vs 哼唱）—— 纯声学特征判断，不需要额外模型。

用途：麦克风录完音后判断用户想干什么
  - 说话  → 交给 ASR 转文字，再走语义检索（等于用嘴代替键盘）
  - 哼唱  → 走 Chromaprint 指纹，只在音频/视频里找那一段旋律

判据（同一段 PCM，16kHz 单声道，全部用 numpy 算）：
  1. long_stable 长稳定音高占比 —— 哼唱会持续保持同一个音高（>=300ms 内 F0 波动 <3%），
     说话则在不断滑动语调。这是最强的判据。
  2. zcr 过零率 / hf 高频能量占比 —— 说话有咝音、爆破音，高频丰富；哼唱几乎全是谐波。
  3. mod_depth 振幅调制深度 —— 说话按音节起伏（2~10Hz），哼唱气息平稳。
  4. voiced 浊音占比 —— 哼唱几乎全程发声，说话词间有停顿。
依赖：numpy（本机已有）+ ffmpeg（复用 indexer 里解析好的路径）。
"""
import os
import subprocess
import tempfile

SR = 16000          # 统一重采样到 16k 单声道
FRAME_MS = 40       # 分析帧长
HOP_MS = 20         # 帧移
F0_MIN, F0_MAX = 70.0, 500.0     # 人声基频搜索范围（Hz）

# 判定阈值：hum_score 超过该值认为是哼唱
HUM_THRESHOLD = 0.46

# 权重：以「长稳定音高 + 音节调制」为主判据（实测这两项对说话/哼唱区分度最大），
# 频谱类特征（过零/高频/谱平坦）权重压低——它们对干净录音两边都接近满分，
# 只有在真实麦克风录制（有咝音、底噪）时才起微调作用。
W_STABLE, W_MOD, W_VOICED = 0.45, 0.25, 0.12
W_ZCR, W_HF, W_FLAT = 0.06, 0.06, 0.06


# ---------------------------------------------------------------- 解码
def _decode(path: str, sr: int = SR):
    """用 ffmpeg 解码成 float32 单声道波形。失败返回 None。"""
    import numpy as np
    import indexer as ix
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "a.raw")
        try:
            subprocess.run([ix.FFMPEG, "-v", "error", "-i", path,
                            "-ac", "1", "-ar", str(sr), "-f", "s16le", "-y", raw],
                           capture_output=True, timeout=120)
        except Exception:
            return None
        if not os.path.exists(raw) or os.path.getsize(raw) < 3200:
            return None
        with open(raw, "rb") as f:
            data = f.read()
    if len(data) < 2:
        return None
    a = np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2").astype(np.float32)
    return a / 32768.0


def _frames(x, fl: int, hop: int):
    """把波形切成帧矩阵 (n_frames, fl)。不足一帧返回 None。"""
    import numpy as np
    n = len(x)
    if n < fl:
        return None
    count = 1 + (n - fl) // hop
    idx = np.arange(fl)[None, :] + hop * np.arange(count)[:, None]
    return x[idx]


# ---------------------------------------------------------------- 特征
def _f0_and_voiced(frames, sr: int):
    """逐帧自相关求基频与浊音置信度。返回 (f0 数组, conf 数组)。

    conf = 归一化自相关峰高，越接近 1 越像周期信号（浊音/哼唱）。
    """
    import numpy as np
    n, fl = frames.shape
    win = np.hanning(fl).astype(np.float32)
    fw = frames * win
    # 功率谱 → 自相关（维纳-辛钦）
    spec = np.fft.rfft(fw, n=2 * fl, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, n=2 * fl, axis=1)[:, :fl]
    r0 = ac[:, :1].copy()
    r0[r0 < 1e-9] = 1e-9
    acn = ac / r0
    lag_min = max(2, int(sr / F0_MAX))
    lag_max = min(fl - 2, int(sr / F0_MIN))
    seg = acn[:, lag_min:lag_max]
    if seg.shape[1] < 3:
        return np.zeros(n), np.zeros(n)
    k = np.argmax(seg, axis=1)
    conf = seg[np.arange(n), k]
    # 抛物线插值提高精度
    kk = k + lag_min
    y0 = acn[np.arange(n), np.clip(kk - 1, 0, fl - 1)]
    y1 = acn[np.arange(n), np.clip(kk, 0, fl - 1)]
    y2 = acn[np.arange(n), np.clip(kk + 1, 0, fl - 1)]
    denom = (y0 - 2 * y1 + y2)
    denom[np.abs(denom) < 1e-9] = 1e-9
    delta = np.clip(0.5 * (y0 - y2) / denom, -1.0, 1.0)
    lag = kk + delta
    f0 = sr / np.maximum(lag, 1.0)
    return f0.astype(np.float32), np.clip(conf, 0.0, 1.0).astype(np.float32)


def _spectral(frames):
    """过零率、高频能量占比、谱平坦度（逐帧）。"""
    import numpy as np
    n, fl = frames.shape
    # 过零率
    s = np.sign(frames)
    s[s == 0] = 1
    zcr = np.mean(np.abs(np.diff(s, axis=1)) > 0, axis=1).astype(np.float32)
    win = np.hanning(fl).astype(np.float32)
    mag = np.abs(np.fft.rfft(frames * win, axis=1)) + 1e-10
    freqs = np.fft.rfftfreq(fl, 1.0 / SR)
    power = mag ** 2
    total = power.sum(axis=1) + 1e-12
    hf = power[:, freqs >= 2000].sum(axis=1) / total
    # 谱平坦度：几何均值 / 算术均值（越接近 1 越像噪声，越小越像纯音）
    logm = np.mean(np.log(power), axis=1)
    arim = np.mean(power, axis=1) + 1e-12
    flat = np.exp(logm) / arim
    return zcr.astype(np.float32), hf.astype(np.float32), flat.astype(np.float32)


def _mod_depth(rms, hop: int):
    """振幅包络在 2~10Hz（音节速率）的调制强度，0~1。

    说话按音节一强一弱地起伏，哼唱气息平稳 → 该值低。
    """
    import numpy as np
    env = rms.astype(np.float64)
    if len(env) < 8:
        return 0.0
    env = env - env.mean()
    if np.allclose(env, 0):
        return 0.0
    spec = np.abs(np.fft.rfft(env * np.hanning(len(env))))
    freqs = np.fft.rfftfreq(len(env), hop / float(SR))
    band = (freqs >= 2.0) & (freqs <= 10.0)
    total = spec[1:].sum() + 1e-12
    low = spec[band].sum()
    return float(min(1.0, low / total))


def _long_stable_ratio(f0, voiced, min_run=15, tol=0.03):
    """长稳定音高占比：F0 在连续 >=min_run 帧（约 300ms）内相对波动 < tol 的帧占比。

    哼唱会「拖长音」，说话几乎不会。
    """
    import numpy as np
    n = len(f0)
    if n == 0:
        return 0.0
    good = np.zeros(n, dtype=bool)
    i = 0
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and voiced[j + 1]:
            j += 1
        seg = f0[i:j + 1]
        m = float(np.median(seg))
        if m > 0:
            # 在浊音段内找满足容差的游程
            ok = np.abs(seg - m) / m <= tol
            k = 0
            while k < len(ok):
                if not ok[k]:
                    k += 1
                    continue
                t = k
                while t + 1 < len(ok) and ok[t + 1]:
                    t += 1
                if (t - k + 1) >= min_run:
                    good[i + k:i + t + 1] = True
                k = t + 1
        i = j + 1
    return float(good.sum()) / float(n)


# ---------------------------------------------------------------- 对外
def analyze(path: str, verbose: bool = False) -> dict:
    """分析音频，判断是「说话」还是「哼唱」。

    返回 {ok, intent:'speech'|'hum', hum_score, confidence, duration_sec,
          features:{...}, message}
    判断不了时 ok=False，调用方应给出友好提示。
    """
    import numpy as np
    if not path or not os.path.exists(path):
        return {"ok": False, "message": "音频文件不存在"}

    x = _decode(path)
    if x is None or len(x) < SR * 0.8:
        return {"ok": False, "message": "录音太短或无法解码（至少需要 0.8 秒）"}

    duration = len(x) / float(SR)
    # 太长只取前 30 秒，够判断意图了
    if len(x) > SR * 30:
        x = x[: SR * 30]

    fl = int(SR * FRAME_MS / 1000)
    hop = int(SR * HOP_MS / 1000)
    frames = _frames(x, fl, hop)
    if frames is None:
        return {"ok": False, "message": "音频太短"}

    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    peak = float(np.percentile(rms, 95)) + 1e-9
    # 能量门限：相对峰值 -26dB，避开底噪
    active = rms > (peak * 0.05)

    f0, conf = _f0_and_voiced(frames, SR)
    zcr, hf, flat = _spectral(frames)

    voiced = active & (conf >= 0.45) & (f0 >= F0_MIN) & (f0 <= F0_MAX)
    voiced_ratio = float(voiced.sum()) / float(len(voiced))

    long_stable = _long_stable_ratio(f0, voiced)
    mod_depth = _mod_depth(rms, hop)

    if voiced.sum() >= 3:
        zcr_v = float(np.median(zcr[voiced]))
        hf_v = float(np.median(hf[voiced]))
        flat_v = float(np.median(flat[voiced]))
    else:
        zcr_v = float(np.median(zcr))
        hf_v = float(np.median(hf))
        flat_v = float(np.median(flat))

    # ---- 归一化到 0~1（1 = 更像哼唱）----
    def clamp(v):
        return float(max(0.0, min(1.0, v)))

    s_stable = clamp(long_stable / 0.55)          # 长稳定音高
    s_zcr = clamp((0.14 - zcr_v) / 0.12)          # 过零率越低越像哼唱
    s_hf = clamp((0.30 - hf_v) / 0.28)            # 高频越少越像哼唱
    s_mod = clamp((0.72 - mod_depth) / 0.55)      # 音节起伏越弱越像哼唱
    s_voiced = clamp((voiced_ratio - 0.35) / 0.5)  # 发声越连续越像哼唱
    s_flat = clamp((0.20 - flat_v) / 0.18)        # 谱越不「平」越像哼唱

    hum_score = (W_STABLE * s_stable + W_MOD * s_mod + W_VOICED * s_voiced +
                 W_ZCR * s_zcr + W_HF * s_hf + W_FLAT * s_flat)
    hum_score = clamp(hum_score)

    # 没检测到足够的浊音 → 判不了（纯静音、纯噪声、气音）。
    # 实测噪声的浊音占比只有 ~0.34，而说话 >=0.77、哼唱 ~1.0，用这个门限最稳。
    if voiced.sum() < 3 or voiced_ratio < 0.42:
        return {"ok": False,
                "message": "没有听清清晰的语音或哼唱，请靠近麦克风再说一次",
                "features": {"voiced_frames": int(voiced.sum()),
                             "total_frames": int(len(voiced)),
                             "voiced_ratio": round(voiced_ratio, 4)}}

    # 置信度：离阈值越远越有把握
    confidence = clamp(abs(hum_score - HUM_THRESHOLD) / 0.30 * 0.6 + 0.4)

    intent = "hum" if hum_score >= HUM_THRESHOLD else "speech"

    features = {
        "long_stable": round(long_stable, 4),
        "voiced_ratio": round(voiced_ratio, 4),
        "zcr": round(zcr_v, 4),
        "hf_ratio": round(hf_v, 4),
        "flatness": round(flat_v, 4),
        "mod_depth": round(mod_depth, 4),
        "voiced_frames": int(voiced.sum()),
        "total_frames": int(len(voiced)),
        "sub": {
            "稳定长音": round(s_stable, 3), "低过零": round(s_zcr, 3),
            "低高频": round(s_hf, 3), "弱调制": round(s_mod, 3),
            "连续发声": round(s_voiced, 3), "低谱平坦": round(s_flat, 3),
        },
    }
    return {
        "ok": True,
        "intent": intent,
        "hum_score": round(hum_score, 4),
        "threshold": HUM_THRESHOLD,
        "confidence": round(confidence, 3),
        "duration_sec": round(duration, 2),
        "features": features,
    }


if __name__ == "__main__":
    import json
    import sys
    for p in sys.argv[1:]:
        print(os.path.basename(p), json.dumps(analyze(p), ensure_ascii=False, indent=2))
