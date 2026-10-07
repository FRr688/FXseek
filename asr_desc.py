#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
音频转写（ASR）—— 对接 OpenAI 兼容 /v1/audio/transcriptions（如 oMLX 的 Qwen3-ASR）

用途：把音频内容转成文字，写入索引 meta，让「按歌词/说话内容搜索」成为可能。
依赖：仅标准库。
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

from net_util import urlopen_smart


class ASRError(Exception):
    pass


def _ffmpeg_bin() -> str:
    """拿 ffmpeg 的**绝对路径**：应用自带的 bin/ > $FFMPEG > 系统 PATH 与常见位置
    （解析逻辑跟 indexer 同一份，别自己再写一遍）。

    ★ 为什么不能直接写 `"ffmpeg"`：双击图标启动的打包版走 LaunchServices，
    进程 PATH 里根本没有 `/opt/homebrew/bin`，于是报
    `[Errno 2] No such file or directory: 'ffmpeg'` —— 明明应用自己带了一份。
    （`paths.py` 现在也会把 `bin/` 补进 PATH，这里是第二道保险：拿到绝对路径。）"""
    try:
        import indexer as ix
        return ix.FFMPEG
    except Exception:
        return "ffmpeg"


# 上传前统一转成 16 kHz 单声道 mp3。实测（本机 oMLX + Qwen3-ASR-1.7B-4bit）：
#   · 直接把 26 MB 的 FLAC 原样丢过去，一首 4 分钟的歌要十几分钟量级；
#   · 转成 16 kHz 单声道 48 kbps mp3 后体积缩到约 1/26，服务端解码也轻得多；
#   · 视频更是必须转 —— 原来是把整段视频（可能几百 MB）发给 ASR 服务。
ASR_RATE = 16000
ASR_BITRATE = "48k"

# ---- 分窗（★ 关键，别改掉）--------------------------------------------------
# Qwen3-ASR 在 oMLX 里走 `model.generate(..., chunk_duration=1200.0)` —— 默认
# 1200 秒等于「整首不切」，于是 4 分钟的歌一次性进音频编码器，**远超它的窗口**，
# 解码直接退化成复读机。实测同一首 248.6 秒的歌、同一个 4bit 模型：
#     整段（1200s 默认）→ 11513 字、唯一率 1.2%、46.3 字/秒、耗时 318 秒
#     60 秒分窗          →   356 字、唯一率 87.3%、 1.4 字/秒、耗时  20 秒
# 「46 字/秒」物理不可能（人类唱歌 2~5 字/秒），这就是复读的铁证。
# oMLX 的 /v1/audio/transcriptions 表单**没有 chunk_duration 字段**（audio_routes.py
# 只收 model/language/prompt/response_format/temperature/stream/max_tokens/
# word_timestamps），传了也被忽略 —— 所以分窗只能在客户端做，没有别的路。
# 顺带的好处：分窗让每片都短，速度快 15 倍，峰值内存也降下来；而每片的
# （音频, 文本）配对是**构造上就精确**的，正好给歌词逐字对齐当分窗器。
ASR_CHUNK_SEC = 60.0        # 单窗时长（实测 60 秒质量最好：唯一率 68~87%）
ASR_CHUNK_MIN = 20.0        # 退化重试时的最小窗

# ---- 复读退化判定（★ 2026-10 重做，别退回「字/秒」）--------------------------
# 旧实现用「非标点字数 ÷ 秒数 > 8」判复读，两个致命缺陷：
#   ① `_plain_len` 只认 CJK+ASCII，韩文/俄文/日文假名会被整段剥空 → 恒为 0 字/秒，
#      既判不出复读，也让这个数对其他语种毫无意义；
#   ② 阈值 8.0 是按中文标定的：正常英语 150 wpm ≈ 8.8 字母/秒就已经越线，
#      于是**内容完全正常的英文歌被判复读**，还会连锁触发 60s→20s 的二次切细
#      （请求数翻 3 倍），最后被 indexer 的入库闸门丢弃，报「疑似模型复读」。
# 改用与语种/语速/副歌都无关的两条判据（全库 123 首实测 0 误杀 0 漏判）：
#   A. **压缩比**（zlib）：复读是同一串东西刷屏，天然压得极狠。整段级实测
#      正常歌最低 0.156、复读最高 0.050，中间 3 倍空档。缺点：短文本压不动，
#      阈值必须随长度放宽，故 `_repeat_ratio_threshold` 按 600 字基准做平方根缩放。
#   B. **句子唯一率**：复读 4000 句里唯一句只有 11 个（0.3%），正常歌副歌再狠
#      也在 20% 以上。这一条不需要时长，正好补压缩比在短文本上的短板。
# 两条都命不中才放行 —— 宁可漏判（重试一次而已），也绝不误杀正常内容。
ASR_REPEAT_RATIO = 0.088    # 整段压缩比下限（600 字基准）
ASR_REPEAT_UNIQ = 0.15      # 句子唯一率下限
ASR_REPEAT_MIN_SENT = 8     # 句数少于此不启用唯一率判据（样本太少不可靠）


def _segment_for_asr(path: str, chunk_sec: float = ASR_CHUNK_SEC):
    """转码成 16 kHz 单声道 mp3 并**同时按 chunk_sec 切片**。

    返回 (片段路径列表, 临时目录)；转码不可用时返回 ([], None)。
    一次 ffmpeg 调用同时完成转码与切段（`-f segment`），实测 4 分钟的歌 0.4 秒。
    """
    ff = _ffmpeg_bin()
    td = tempfile.mkdtemp(prefix="fxseek_asr_")
    try:
        p = subprocess.run(
            [ff, "-v", "error", "-y", "-i", path, "-vn", "-ac", "1",
             "-ar", str(ASR_RATE), "-b:a", ASR_BITRATE,
             "-f", "segment", "-segment_time", str(int(max(1, chunk_sec))),
             "-reset_timestamps", "1", os.path.join(td, "seg_%04d.mp3")],
            capture_output=True, timeout=900)
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        return [], None
    segs = sorted(os.path.join(td, f) for f in os.listdir(td)
                  if f.startswith("seg_") and f.endswith(".mp3"))
    segs = [s for s in segs if os.path.getsize(s) > 0]
    if p.returncode != 0 or not segs:
        shutil.rmtree(td, ignore_errors=True)
        return [], None
    return segs, td


def _old_transcode_for_asr(path: str):
    """转成 16 kHz 单声道 mp3 整段（**不再用于转写**，仅退化兜底保留）。"""
    ff = _ffmpeg_bin()
    td = tempfile.mkdtemp(prefix="fxseek_asr_")
    out = os.path.join(td, "asr.mp3")
    cmd = [ff, "-v", "error", "-y", "-i", path, "-vn", "-ac", "1",
           "-ar", str(ASR_RATE), "-b:a", ASR_BITRATE, out]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=900)
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        return None
    if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        shutil.rmtree(td, ignore_errors=True)
        return None
    return out


def _seg_seconds(path: str) -> float:
    """片段的秒数：先问 ffprobe，失败再按 48 kbps 定码率（6 kB/s）估。"""
    try:
        import indexer as ix
        out = subprocess.run(
            [ix.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20)
        v = float(out.stdout.strip() or 0)
        if v > 0:
            return v
    except Exception:
        pass
    try:
        return max(0.1, os.path.getsize(path) * 8.0 / 48000.0)
    except Exception:
        return 0.0


def _plain_len(text: str) -> int:
    """去掉标点/空白后的字数。

    ★ 用 unicodedata 判「字母/数字」类别，别退回白名单正则：
    `[^\\u4e00-\\u9fffA-Za-z0-9]` 会把韩文/俄文/日文假名整段剥空，
    让这些语种的字数恒为 0（详见文件顶部关于复读判定的注释）。
    """
    import unicodedata as _ud
    return sum(1 for ch in (text or "") if _ud.category(ch)[0] in ("L", "N"))


def _sentences(text: str):
    """粗切句：中英文标点都认；过滤掉 1 个字以下的碎片。"""
    import re as _re
    parts = _re.split(r"(?<=[。！？!?；;\.])\s*", text or "")
    return [s.strip() for s in parts if len(s.strip()) >= 2]


def _repeat_ratio_threshold(n_chars: int) -> float:
    """压缩比阈值随文本长度放宽（短文本本来就压不动）。

    以 600 字为基准做平方根缩放，夹在 0.05 ~ 0.30：
    60 字 → 0.28、200 字 → 0.15、800 字 → 0.08、1500 字以上 → 0.05。
    （实测：中文 60 秒窗约 85 字、英文 60 秒窗约 150~200 字符，
    固定阈值在窗口尺度上会误杀，必须随长度走。）
    """
    import math as _math
    return min(0.30, max(0.05, ASR_REPEAT_RATIO * _math.sqrt(600.0 / max(n_chars, 40))))


def _looks_degenerate(text: str, seconds: float = 0.0) -> bool:
    """复读退化判定：压缩比过低 或 句子唯一率过低。

    `seconds` 保留在签名里只为兼容旧调用点，判定本身**不再依赖时长**
    （时长只影响音频、不影响文本是否复读，而缺 duration 时旧实现直接放行）。

    误判代价不对称：**漏判**只是多转一次（或入库后被 asr_status 挑出来重排），
    **误杀**却会把正常歌词丢掉并报「疑似复读」。所以两条判据都偏保守。
    """
    t = (text or "").strip()
    if not t:
        return False
    # A. 压缩比（短文本自动放宽阈值）
    raw = t.encode("utf-8")
    if len(raw) >= 40:
        import zlib as _zlib
        packed = len(_zlib.compress(raw, 9))
        if packed / len(raw) < _repeat_ratio_threshold(len(t)):
            return True
    # B. 句子唯一率
    sents = _sentences(t)
    if len(sents) >= ASR_REPEAT_MIN_SENT:
        if len(set(sents)) / len(sents) < ASR_REPEAT_UNIQ:
            return True
    return False


def _join_texts(parts) -> str:
    """拼接各窗文本：汉字/日文相邻直接接，其余补一个空格，避免粘连英文单词。"""
    import re as _re
    out = ""
    for s in parts:
        s = (s or "").strip()
        if not s:
            continue
        if out and not (_re.match(r"[\u4e00-\u9fff\u3040-\u30ff]", s[0])
                        and _re.search(r"[\u4e00-\u9fff\u3040-\u30ff]$", out)):
            out += " "
        out += s
    return out.strip()


def _ctype_for(filename: str) -> str:
    low = (filename or "").lower()
    if low.endswith(".wav"):
        return "audio/wav"
    if low.endswith(".m4a") or low.endswith(".mp4"):
        return "audio/mp4"
    if low.endswith(".flac"):
        return "audio/flac"
    if low.endswith(".ogg") or low.endswith(".opus"):
        return "audio/ogg"
    if low.endswith(".webm"):
        return "audio/webm"
    return "audio/mpeg"


def _transcribe_audio_file(base_url: str, api_key: str, model: str,
                           audio_bytes: bytes, filename: str,
                           language: str = "", timeout: int = 300) -> str:
    """调用 /v1/audio/transcriptions，返回转写文本。"""
    base = base_url.rstrip("/")
    url = (base + "/audio/transcriptions") if base.endswith("/v1") \
        else (base + "/v1/audio/transcriptions")

    boundary = "----dshAsr" + uuid.uuid4().hex
    # 构造 multipart/form-data
    buf = io.BytesIO()
    def field(name, value):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    def file_field(name, fname, data, ctype):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")

    field("model", model)
    if language:
        field("language", language)
    field("response_format", "json")
    file_field("file", filename, audio_bytes, _ctype_for(filename))
    buf.write(f"--{boundary}--\r\n".encode())

    body = buf.getvalue()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen_smart(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
            txt = d.get("text") or ""
            # 有些服务返回 segments
            if not txt and d.get("segments"):
                txt = " ".join(s.get("text", "") for s in d["segments"] if s.get("text"))
            return txt.strip()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise ASRError(f"HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise ASRError(f"无法连接 {url}（{e.reason}）")
    except Exception as e:
        raise ASRError(str(e))


def transcribe(base_url: str, api_key: str, model: str, audio_path: str,
               language: str = "", timeout: int = 1800) -> dict:
    """转写本地音频/视频。返回 {text, model, seconds, upload_bytes, chunks}。

    **按 ASR_CHUNK_SEC 分窗逐片转写再拼接**（原因见文件顶部那段注释：
    不分窗会让 Qwen3-ASR 退化成复读机）。转码/切片不可用时退回整段上传，
    行为与旧版一致。每一片都做「字/秒」退化判定，发现复读就把该片切得更细重试。
    """
    if not os.path.exists(audio_path):
        raise ASRError("音频文件不存在")
    if not os.path.getsize(audio_path):
        raise ASRError("音频文件为空")
    import time as _time
    t0 = _time.time()

    segs, td = _segment_for_asr(audio_path, ASR_CHUNK_SEC)
    upload_bytes = 0
    try:
        if segs:
            parts, retried = [], 0
            for sp in segs:
                with open(sp, "rb") as f:
                    data = f.read()
                upload_bytes += len(data)
                sec = _seg_seconds(sp)
                txt = _transcribe_audio_file(base_url, api_key, model, data,
                                             "audio.mp3", language, timeout=timeout)
                # 单窗退化 → 把这一窗切细重试一次（只重试一次，避免放大失败）
                if _looks_degenerate(txt, sec) and sec > ASR_CHUNK_MIN * 1.5:
                    sub, sub_td = _segment_for_asr(sp, ASR_CHUNK_MIN)
                    try:
                        if sub and len(sub) > 1:
                            retried += 1
                            fixed = []
                            for ss in sub:
                                with open(ss, "rb") as f:
                                    d2 = f.read()
                                upload_bytes += len(d2)
                                fixed.append(_transcribe_audio_file(
                                    base_url, api_key, model, d2, "audio.mp3",
                                    language, timeout=timeout))
                            cand = _join_texts(fixed)
                            if not _looks_degenerate(cand, sec):
                                txt = cand
                    finally:
                        if sub_td:
                            shutil.rmtree(sub_td, ignore_errors=True)
                parts.append(txt)
            txt = _join_texts(parts)
        else:
            # 退化路径：不会切片的容器（或没有 ffmpeg）只能整段发，行为同旧版
            tmp = _old_transcode_for_asr(audio_path)
            try:
                if tmp:
                    with open(tmp, "rb") as f:
                        data = f.read()
                    fname = "audio.mp3"
                else:
                    with open(audio_path, "rb") as f:
                        data = f.read()
                    fname = os.path.basename(audio_path)
            finally:
                if tmp:
                    shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
            if not data:
                raise ASRError("音频文件为空")
            upload_bytes = len(data)
            txt = _transcribe_audio_file(base_url, api_key, model, data, fname,
                                         language, timeout=timeout)
            segs = []
    finally:
        if td:
            shutil.rmtree(td, ignore_errors=True)
    if isinstance(txt, str):
        txt = txt.strip()
    return {"text": txt, "model": model, "upload_bytes": upload_bytes,
            "chunks": len(segs), "seconds": round(_time.time() - t0, 1)}


def test_connection(base_url: str, api_key: str, model: str) -> dict:
    """测试 ASR 连通性（用 1 秒静音/正弦波）。"""
    import subprocess, tempfile
    ff = _ffmpeg_bin()
    try:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "test.mp3")
            subprocess.run([ff, "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", "-y", fp],
                           capture_output=True, timeout=20)
            if not os.path.exists(fp):
                return {"ok": False, "message": f"ffmpeg 生成测试音频失败（用的 {ff}）"}
            r = transcribe(base_url, api_key, model, fp, language="")
            return {"ok": True, "message": f"连接成功，模型「{model}」响应正常",
                    "text": r["text"]}
    except FileNotFoundError:
        return {"ok": False,
                "message": f"本机找不到 ffmpeg（试过 {ff}），没法生成测试音频"}
    except ASRError as e:
        return {"ok": False, "message": f"转写失败：{e}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
