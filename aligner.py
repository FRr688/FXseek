#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增强对齐时间轴：Qwen3 强制对齐（forced aligner）。

## 它解决什么

转写只给你「这一整段是什么字」，不给你「第 37 秒在唱哪一句」。所以播放器只能
把歌词按行数均分来高亮——副歌一长，高亮就跑偏，用户看到的是「整段窗口」而不是
「跟着唱到哪」。

强制对齐是拿「已知文本 + 音频」去求每个字/词的时间戳，正好补上这一段。开这个
开关之后，音频/视频的播放面板会逐字跟着唱。

## 为什么是一个独立进程

对齐模型（Qwen3-ForcedAligner-0.6B-8bit）要占 1.2GB 左右显存，而且是**偶发**
需求：转写完一个文件才用一次。常驻在那儿陪检索模型一起占内存不划算，所以对齐
永远走 `subprocess`：主进程 spawn 一个 worker，worker 加载模型、对齐、写结果、
退出，内存整个还给系统。

## 为什么必须分块

实测（本机 16G，metal 单次分配上限 9534832640 字节 ≈ 8.88 GiB）：

    时长      audio_tokens   words    seq     估算分配
    300 s     3900           1188     6276    1.91 GB
    600 s     7800           2377     12554   3.82 GB
    1200 s    15600          4755     25110   7.63 GB
    1500 s    19500          5943     31386   9.54 GB  ← 贴着上限
    1800 s    23400          7132     37664   11.45 GB ← 直接崩

用户库里三个多小时的演唱会 flac 是常态（3524 s / 13964 字），整段喂进去必然
`RuntimeError: [metal::malloc] ...`。所以这里要切块：每块用 ffmpeg 抽 16kHz
单声道，文本按**同比例字数**切，对齐完把时间戳加上块偏移。

★ 切块策略是「预算驱动 + 窗口尽量大」，不是「固定 300 s 一块」。这两个决定都有
实测依据，改之前务必看完：

1. **窗口数由内存预算算出，不是拍脑袋定的**。序列长度
   `seq ≈ 13*T + 2*W`（`T` 秒、`W` 词数；`audio_tokens ≈ 13/s` 是 `n_window_infer`
   派生的固定压缩率），实测每 seq token 约 293600 字节的峰值分配。取
   `BUDGET_SEQ = 18000`（≈5.3 GB）就得到 `n = ceil(seq / BUDGET_SEQ)`，
   `chunk = duration / n`。例：60 s 素材 → 1 块；306 s/493 字 → 1 块；
   2886 s/6581 字 → 3 块 × 962 s。预算只是估算（词数用字符数近似），真到边界
   上还会差一点，所以窗口撞 metal 上限时还会**二分重试**（`_align_chunk`）。

2. **窗口要大，不然会塌**。窗口越小，窗口里「非人声段」占比越高，而模型无从知道
   前奏/间奏有多长 —— 实测把 60.4 s 素材（人声从 22.0 s 才开始）切到 30 s 窗口，
   前 8 条时间戳全部塌成 0.0；切到 90 s 以上才正常。所以「300 s 一块」这种保守值
   反而是**更差**的选择，能一大块跑完就别切。

3. **不要用「自适应游标 / 按已提交进度推进窗口」那一套**。试过：拿最后一个条目的
   `end` 当游标往前推，结果在 962 s 窗口里模型把所有时间戳压在头 245 s，
   游标就只走 245 s，窗口大量重叠空转，同一个 2886 s 素材 110 s 变成 264 s，
   且与单遍结果 mean 差 934 s。**按固定预算等分窗口才是对的。**

分块准确性有 ground truth 验证过：把 60.4 s 的素材复制三份拼成 180 s，整段一次
对出 `'阳'` 在 0.0 / 82.16 / 142.72；按 60.4 s 分三块再各加偏移得到 22.0 / 82.4 /
142.8 —— 误差 ≤0.08 s，只来自 ffmpeg 切点。够歌词用。306 s 素材单块与 GT 逐条
比对 mean 0.11 s（多块比例切才会劣化到 mean 9.6 s 以上）。

好消息是这不是性能问题：库里 302 个已转写音视频有 242 个（80%）≤300 s，
一块就完事，只有 20% 真的会走多块。

## 为什么视频要先抽音轨

`mlx_audio.audio_io` 走 miniaudio，只认 WAV/MP3/FLAC/Vorbis；mp4/mov 这类
**视频容器喂不进去**。所以这里一律先用 ffmpeg 抽成 16k 单声道 wav，顺便把采样率
也规整了。

## 为什么日/韩要跳过

`ForceAlignProcessor.encode_timestamp()` 里日语要 nagisa、韩语要 soynlp，本包
venv 里都没装，直接抛 `ImportError`。对齐是后台队列里的一环，让 ImportError 炸穿
整条队列是不可接受的，所以这两种语种在**进模型之前**就优雅跳过，并记 `align_na`，
不反复重试。

## 落库形状

`items.meta` 是自由 JSON，加字段不用迁移。对齐结果写在这些键上：

    align_ok      True 表示对齐成功
    align_chunk   本次用的块长（秒）
    align_chunks  实际切了几块
    align_lang    实际使用的语种名
    align_model   模型名
    align_at      完成时间戳
    align_chars   对齐时 asr_text 的字符数（**用来发现文本变了**：
                  重新转写后字数对不上 → 前端不认这份时间轴、队列重新对齐）
    align_items   [[词, 起, 止, 在原文本里的字符偏移], ...]
    align_na      True 表示「这个语种对不了」，不再重试

`align_chars` 是这套设计里最关键的一个字段：用户点了「重新转写」之后，旧时间轴
必须立刻失效，否则会出现「歌词是新的、高亮位置是旧的」这种最难查的错位。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

# 默认块长。★ 0 = 按内存预算自动决定（见 plan_chunks），且**窗口越大越好**：
# 窗口小反而会把时间戳压塌（见文件头说明）。传正数则表示「强制每块不超过这么多秒」，
# 一般只在做对照实验时才用。
DEFAULT_CHUNK_SEC = 0

# 单个窗口的序列预算（seq token 数）。实测每 seq token 约 293600 字节的峰值分配，
# 18000 对应每块约 5.3 GB；metal 单次分配上限是 9534832640 字节（约 8.88 GiB，
# 实测 seq 上限约 32400），留出对齐模型本身（1.27 GB）与检索模型的余量。
#
# ★ 这个值不是拍的：取 18000 时，实测跑通过的 2886 s / 6581 字素材正好切成
# 3 块 × 962 s（seq 50680 / 18000 → 3），而 962 s 这一档是实测验证过能跑完、
# 且比更小窗口更准的窗口长度（见文件头第 2 条）。
BUDGET_SEQ = 18000

# seq ≈ SEQ_PER_SEC * 秒 + SEQ_PER_WORD * 词数。13 tokens/s 是 n_window_infer=800
# 派生的固定音频压缩率（60 s→780、300 s→3900、1800 s→23400，线性且实测吻合）；
# 词数在中文里约等于字数（逐字分词），英文按词。
SEQ_PER_SEC = 13.0
SEQ_PER_WORD = 2.0

# 窗口数的上限。纯粹是防呆：真出现「预算算出几百块」的离谱输入时也别真的切几百块。
MAX_CHUNKS = 64

# 单窗口 OOM 后的二分重试深度。预算公式是估算，真撞上 metal 分配失败时二分兜底，
# 比只靠公式稳健（实测 1800 s 整段直接报 Attempting to allocate 11059200000 bytes）。
OOM_SPLIT_DEPTH = 4

# 一次最多存多少条时间轴。极端素材（3524 s / 13964 字）会产出近两万条，
# 不设上限的话 meta 会长到几百 KB，详情页每次都要传一遍。
MAX_ITEMS = 60000

# worker 超时（秒）。按块数动态放宽，见 run_worker()。
DEFAULT_TIMEOUT = 3600

# 只做「按空格分词」的语言可以对齐；日/韩缺分词库，进模型前就拦掉。
UNSUPPORTED_LANGS = {"japanese", "korean"}

# 模型能对齐的语种（README / get_supported_languages() 实测返回的 11 种）
SUPPORTED_LANGS = {
    "cantonese", "chinese", "english", "french", "german", "italian",
    "japanese", "korean", "portuguese", "russian", "spanish",
}

# ---------------------------------------------------------------------------
# 语种
# ---------------------------------------------------------------------------

_LANG_ALIASES = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-hans": "Chinese", "zh-hant": "Chinese",
    "chs": "Chinese", "cht": "Chinese", "cmn": "Chinese", "chinese": "Chinese",
    "中文": "Chinese", "汉语": "Chinese", "漢語": "Chinese", "普通话": "Chinese",
    "yue": "Cantonese", "cantonese": "Cantonese", "粤语": "Cantonese",
    "粵語": "Cantonese", "广东话": "Cantonese",
    "en": "English", "eng": "English", "english": "English", "英文": "English",
    "英语": "English",
    "ja": "Japanese", "jp": "Japanese", "jpn": "Japanese", "japanese": "Japanese",
    "日本語": "Japanese", "日文": "Japanese", "日语": "Japanese",
    "ko": "Korean", "kor": "Korean", "korean": "Korean", "한국어": "Korean",
    "韩语": "Korean", "韩文": "Korean", "韓語": "Korean",
    "de": "German", "german": "German", "deu": "German",
    "fr": "French", "french": "French", "fra": "French",
    "es": "Spanish", "spanish": "Spanish", "spa": "Spanish",
    "it": "Italian", "italian": "Italian", "ita": "Italian",
    "pt": "Portuguese", "portuguese": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "russian": "Russian", "rus": "Russian",
}

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f]"
)


def _cjk_ratio(text):
    if not text:
        return 0.0
    n = len(_CJK_RE.findall(text))
    return n / float(len(text))


def normalize_language(raw, text=""):
    """把设置里那串自由文本（'' / 'zh' / '中文' / 'auto' …）翻译成对齐模型认的语种名。

    认不出来的时候**看文本本身**：CJK 占比超过 20% 就当中文。宁可猜中文也别猜
    English —— 猜错了整条时间轴都是废的，而用户界面里那句简介写的是「中文歌词效果最好」。
    """
    s = (raw or "").strip()
    if s:
        key = s.lower()
        if key in _LANG_ALIASES:
            return _LANG_ALIASES[key]
        if key in ("auto", "自动", "自动检测", "none"):
            s = ""
        else:
            # 认不出来的原样交上去：模型那侧对未知语种会走按空格分词，
            # 比强行映射成错的语言安全（至少不会把中日韩当英语切）。
            return s[:1].upper() + s[1:]
    if _cjk_ratio(text) >= 0.20:
        return "Chinese"
    return "English"


# ---------------------------------------------------------------------------
# 日志（★ 需求明确要求「必须写日志」）
# ---------------------------------------------------------------------------

_LOG_MAX = 3 * 1024 * 1024


def log_path():
    try:
        import paths as P
        d = os.path.join(P.DATA_DIR, "logs")
    except Exception:
        d = os.path.join(HERE, "logs")
    return os.path.join(d, "aligner.log")


def log(msg):
    """同时打屏和落盘。落盘才是需求要的那份 —— op_log() 只是内存环形队列，
    进程一退就没了，而「对齐为什么没跑」正是需要事后翻的东西。"""
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print("[对齐] %s" % msg, flush=True)
    except Exception:
        pass
    try:
        p = log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > _LOG_MAX:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 外部命令
# ---------------------------------------------------------------------------

def _ffmpeg():
    """必须用绝对路径。双击启动的打包版 PATH 里没有 /opt/homebrew/bin，
    写 'ffmpeg' 会直接 FileNotFoundError。"""
    try:
        import indexer as ix
        if getattr(ix, "FFMPEG", ""):
            return ix.FFMPEG
    except Exception:
        pass
    return "ffmpeg"


def _probe_duration(path):
    try:
        import indexer as ix
        return float(ix._probe_duration(path) or 0)
    except Exception:
        return 0.0


def _db_path():
    try:
        import paths as P
        return P.DB_PATH
    except Exception:
        return os.path.join(HERE, "data", "index.db")


def _extract_chunk(src, start, dur, out_path):
    """把源文件的 [start, start+dur) 抽成 16kHz 单声道 wav。

    统一走 ffmpeg 而不是把源文件直接喂给模型，有两个理由：
      1. 视频容器（mp4/mov）miniaudio 不认，必须先抽音轨；
      2. 顺便把采样率规整成模型要的 16k，省得模型内部再重采样一遍。
    """
    cmd = [_ffmpeg(), "-v", "error", "-y"]
    if start and start > 0.01:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", src, "-vn", "-ac", "1", "-ar", "16000"]
    if dur and dur > 0:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-f", "wav", out_path]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=900)
    except Exception as e:
        log("ffmpeg 抽音轨异常：%s" % e)
        return False
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 44


# ---------------------------------------------------------------------------
# 文本分块
# ---------------------------------------------------------------------------

_SNAP_PUNCT = "。！？；.!?;，,、\n"


def _snap_index(text, idx, width=40):
    """把切点挪到最近的标点后面，别把一个词从中间劈开。

    实测按纯比例切是能对上的（300 s 块首词误差 0.00），但切在词中间会让模型那一
    侧的第一/最后一个 token 时间戳发毛；挪到标点上既免费又更稳。
    """
    n = len(text)
    if idx <= 0 or idx >= n:
        return idx
    hi = min(n, idx + width)
    for i in range(idx, hi):
        if text[i] in _SNAP_PUNCT:
            return i + 1
    lo = max(0, idx - width)
    for i in range(idx - 1, lo - 1, -1):
        if text[i] in _SNAP_PUNCT:
            return i + 1
    return idx


def plan_chunks(duration, n_chars, chunk_sec=0):
    """算出这次对齐该切几块、每块多长。返回 (块长秒, 块数)。

    ★ 这是「预算驱动 + 窗口尽量大」，不是固定 300 s。理由见文件头第 1、2 条：
    窗口小会把时间戳压塌，而预算公式能保证不撞 metal 上限。

    `chunk_sec > 0` 表示调用方强制了窗口上限（对照实验用），此时只用它。
    """
    duration = float(duration or 0)
    if duration <= 0:
        return 0.0, 1
    if chunk_sec and chunk_sec > 0:
        n = max(1, int(duration / chunk_sec) + (1 if duration % chunk_sec else 0))
        return float(chunk_sec), min(n, MAX_CHUNKS)

    # 词数用字符数近似：中文逐字分词，英文按词但平均词长也就 4–5 字符，
    # 这个近似对「预算」这种量级判断足够（实测 2886 s/6581 字算出的 3 块正好）。
    seq = SEQ_PER_SEC * duration + SEQ_PER_WORD * max(0, n_chars or 0)
    n = max(1, int(seq / BUDGET_SEQ) + (1 if seq % BUDGET_SEQ else 0))
    n = min(n, MAX_CHUNKS)
    return duration / n, n


def split_text_by_time(text, duration, chunk_sec):
    """按时间比例把文本切成块。返回 [(块起点秒, 块长秒, 子文本, 子文本在原文本里的偏移)]。

    用「同比例字数」而不是「按句子数」：模型要的是「这段音频里的字」，音频是按时长
    切的，文本自然也要按时长切。实测一致。

    ★ 注意这里**不做自适应调整**。试过「按上一块提交到的位置推进游标」，在长窗口下
    模型会把时间戳全压在窗口头部，游标就只往前走一点点，窗口大量重叠空转（2886 s
    素材 110 s 变 264 s）。固定按预算等分才是对的。
    """
    text = text or ""
    if duration <= 0 or chunk_sec <= 0 or duration <= chunk_sec:
        return [(0.0, max(duration, 0.0), text, 0)]

    out = []
    n = len(text)
    t = 0.0
    prev = 0
    while t < duration:
        end = min(t + chunk_sec, duration)
        if end >= duration:
            b = n
        else:
            b = _snap_index(text, int(round(n * (end / duration))))
            if b <= prev:
                b = min(n, prev + 1)
        out.append((t, end - t, text[prev:b], prev))
        prev = b
        t = end
        if prev >= n:
            break
    return out


# ---------------------------------------------------------------------------
# 时间戳清洗
# ---------------------------------------------------------------------------

def _is_kept(ch):
    """和 mlx_audio 的 ForceAlignProcessor.is_kept_char 同一判据：
    字母 / 数字 / 撇号算内容，标点空白算分隔。"""
    if ch == "'":
        return True
    return unicodedata.category(ch)[:1] in ("L", "N")


def token_offsets(text, tokens, base=0):
    """把对齐结果里的词，一个个对回**原始文本**的字符位置。

    模型那侧的分词会把标点/空白全丢掉，所以拿不到现成的偏移，只能顺着找：
    从游标往后搜这个词的第一个出现位置。词序是有保证的（分词是按文本顺序做的），
    所以顺序搜索不会串位。

    为什么非要这个偏移：前端要做的映射是「这个词属于哪一行歌词」。有偏移就能把
    行（在原文本里的区间）和词直接对上，不用再猜。
    """
    out = []
    cur = 0
    n = len(text)
    for tok in tokens:
        t = tok or ""
        if not t:
            continue
        idx = text.find(t, cur)
        if idx < 0:
            # 找不到（理论上不该发生：分词就是从这份文本切出来的）。
            # 退化成「接着上一个词」，宁可偏移差一点也不要整条时间轴作废。
            idx = cur
        idx = min(idx, n)
        out.append((t, base + idx))
        cur = idx + len(t)
    return out


def clean_item(tok, start, end, duration, cbase=-1):
    """清洗一条 (词, 起, 止)，夹到 [0, duration] 内。返回 [词,起,止,字符偏移] 或 None。

    ★ 实测模型会**越过音频末尾**：60.40 s 的素材最后一条落在 65.44 s，末尾那串
    'oh' 全是 65.440–65.440（零长度）。不夹的话前端会拿着一个永远不会到达的时间
    去比，那一行就永远不高亮。
    """
    if not tok:
        return None
    s = float(start)
    e = float(end)
    if s < 0:
        s = 0.0
    if duration > 0:
        if s >= duration:
            return None            # 完全越界：丢掉
        if e > duration:
            e = duration
    if e <= s:
        # 零长度或倒挂：给一个最小可见时长，别让这个字彻底不亮。
        e = s + 0.06
        if duration > 0 and e > duration:
            e = duration
        if e <= s:
            return None
    return [tok, round(s, 2), round(e, 2), int(cbase)]


def clean_items(raw_items, offset, duration):
    """批量清洗。raw_items 是 (词, 起, 止) 三元组，offset 是整体偏移。"""
    out = []
    for tok, s, e in raw_items:
        it = clean_item(tok, float(s) + offset, float(e) + offset, duration)
        if it is not None:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# 对齐本体（在 worker 进程里跑）
# ---------------------------------------------------------------------------

def _load_model(model_dir, log_fn=None):
    from mlx_audio.stt.utils import load_model
    t0 = time.time()
    m = load_model(model_dir)
    if log_fn:
        log_fn("模型加载完成 %.2fs：%s" % (time.time() - t0, model_dir))
    return m


def align_file(path, text, language="", duration=0.0, model_dir=None,
               chunk_sec=DEFAULT_CHUNK_SEC, log_fn=None):
    """对一个音视频做强制对齐。**在 worker 进程里调用**。

    返回
      {"ok": True, "items": [[词, 起, 止, 字符偏移], ...], "lang", "chunks", "seconds"}
      {"ok": True, "skipped": True, "reason": ...}   ← 日/韩、空文本
      {"ok": False, "message": ...}
    """
    def _log(m):
        (log_fn or log)(m)

    text = (text or "").strip()
    if not text:
        return {"ok": True, "skipped": True, "reason": "没有转写文本"}

    if not model_dir or not os.path.isdir(model_dir):
        return {"ok": False, "message": "对齐模型目录不存在：%s" % model_dir}

    lang = normalize_language(language, text)
    if lang.lower() in UNSUPPORTED_LANGS:
        return {"ok": True, "skipped": True,
                "reason": "暂时不支持 %s（缺分词库 nagisa / soynlp）" % lang}

    if not duration or duration <= 0:
        duration = _probe_duration(path)
    if not duration or duration <= 0:
        return {"ok": False, "message": "拿不到时长（ffprobe 失败）"}

    forced = float(chunk_sec or 0)
    chunk, nchunks = plan_chunks(duration, len(text), forced)
    chunks = split_text_by_time(text, duration, chunk)
    _log("%s · %.1fs · %d 字 · %s · %d 块 × %.0fs（预算 %d）"
         % (os.path.basename(path), duration, len(text), lang,
            len(chunks), chunk, BUDGET_SEQ))

    try:
        model = _load_model(model_dir, _log)
    except Exception as e:
        return {"ok": False, "message": "加载对齐模型失败：%s: %s"
                % (type(e).__name__, e)}

    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="fxseek-align-")
    items = []
    try:
        for i, (start, clen, sub, base) in enumerate(chunks):
            got, err = _align_chunk(model, tmp, i, path, start, clen, sub,
                                    base, text, duration, lang, 0)
            if err:
                return {"ok": False, "message": err}
            items.extend(got)
            _log("第 %d/%d 块完成：%d 条（%.1f–%.1fs）"
                 % (i + 1, len(chunks), len(got), start, start + clen))
            if len(items) > MAX_ITEMS:
                _log("时间轴超过 %d 条，截断" % MAX_ITEMS)
                items = items[:MAX_ITEMS]
                break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    el = time.time() - t0
    _log("完成：%d 条 · 对齐耗时 %.2fs" % (len(items), el))
    return {"ok": True, "items": items, "lang": lang,
            "chunks": len(chunks), "seconds": round(el, 2),
            "duration": duration, "chunk": round(chunk, 1)}


def _is_oom(e):
    """认 metal 分配失败。文案实测是
    `[metal::malloc] Attempting to allocate N bytes which is greater than the
    maximum allowed buffer size of M bytes.`"""
    s = "%s" % (e,)
    return "metal" in s or "Attempting to allocate" in s or "buffer size" in s


def _align_chunk(model, tmp, idx, path, start, clen, sub, base, text,
                 duration, lang, depth, tag="c"):
    """对齐一个窗口，返回 (items, error_message)。

    ★ 撞到 metal 分配上限时**二分重试**：预算公式是估算（词数用字符数近似），
    真到边界上还会差一点。二分比只靠公式稳，代价只是多抽一次音轨。
    实测 1800 s 整段直接报 `Attempting to allocate 11059200000 bytes`。
    """
    sub = (sub or "").strip()
    if not sub:
        return [], None
    wav = os.path.join(tmp, "%s%04d.wav" % (tag, idx))
    if not _extract_chunk(path, start, clen, wav):
        log("第 %s%d 块抽音轨失败，跳过（%.1f–%.1fs）"
            % (tag, idx, start, start + clen))
        return [], None
    try:
        r = model.generate(wav, text=sub, language=lang)
    except Exception as e:
        if _is_oom(e) and depth < OOM_SPLIT_DEPTH and clen > 5:
            log("块 %.1f–%.1fs 撞内存上限（%.0fs），二分重试（第 %d 层）"
                % (start, start + clen, clen, depth + 1))
            half = clen / 2.0
            b = _snap_index(text, base + int(round(len(sub) * (half / clen))))
            if b <= base:
                b = min(len(text), base + max(1, len(sub) // 2))
            left, e1 = _align_chunk(model, tmp, idx, path, start, half,
                                    text[base:b], base, text, duration, lang,
                                    depth + 1, tag + "L")
            if e1:
                return [], e1
            right, e2 = _align_chunk(model, tmp, idx, path, start + half,
                                     clen - half, text[b:], b, text, duration,
                                     lang, depth + 1, tag + "R")
            if e2:
                return [], e2
            return left + right, None
        return [], "块 %.1f–%.1fs 对齐失败：%s: %s" % (
            start, start + clen, type(e).__name__, e)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    raws = [(it.text, it.start_time, it.end_time) for it in r.items]
    # ★ 先按本块文本求字符偏移（相对于整篇 text），再交给 clean_item 夹时间。
    offs = token_offsets(text, [x[0] for x in raws], base)
    out = []
    for j, (tok, s, e) in enumerate(raws):
        cbase = offs[j][1] if j < len(offs) else -1
        # 块偏移 start 直接加在时间上；越界/零长度在这里一并丢掉。
        it = clean_item(tok, s + start, e + start, duration, cbase)
        if it is not None:
            out.append(it)
    return out, None


# ---------------------------------------------------------------------------
# worker 入口：python aligner.py --job <job.json>
# ---------------------------------------------------------------------------

def _run_job(job):
    res = align_file(
        job["path"], job.get("text") or "", job.get("language") or "",
        duration=float(job.get("duration") or 0),
        model_dir=job.get("model_dir"),
        chunk_sec=int(job.get("chunk_sec") or DEFAULT_CHUNK_SEC),
    )
    res["worker_pid"] = os.getpid()
    return res


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="FXseek 强制对齐 worker")
    ap.add_argument("--job", required=True, help="任务 JSON 文件")
    args = ap.parse_args(argv)

    with open(args.job, "r", encoding="utf-8") as f:
        job = json.load(f)
    out = job.get("out")
    try:
        res = _run_job(job)
    except Exception as e:
        import traceback
        res = {"ok": False,
               "message": "%s: %s" % (type(e).__name__, e),
               "trace": traceback.format_exc()[-2000:]}
    if out:
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        except Exception as e:
            print("写结果失败：%s" % e, flush=True)
            return 2
    else:
        print(json.dumps(res, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# 主进程侧：spawn / 状态 / 落库
# ---------------------------------------------------------------------------

def run_worker(path, text, language="", duration=0.0, model_dir=None,
               chunk_sec=DEFAULT_CHUNK_SEC, timeout=None):
    """在主进程里调用：起一个 worker 子进程跑完一次对齐，返回结果 dict。

    ★ 用 `sys.executable`：主进程本身就跑在打包 venv 的 python3.11 里，
    这样不用去猜解释器在哪。

    `chunk_sec` 传 0（默认）就让 worker 按内存预算自己决定块长；只有做对照实验
    才传正数强制窗口上限。
    """
    tmp = tempfile.mkdtemp(prefix="fxseek-alignjob-")
    jobf = os.path.join(tmp, "job.json")
    outf = os.path.join(tmp, "out.json")
    job = {"path": path, "text": text, "language": language,
           "duration": duration, "model_dir": model_dir,
           "chunk_sec": int(chunk_sec or 0), "out": outf}
    with open(jobf, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)

    _c, n_chunks = plan_chunks(duration, len(text or ""), chunk_sec)
    if not timeout:
        # 每块实测最快 ~5 s，慢的（长文件 + 读盘）几十秒；给足但别无限等。
        timeout = max(600, min(DEFAULT_TIMEOUT, n_chunks * 240 + 300))

    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(HERE, "aligner.py"), "--job", jobf],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout)
        tail = (p.stdout or b"").decode("utf-8", "replace")[-1500:]
        if p.returncode != 0:
            return {"ok": False,
                    "message": "worker 退出码 %d：%s" % (p.returncode, tail[-400:])}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "对齐超时（>%ds）" % timeout}
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
    finally:
        pass

    try:
        if not os.path.isfile(outf):
            return {"ok": False, "message": "worker 没有写出结果"}
        with open(outf, "r", encoding="utf-8") as f:
            res = json.load(f)
    except Exception as e:
        return {"ok": False, "message": "读结果失败：%s" % e}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    res["elapsed"] = round(time.time() - t0, 2)
    return res


def map_items(items, orig, clean, max_len=8000):
    """把对齐条目的字符偏移从 `asr_text` 重映射到优化版 `asr_clean` 的坐标上。

    为什么需要它：强制对齐吃的是**转写原文**，所以每条 `[词, 起, 止, 字符偏移]`
    的偏移指的是 `asr_text` 里的位置。可用户在播放器里默认看到的是**优化版**
    （对话模型改过错别字、补过标点的版本）—— 两段文字哪怕只差一个标点，偏移也
    整体错位，所以之前干脆不给优化版做逐字高亮（面板里那个按钮是灰的）。

    优化版是「在原文上修错别字、补标点」，不是重写，所以两段文字绝大多数位置
    一一对应。这里用 difflib 求字符级对照表：

      equal 段        —— 直接平移（最常见的路径）
      replace/insert/delete 段 —— 落在块里的词按相对位置摊到目标区间上

    ★ 宁可不猜也不猜错：两段文字的相似度低于 0.55（说明「优化」其实重写了，
    不是修错字）直接返回 None，前端就照旧退回整行高亮 —— 一个指哪错哪的高亮
    比不高亮更糟，它会让整条时间轴都不可信。

    返回新的 items（每条形如 `[词, 起, 止, 新偏移]`）或 None。
    """
    if not items or not orig or not clean:
        return None
    if orig == clean:
        return [[w, s, e, int(o)] for w, s, e, o in items]
    if max(len(orig), len(clean)) > max_len:
        return None                     # 超长文本上 difflib 会退化成 O(n·m)，不值得
    try:
        import difflib
        sm = difflib.SequenceMatcher(None, orig, clean, autojunk=False)
        if sm.ratio() < 0.55:
            return None
        ops = sm.get_opcodes()
    except Exception:
        return None

    def conv(o):
        """orig 里的一个偏移 → clean 里的偏移。"""
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= o < i2:
                if tag == "equal":
                    return j1 + (o - i1)
                span = max(1, i2 - i1)
                t = (o - i1) / span
                return j1 + int(round(t * (j2 - j1)))
        return len(clean)               # 落在末尾之后（尾部被删掉的情况）

    n = len(clean)
    out = []
    prev = -1
    for it in items:
        try:
            off = int(it[3])
        except (TypeError, ValueError):
            continue
        no = max(0, min(n, conv(off)))
        if no > n:
            no = n
        # 偏移必须严格递增：`mvLineSpans` 是靠「下一个词的偏移」切片的，
        # 两个词撞到同一个位置就会切出空 span（那个字永远不会亮）。
        if no <= prev:
            no = min(n, prev + 1)
        if no >= n:
            break                       # 后面全部落到了 clean 之外，留着也没用
        prev = no
        out.append([it[0], it[1], it[2], no])
    return out or None


def mapped_items(path, db_path=None):
    """读库、算好映射、写回 `align_items_clean`。给 `/v1/detail` 兜底用。

    写回时把 `asr_clean_at` 一起记成 `align_items_clean_src`：重新优化一次
    （`asr_clean` 变了）就会因时间戳对不上而自动重算，不用另设失效逻辑。
    """
    import indexer as ix

    db = db_path or _db_path()
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        m = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        return None
    items = m.get("align_items")
    if not items:
        return None
    mapped = map_items(items, m.get("asr_text") or "", m.get("asr_clean") or "")
    patch = {"align_items_clean_src": m.get("asr_clean_at") or 0}
    clear = ()
    if mapped:
        patch["align_items_clean"] = mapped
    else:
        clear = ("align_items_clean",)  # 之前算出来过、现在又不成立了
    try:
        con = sqlite3.connect(db)
        try:
            ix._merge_meta(con, path, patch, clear=clear)
            con.commit()
        finally:
            con.close()
    except Exception:
        pass
    return mapped


def apply_result(path, res, db_path=None):
    """把对齐结果写进 items.meta。返回写入的 meta patch（便于日志）。"""
    import indexer as ix

    db = db_path or _db_path()
    chars = 0
    cur = {}
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            cur = json.loads(row[0])
            chars = len((cur.get("asr_text") or "").strip())
    except Exception:
        cur = {}

    if res.get("skipped"):
        patch = {"align_na": True, "align_skip": True,
                 "align_reason": (res.get("reason") or "")[:200],
                 "align_at": time.time()}
        clears = ("align_ok", "align_items", "align_model", "align_chars",
                  "align_items_clean", "align_items_clean_src")
    else:
        patch = {
            "align_ok": True,
            "align_chunk": int(res.get("chunk") or 0) or None,
            "align_chunks": int(res.get("chunks") or 0),
            "align_lang": res.get("lang") or "",
            "align_model": os.path.basename(
                (res.get("model_dir") or "").rstrip("/")) or "Qwen3-ForcedAligner-0.6B-8bit",
            "align_at": time.time(),
            "align_chars": chars,
            "align_items": res.get("items") or [],
        }
        clears = ("align_na", "align_skip", "align_reason")
        # 优化版的高亮偏移跟着这次对齐一起更新（没有优化版就不算，避免白存一份副本）。
        mapped = None
        if (cur.get("asr_clean") or "").strip():
            mapped = map_items(res.get("items") or [], cur.get("asr_text") or "",
                               cur.get("asr_clean") or "")
        if mapped:
            patch["align_items_clean"] = mapped
            patch["align_items_clean_src"] = cur.get("asr_clean_at") or 0
        else:
            clears = clears + ("align_items_clean", "align_items_clean_src")
    patch = {k: v for k, v in patch.items() if v is not None}

    con = sqlite3.connect(db)
    try:
        ix._merge_meta(con, path, patch, clear=clears)
        con.commit()
    finally:
        con.close()
    return patch


# ---------------------------------------------------------------------------
# 队列与统计（设置页要的「已对齐 / 未对齐」）
# ---------------------------------------------------------------------------

def _read_rows(db_path=None):
    import indexer as ix
    db = db_path or _db_path()
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT path, kind, meta FROM items"
            " WHERE kind IN ('audio','video') AND chunk_idx<?"
            " GROUP BY path, kind ORDER BY MIN(id)", (ix.AI_TAG_BASE,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return rows


def align_status(db_path=None, max_items=200):
    """返回 {total, aligned, pending, na, items:[{path,kind,name}]}。

    total 是「有转写文本、理论上可以对齐」的数量 —— 没转写的素材归转写队列管，
    算进对齐的分母只会让用户以为漏了一堆。所以 total = aligned + na + pending。
    """
    rows = _read_rows(db_path)
    pend = []
    aligned = 0
    na = 0
    for p, k, mstr in rows:
        try:
            m = json.loads(mstr or "{}")
        except Exception:
            m = {}
        txt = (m.get("asr_text") or "").strip()
        if not txt:
            continue
        if m.get("align_na"):
            na += 1
            continue
        # ★ 字数对不上 = 转写被重做过，旧时间轴已经失效，重新排队。
        if m.get("align_ok") and int(m.get("align_chars") or 0) == len(txt):
            aligned += 1
            continue
        pend.append({"path": p, "kind": k, "name": os.path.basename(p)})
    total = aligned + na + len(pend)
    return {"total": total, "aligned": aligned, "na": na,
            "pending": len(pend), "items": pend[:max_items],
            "pending_list": pend}


def pending_list(db_path=None):
    """给后台队列用的待对齐列表（按入库顺序）。"""
    return align_status(db_path)["pending_list"]


def model_dir(data_dir=None):
    """找对齐模型目录。找不到返回 None（开关开了但还没下）。"""
    try:
        import model_dl
        if data_dir is None:
            import paths as P
            data_dir = P.DATA_DIR
        return model_dl.find_model(data_dir, model_dl.SPEC_ALIGNER)
    except Exception:
        return None


def available(data_dir=None):
    """对齐能力现在能不能用：有模型目录 + venv 里有 mlx_audio。"""
    if not model_dir(data_dir):
        return False, "对齐模型未下载"
    try:
        import mlx_audio  # noqa: F401
    except Exception as e:
        return False, "缺少 mlx_audio（%s）" % e
    return True, ""


if __name__ == "__main__":
    sys.exit(main())
