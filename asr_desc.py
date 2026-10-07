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

# ---- 输出 token 上限（★ 关键，别改掉）---------------------------------------
# oMLX 的 STT 里 max_tokens 默认 8192（engine/stt.py: `max_tokens = int(kwargs.get
# ("max_tokens") or 8192)`），而**退化时模型不会自己停**，会一路生成到上限才返回 ——
# 实测单窗输出 24564~32758 字、耗时 130~220 秒。这就是用户看到的
# 「有的音频特别快，有的卡死，一首歌几分钟都没转录出来」。
# 好在 oMLX 的 /v1/audio/transcriptions 收 `max_tokens` 表单字段（非 OpenAI 标准，
# 属 oMLX 扩展），所以按「每秒音频给多少 token」给它封顶即可。同一退化窗实测：
#     不设上限        216.0s / 28664 字
#     cap = 秒数×10    17.4s /  2085 字   ← 12.4 倍
#     cap = 秒数×20    25.6s /  4185 字
#     cap = 秒数×40    53.0s /  8385 字
# 取 20/s：正常中英文 speech 实测 ≤8 token/秒，留了 2.5 倍余量，不会截断正常内容；
# 退化窗被截断后照样判为退化，照旧触发下面的 20 秒重切并恢复干净文本
# （实测「say yeah」退化窗重切后 688 字、整体判退化 False）。
# 不认识这个字段的服务端会报 4xx —— 那时自动退回「不带 max_tokens」重试一次，
# 并永久记住（_NO_MAX_TOKENS），不再打扰。
ASR_TOKEN_RATE = 20.0       # 每秒音频允许的最大输出 token
ASR_TOKEN_MIN = 60          # 再短的窗也至少给这么多 token
_NO_MAX_TOKENS = False      # 服务端不支持 max_tokens 时置 True（自动降级）


def _token_cap(seconds: float) -> int:
    """按音频秒数算输出 token 上限（见 ASR_TOKEN_RATE 注释）。"""
    if seconds <= 0:
        return ASR_TOKEN_MIN
    return max(ASR_TOKEN_MIN, int(seconds * ASR_TOKEN_RATE))

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
# C. **字/秒**（2026-10 补，专治上面两条的漏网之鱼）：
#      压缩比和唯一率都怕同一种文本 —— **几乎全用逗号连写的复读**。
#      `_sentences` 按句末标点切，逗号连写只能切出几句（< 8 就停用唯一率判据），
#      而短文本本身又压不动（实测某退化窗 2854 字 / 41.2 秒：压缩比 0.0687 > 阈值
#      下限 0.05、唯一率判据因只切出 7 句而不启用 → 两条都放行）。
#      于是补一条最朴素的物理约束：**人说话/唱歌的字数速度有上限**。
#      全库实测（按 60 秒比例切片模拟分窗）：
#          98 首干净歌的 419 个窗口 → 最大 11.00/s，P99=9.90、P95=7.60
#          29 首复读歌的 123 个窗口 → 最小 22.75/s
#      中间有干净的 2 倍空档；阈值取 12~20 任意值都是 0 误杀 0 漏判，取中间 15。
#      ★ 只对「够长的窗口」启用（秒数 ≥ ASR_CPS_MIN_SEC）：几秒的尾窗字数样本
#      太少，稍有波动就会越线，没必要拿它冒险 —— 那种短窗交给上面两条判据。
ASR_REPEAT_RATIO = 0.088    # 整段压缩比下限（600 字基准）
ASR_REPEAT_UNIQ = 0.15      # 句子唯一率下限
ASR_REPEAT_MIN_SENT = 8     # 句数少于此不启用唯一率判据（样本太少不可靠）
ASR_CPS_MAX = 15.0          # 字/秒 上限（超它即复读）
ASR_CPS_MIN_SEC = 10.0      # 短于此的窗口不启用字/秒判据


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
    """复读退化判定：压缩比过低 或 句子唯一率过低 或 字/秒过高。

    三条判据互为补位，**任一命中即判退化**（详见常量区 A/B/C 三段注释）：
      A 压缩比   —— 复读是同一串刷屏，压得极狠；短文本自动放宽阈值。
      B 句子唯一率 —— 不需要时长，正好补 A 在短文本上的短板。
      C 字/秒    —— 专治「逗号连写」：A 压不动、B 又切不出 8 句，只有物理
                   语速上限能兜住。只对 ≥ ASR_CPS_MIN_SEC 的窗口启用。

    误判代价不对称：**漏判**只是多转一次（或入库后被 asr_status 挑出来重排），
    **误杀**却会把正常歌词丢掉并报「疑似复读」。所以三条判据都偏保守。
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
    # C. 字/秒（见常量区 C 段注释）：逗号连写的复读既压不动也切不出句子，
    #    只有这条能兜住。窗口太短时不启用（样本少，波动大）。
    if seconds and seconds >= ASR_CPS_MIN_SEC:
        if _plain_len(t) / seconds > ASR_CPS_MAX:
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
                           language: str = "", timeout: int = 300,
                           max_tokens: int = 0) -> str:
    """调用 /v1/audio/transcriptions，返回转写文本。

    max_tokens > 0 时带上该字段（oMLX 扩展，防退化窗跑满 8192 上限，见
    ASR_TOKEN_RATE 注释）；若服务端不认这个字段（4xx），自动去掉重试一次
    并置 _NO_MAX_TOKENS，后续不再尝试。
    """
    global _NO_MAX_TOKENS
    if _NO_MAX_TOKENS:
        max_tokens = 0
    base = base_url.rstrip("/")
    url = (base + "/audio/transcriptions") if base.endswith("/v1") \
        else (base + "/v1/audio/transcriptions")

    def _build(with_cap: bool) -> bytes:
        boundary = "----dshAsr" + uuid.uuid4().hex
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
        if with_cap and max_tokens > 0:
            field("max_tokens", str(int(max_tokens)))
        file_field("file", filename, audio_bytes, _ctype_for(filename))
        buf.write(f"--{boundary}--\r\n".encode())
        return boundary, buf.getvalue()

    def _post(with_cap: bool):
        boundary, body = _build(with_cap)
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urlopen_smart(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
            txt = d.get("text") or ""
            # 有些服务返回 segments
            if not txt and d.get("segments"):
                txt = " ".join(s.get("text", "") for s in d["segments"] if s.get("text"))
            return txt.strip()

    try:
        try:
            return _post(True)
        except urllib.error.HTTPError as e:
            # 400/422 常意味着服务端不认 max_tokens：降级重试一次并记住
            if max_tokens > 0 and e.code in (400, 404, 405, 422):
                _NO_MAX_TOKENS = True
                return _post(False)
            raise
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
            for si, sp in enumerate(segs):
                with open(sp, "rb") as f:
                    data = f.read()
                upload_bytes += len(data)
                sec = _seg_seconds(sp)
                # ★ 首窗要替后面所有窗「预热」oMLX 的 ASR 模型：实测首窗 31.5s、
                # 之后每窗 4~5s（模型加载/编译摊在第一个请求上）。给首窗放宽
                # 超时，别让本来正常的它被 timeout 掐掉。
                to = timeout if si else max(timeout, 600)
                txt = _transcribe_audio_file(base_url, api_key, model, data,
                                             "audio.mp3", language, timeout=to,
                                             max_tokens=_token_cap(sec))
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
                                s2 = _seg_seconds(ss)
                                fixed.append(_transcribe_audio_file(
                                    base_url, api_key, model, d2, "audio.mp3",
                                    language, timeout=timeout,
                                    max_tokens=_token_cap(s2)))
                            cand = _join_texts(fixed)
                            # ★ 重切后仍退化 → 两版都是垃圾（原版是被 max_tokens
                            # 截断的复读刷屏，重切版也没救回来），**丢弃这一窗**，
                            # 不要把它拼进全文：否则几千字的「Yeah. Yeah. Yeah.」
                            # 会污染关键词与向量，还会让 indexer 的入库闸门把整首
                            # （含正常窗）一起丢掉。少一窗最多少一段歌词，比留一坨
                            # 复读好；保住其余正常窗才有意义。
                            txt = cand if not _looks_degenerate(cand, sec) else ""
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
                                         language, timeout=max(timeout, 600),
                                         max_tokens=_token_cap(_seg_seconds(audio_path)))
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
