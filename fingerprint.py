#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
音频指纹（Chromaprint / AcoustID）—— 精确匹配「同一段音频」。

用途：用户上传一段音频片段 / 麦克风录音，找出它出自哪个视频/音频文件的第几秒。

实现：
  - 用 fpcalc（chromaprint CLI，已 bundle 进 app 的 bin/）提取指纹
  - 索引时：对整个文件提取指纹，存入 sqlite
  - 检索时：对查询片段提取指纹，与库内指纹做「子序列匹配」，返回匹配文件 + 时间偏移

指纹比对算法：
  Chromaprint 指纹是 int32 序列（每项编码约 0.123 秒的频谱特征）。
  子序列匹配用「滑动窗口 + 位错误率」，能容忍少量噪声/编码误差。
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "bin")

# 指纹存储：单独一个表（避免与语义向量表混在一起）
import paths as _paths          # 注意：本文件内 `P` 被用作局部变量，故换个别名
FP_DB = _paths.FP_DB


def _resolve_fpcalc() -> str:
    """优先用 app 内置的 fpcalc，找不到退回系统 PATH。"""
    local = os.path.join(BIN_DIR, "fpcalc")
    if os.path.exists(local):
        return local
    return "fpcalc"


def _fpcalc(path: str, raw: bool = True) -> dict:
    """调用 fpcalc 提取指纹。返回 {duration, fingerprint:[int...]}"""
    fpcalc = _resolve_fpcalc()
    args = [fpcalc]
    if raw:
        args += ["-raw"]
    # ★关键：fpcalc 的 -length 默认只有 120 秒！不显式放开的话，任何超过 2 分钟
    # 的音视频都只会为「前 2 分钟」建指纹（实测 207 秒的歌只存了 120 秒的指纹，
    # 948 项 vs 放开后的 1650 项），用户播放 2 分钟之后的段落永远匹配不到。
    args += ["-length", "86400"]
    args += ["-json", path]
    try:
        r = subprocess.run(args, capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", errors="replace")[:300])
        d = json.loads(r.stdout.decode("utf-8", errors="replace"))
        return {"duration": float(d.get("duration", 0)),
                "fingerprint": d.get("fingerprint", [])}
    except Exception as e:
        raise RuntimeError(f"fpcalc 失败: {e}")


def _init_db():
    con = sqlite3.connect(FP_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fingerprints(
        path TEXT PRIMARY KEY,
        kind TEXT,
        duration REAL,
        fingerprint TEXT,      -- json 数组
        mtime REAL,
        indexed_at REAL
    )""")
    con.commit()
    return con


def _hamming(a: int, b: int) -> int:
    """两个 int32 的位错误数（Hamming 距离）。"""
    return bin(a ^ b).count("1")


# 查询片段至少要够长、够有变化，指纹才有区分度。
# 实测：3 秒的噪声 / 纯正弦 / 静音，fpcalc 会给出**完全相同的常量指纹**
# （如 [627964279, 627964279, 627964279]）——这种指纹拿去滑动匹配，只要库里
# 任何一处出现过该常量，就是 0 位错误、相似度 1.000 的假阳性。必须先挡掉。
MIN_QUERY_ITEMS = 3          # 实测 3 秒真歌片段→3 项（互不相同）已能明确区分；
                             # 再短就连 3 项都凑不齐，没有区分度
MIN_DISTINCT_RATIO = 0.5     # 去重后至少占一半，否则视为「平稳信号」


def query_quality(query_path: str):
    """检查查询片段的指纹是否可用。

    返回 (fpcalc 结果 dict, 不可用原因 or None)。原因非空时调用方应直接当
    「没匹配到」处理，并可以把原因告诉用户（比默默返回一屏假阳性要好）。
    """
    if not os.path.exists(query_path):
        return {}, "文件不存在"
    try:
        q = _fpcalc(query_path)
    except Exception as e:
        return {}, f"无法读取这段录音：{e}"
    qfp = q.get("fingerprint") or []
    if not qfp:
        return {}, "这段录音太短或没有声音，提取不到声学指纹"
    if len(qfp) < MIN_QUERY_ITEMS:
        return {}, "录音太短了，至少要哼 3 秒左右"
    if len(set(qfp)) < max(2, int(len(qfp) * MIN_DISTINCT_RATIO)):
        return {}, "这段录音太平稳（几乎没有音高变化），提取到的指纹没有区分度"
    return q, None


def _bit_error_rate(seq_a, seq_b) -> float:
    """两段等长指纹的平均位错误率（0~1，越小越像）。"""
    if not seq_a or not seq_b:
        return 1.0
    n = min(len(seq_a), len(seq_b))
    errs = sum(_hamming(seq_a[i], seq_b[i]) for i in range(n))
    return errs / (n * 32.0)


def index_file(path: str, kind: str, mtime: float):
    """为单个音频/视频文件建立指纹并入库。"""
    if not os.path.exists(path):
        return False
    try:
        d = _fpcalc(path)
        if not d.get("fingerprint"):
            return False
        con = _init_db()
        con.execute(
            "INSERT OR REPLACE INTO fingerprints(path,kind,duration,fingerprint,mtime,indexed_at)"
            " VALUES (?,?,?,?,?,?)",
            (path, kind, d["duration"], json.dumps(d["fingerprint"]),
             mtime, __import__("time").time()))
        con.commit()
        con.close()
        return True
    except Exception as e:
        print(f"  [指纹失败] {os.path.basename(path)}: {e}")
        return False


def has_fingerprint(path: str) -> bool:
    """查询指纹库是否已有该文件的指纹。"""
    if not os.path.exists(FP_DB):
        return False
    con = _init_db()
    row = con.execute("SELECT 1 FROM fingerprints WHERE path=?", (path,)).fetchone()
    con.close()
    return row is not None


def remove_file(path: str):
    con = _init_db()
    con.execute("DELETE FROM fingerprints WHERE path=?", (path,))
    con.commit()
    con.close()


def all_paths() -> list:
    """指纹库里所有记录的路径。"""
    if not os.path.exists(FP_DB):
        return []
    con = _init_db()
    try:
        return [r[0] for r in con.execute("SELECT path FROM fingerprints").fetchall()]
    finally:
        con.close()


def stale_paths() -> list:
    """文件已经不在磁盘上的指纹记录。

    这类记录很容易残留：文件夹**改名或移动**之后，重建索引会把 index.db 的
    items 换成新路径（旧路径变成「不存在」→ 被 GC 收走），但指纹表是按 path
    插入、按 path 删除的，老那条没有任何人去清它。

    后果是「以音搜素材」会**同时**返回新路径和老路径两条 —— 同一个文件名出现
    两次，其中一条标着「文件已移动」。用户点了「清理失效记录」也没用，
    因为以前只清了 index.db。
    """
    return [p for p in all_paths() if not os.path.exists(p)]


def gc_remove(paths) -> int:
    """按路径删指纹记录，连带清掉引用它们的 song_links。返回删掉几条。

    song_links 存的是 (a, b) 文件对，任一端没了这条关系就作废 —— 否则
    「同一首歌」那一栏会指向一个已经不存在的文件。
    """
    paths = [p for p in (paths or []) if p]
    if not paths or not os.path.exists(FP_DB):
        return 0
    con = _init_db()
    try:
        n = 0
        for p in paths:
            cur = con.execute("DELETE FROM fingerprints WHERE path=?", (p,))
            n += cur.rowcount or 0
        try:
            ph = ",".join("?" * len(paths))
            con.execute(
                "DELETE FROM song_links WHERE a IN (%s) OR b IN (%s)" % (ph, ph),
                list(paths) + list(paths))
        except sqlite3.OperationalError:
            pass                     # song_links 还没建过（没跑过 detect_links）
        con.commit()
        return n
    finally:
        con.close()


def _best_offset(full_fp, query_fp, min_segment=8):
    """在 full_fp 里找 query_fp 的最佳匹配位置，返回 (offset, bit_error_rate)。

    offset = query 片段在 full 里的起始指纹序号（乘以每帧时长≈0.123s 可得秒）。
    用滑动窗口在 full 上找与 query 位错误率最低的窗口。
    """
    full = full_fp
    q = query_fp
    if not full or not q:
        return None
    qlen = len(q)
    if qlen > len(full):
        # 查询比库长（不常见），退化为头对齐比对
        return (0, _bit_error_rate(full, q))
    best_off, best_err = 0, 1.0
    # 步长 1，逐位滑动（指纹通常几百~几千项，性能可接受）
    for off in range(0, len(full) - qlen + 1):
        err = _bit_error_rate(full[off:off + qlen], q)
        if err < best_err:
            best_err, best_off = err, off
    return (best_off, best_err)


# --------------------------------------------------------------- 鲁棒匹配
# 为什么需要它（2026-10-01 实测依据）：
#   真实链路「外放原曲 → 麦克风录音」会被房间混响与麦克风频响毁掉，逐位严格
#   比对只能到 0.61~0.74；而拿完全无关的素材（说话/哼唱/房间噪声）去比也能擦到
#   0.71~0.79 —— 两者重叠，**单看分数根本分不开**。对策分两层：
#     1) 逐帧容忍：允许指纹在时间轴上糊开 ±TOL 帧（混响会让相邻帧互相污染）；
#     2) 多窗口投票：把录音切成若干 4 秒窗口（步长 2 秒），每窗各自选 top1，
#        要求同一个文件在 ≥2/3 的窗口里都排第一。
#   实测：真阳性一致度 3/3、3/4；假阳性（说话 1/3、哼唱 1/2、房间噪声 1/2）
#   全部落选 —— 这才把真假分开。
WIN_SEC = 4.0            # 投票窗口时长（秒）
WIN_STEP = 2.0           # 窗口步长（秒），50% 重叠
TOL = 2                  # 逐帧容忍 ±N 帧
# chromaprint 的帧步长是固定的 ≈0.1238 秒；不要用「时长/帧数」去推算——文件头
# 时长与实际解码长度对不上时（实测 hum_real.wav 报 7.65s 却只有 40 帧）会把窗口
# 切错。窗口大小直接按帧数定死。
LIB_FRAME = 0.1238
VOTE_RATIO = 2.0 / 3.0   # base 一致性：≥2/3 的窗口要对齐到同一位置
UNANIMOUS = True         # 冠军必须在**每一个**窗口里都排第一（实测这一条把假阳性全挡掉）
SCORE_FLOOR = 0.70       # 冠军的平均相似度下限
MIN_WINDOWS = 2          # 至少要切出 2 个投票窗口，否则没有投票可言
SHORT_OK_SCORE = 0.85    # 只有 1 个窗口时（录音太短）必须高到这个分才敢认
MAX_WINDOWS = 12         # 长录音也不必切太多窗（12 窗已足够投票）
# 实测 fpcalc 的帧数满足 n ≈ (时长 − 2.7s)/0.1238 —— 开头约 2.7 秒是算法预热，
# 不出指纹。所以每帧固定 0.1238 秒，但「8 秒的录音只有 43 帧」，窗口必须按
# 查询长度自适应，否则 4 秒窗在短录音上只剩 1 个窗、投票就失效了。
MAX_WIN_FRAMES = 32      # ≈4 秒
MIN_WIN_FRAMES = 10      # ≈1.25 秒
# base = 库内偏移 − 窗口起点。真匹配时每窗算出的 base 应当恒定不变；假阳性是
# 「哪首歌碰巧分了最高」的随机事件，base 会乱跳。这是比分数更硬的判据。
BASE_TOL = 4             # 允许 ±4 帧（≈0.5 秒）的抖动

_POPCOUNT = None


def _popcount():
    global _POPCOUNT
    if _POPCOUNT is None:
        import numpy as np
        _POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    return _POPCOUNT


def _match_np(full, q, tol: int = TOL):
    """带逐帧容忍的滑动匹配（numpy 向量化）。

    full/q 为 uint32 数组。返回 (相似度 0~1, 最佳偏移帧号)。
    err(off) = mean_i min_{d∈[-tol,tol]} popcount(full[off+i+d] ^ q[i]) / 32
    """
    import numpy as np
    L, N = len(q), len(full)
    if L < 3 or N < L + tol:
        return 0.0, 0
    win = np.lib.stride_tricks.sliding_window_view(full, L)
    x = np.ascontiguousarray(win ^ q[None, :])
    P0 = _popcount()[x.view(np.uint8)].reshape(win.shape[0], L, 4).sum(axis=2).astype(np.int16)
    P = P0.copy()
    M = P0.shape[0]
    for d in range(1, tol + 1):
        a = np.full_like(P0, 32); a[d:] = P0[:M - d]; np.minimum(P, a, out=P)
        b = np.full_like(P0, 32); b[:M - d] = P0[d:]; np.minimum(P, b, out=P)
    err = P.mean(axis=1) / 32.0
    k = int(err.argmin())
    return 1.0 - float(err[k]), k


def search_robust(query_path: str, top_k: int = 10,
                  kinds: tuple | None = ("audio", "video")):
    """鲁棒指纹检索（多窗口投票）。返回 (results, diag)。

    results 为空 = 「不敢确定，请调用方回落到 ASR/语义检索」。
    diag 里带着投票明细，供日志/前端展示用。
    """
    import numpy as np
    import math
    diag = {"windows": 0, "need": 0, "votes": {}, "scores": {}, "best": None,
            "floor": SCORE_FLOOR, "tol": TOL}
    if not os.path.exists(query_path):
        return [], diag
    q, _why = query_quality(query_path)
    if not q:
        return [], diag
    qfp = q.get("fingerprint") or []
    if len(qfp) < 8:
        return [], diag
    qa = np.asarray(qfp, dtype=np.uint32)
    qdur = float(q.get("duration") or 0) or len(qa) * LIB_FRAME
    n = len(qa)
    win = min(MAX_WIN_FRAMES, max(MIN_WIN_FRAMES, n // 2))
    if n <= win:
        starts = [0]
    else:
        step = max(4, win // 2)
        if (n - win) // step + 1 > MAX_WINDOWS:
            step = max(step, (n - win) // (MAX_WINDOWS - 1))
        starts = list(range(0, n - win + 1, step))

    con = _init_db()
    if kinds:
        ph = ",".join("?" * len(kinds))
        rows = con.execute(
            f"SELECT path,kind,duration,fingerprint FROM fingerprints WHERE kind IN ({ph})",
            tuple(kinds)).fetchall()
    else:
        rows = con.execute("SELECT path,kind,duration,fingerprint FROM fingerprints").fetchall()
    con.close()
    lib = []
    for path, kind, dur, fpjson in rows:
        try:
            arr = np.asarray(json.loads(fpjson), dtype=np.uint32)
        except Exception:
            continue
        if len(arr) >= 8:
            lib.append((path, kind, float(dur or 0), arr))
    if not lib:
        return [], diag

    votes, scores, offs, bases = {}, {}, {}, {}
    for s in starts:
        qw = qa[s:s + win]
        best_path, best_sc, best_off = None, -1.0, 0
        for path, kind, d, arr in lib:
            sc, off = _match_np(arr, qw, TOL)
            scores.setdefault(path, []).append(sc)
            if sc > best_sc:
                best_sc, best_path, best_off = sc, path, off
        if best_path is not None:
            votes[best_path] = votes.get(best_path, 0) + 1
            offs[best_path] = (best_off, best_sc)
            # base = 库内偏移 − 窗口在查询里的起点：真匹配时它应当恒定
            bases.setdefault(best_path, []).append(best_off - s)

    K = len(starts)
    need = max(1, math.ceil(K * VOTE_RATIO))
    diag["windows"] = K
    diag["need"] = need
    diag["votes"] = {os.path.basename(p): v for p, v in sorted(votes.items(), key=lambda kv: -kv[1])}
    if not votes:
        return [], diag

    def _mean(p):
        return sum(scores[p]) / max(1, len(scores[p]))

    path = max(votes.items(), key=lambda kv: (kv[1], _mean(kv[0])))[0]
    mean_sc = _mean(path)
    bs = bases.get(path, [0])
    med = float(np.median(bs)) if bs else 0.0
    spread = max(bs) - min(bs) if bs else 999
    consist = sum(1 for b in bs if abs(b - med) <= BASE_TOL)
    need_base = max(1, math.ceil(len(bs) * VOTE_RATIO))
    diag["best"] = (os.path.basename(path), votes[path], round(mean_sc, 4),
                    "consist", f"{consist}/{len(bs)}", "spread", spread)
    diag["scores"] = {os.path.basename(p): round(_mean(p), 4)
                      for p in sorted(votes, key=lambda p: -_mean(p))}
    if K < MIN_WINDOWS:
        # 太短、切不出第二个窗口 —— 没有投票保护，只有分数高到接近数字直连才认
        if mean_sc < SHORT_OK_SCORE:
            return [], diag
    elif (votes[path] < (K if UNANIMOUS else need)
            or mean_sc < SCORE_FLOOR
            or consist < need_base):
        return [], diag

    kind = next(k for p, k, d, a in lib if p == path)
    dur = next(d for p, k, d, a in lib if p == path)
    arr = next(a for p, k, d, a in lib if p == path)
    lframe = LIB_FRAME
    off = offs[path][0]
    return [{
        "path": path, "kind": kind, "score": round(mean_sc, 4),
        "offset_sec": round(off * lframe, 2), "duration": round(dur, 2),
        "query_duration": round(qdur, 2),
        "votes": votes[path], "windows": K,
    }][:top_k], diag


# ── 「同一首歌」关联 ────────────────────────────────────────────────────
# 指纹是**同一段录音**级别的匹配。实测：元宵喜乐（FR).mp4 里用了 忆_FR.mp3 的
# 第 6~19 秒当背景音乐，而用户上传的是该曲第 84.25~91.55 秒——同一首歌的两段不同
# 录音，双向指纹匹配都只有 0.69，chroma 相关 0.072（同段是 0.476）。所以「拿歌曲
# 中段去搜、想搜出用了这首歌开头的视频」在指纹层面**原理上做不到**，伴奏分离也救不
# 回来（实测 demucs 分离后分数反而从 0.751 降到 0.727）。
# 但「这个视频用了那首歌」本身是可以自动建起来的：把源文件的指纹切成小窗，每个窗
# 各自去目标曲里找最佳位置 —— 真关联的**底对齐（目标位置 − 窗口起点）恒定**，
# 假关联则乱跳。于是「多窗投票 + 底对齐一致」就能把关系挖出来。
LINK_WIN_FRAMES = 40      # 源文件切窗长度 ≈5 秒
LINK_STEP_FRAMES = 24     # 切窗步长 ≈3 秒
LINK_MIN_VOTES = 3        # 至少这么多个窗指向同一目标且底对齐一致
LINK_OFFSET_TOL = 3       # 底对齐允许 ±3 帧（≈0.37 秒）
LINK_SCORE_FLOOR = 0.70   # 这些窗的平均相似度下限
LINK_MARGIN = 0.05        # ★ 簇内均分必须比「全部窗口的均分」高出这么多
#   这一条是实测加出来的：本库三首歌同专辑、结构相近，**每一对**都能凑出 3 个
#   底对齐一致的窗口（0.649~0.719），光看「有一致簇」全是假阳性。但真关联的簇
#   显著优于全局（忆_FR→元宵 +0.071、元宵→忆_FR +0.092），假关联则和全局持平
#   （最大 +0.027）。0.05 落在两者中间。
LINK_MIN_FRAMES = 40      # 目标指纹太短就没得比
LINK_MAX_WINDOWS = 120    # 单对比较的窗口上限，防长文件把关联扫描拖死


def _link_pair(src_fp, dst_fp, tol: int = TOL):
    """src 里是否用了 dst 的音乐？命中返回 (票数, 底对齐帧, 簇均分, 超出全局)。"""
    import numpy as np
    n = len(src_fp)
    if n < LINK_WIN_FRAMES or len(dst_fp) < LINK_MIN_FRAMES:
        return None
    starts = list(range(0, n - LINK_WIN_FRAMES + 1, LINK_STEP_FRAMES))
    if len(starts) > LINK_MAX_WINDOWS:
        starts = starts[:: max(1, len(starts) // LINK_MAX_WINDOWS)]
    scores, bases = [], []
    for i in starts:
        sc, off = _match_np(dst_fp, src_fp[i:i + LINK_WIN_FRAMES], tol=tol)
        scores.append(sc)
        bases.append(off - i)
    scores = np.asarray(scores, dtype=np.float64)
    bases = np.asarray(bases, dtype=np.int64)
    gmean = float(scores.mean())
    best = None
    for b0 in bases:
        m = np.abs(bases - b0) <= LINK_OFFSET_TOL
        if m.sum() < LINK_MIN_VOTES:
            continue
        cmean = float(scores[m].mean())
        cand = (int(m.sum()), float(np.median(bases[m])), cmean, cmean - gmean)
        if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[3] > best[3]):
            best = cand
    if best and best[2] >= LINK_SCORE_FLOOR and best[3] >= LINK_MARGIN:
        return best
    return None


def detect_links(verbose: bool = False):
    """扫全库音视频，找出「A 里用了 B 的音乐」的关系。

    只比指纹切片，不解码音频，所以很快（几十个素材也是秒级）。
    返回 [(a_path, b_path, votes, base_frames, mean_score), ...]，a 里含 b 的音乐。
    """
    import numpy as np
    con = _init_db()
    # 这张表是纯派生缓存，每次整体重建；DROP 一下顺便兼容旧版本的表结构
    con.execute("DROP TABLE IF EXISTS song_links")
    con.execute("""CREATE TABLE song_links(
        a TEXT, b TEXT, votes INTEGER, base_frames INTEGER, score REAL,
        margin REAL, a_kind TEXT, b_kind TEXT, built_at REAL, PRIMARY KEY (a, b)
    )""")
    rows = con.execute(
        "SELECT path, kind, fingerprint FROM fingerprints WHERE kind IN ('audio','video')"
    ).fetchall()
    items = []
    for p, k, f in rows:
        try:
            arr = np.asarray(json.loads(f), dtype=np.uint32)
        except Exception:
            continue
        if len(arr) >= LINK_MIN_FRAMES:
            items.append((p, k, arr))
    found = []
    for i, (pa, ka, fa) in enumerate(items):
        for pb, kb, fb in items:
            if pa == pb:
                continue
            hit = _link_pair(fa, fb)
            if hit:
                votes, base, score, margin = hit
                found.append((pa, pb, votes, int(base), round(score, 4),
                              round(margin, 4), ka, kb))
                if verbose:
                    print("    %s  ←含←  %s   票%d  底对齐%+d帧  簇均分%.3f  超出全局%+.3f"
                          % (os.path.basename(pa), os.path.basename(pb),
                             votes, base, score, margin))
    now = time.time()
    con.execute("DELETE FROM song_links")
    con.executemany(
        "INSERT OR REPLACE INTO song_links"
        "(a,b,votes,base_frames,score,margin,a_kind,b_kind,built_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [(a, b, v, bf, sc, mg, ka, kb, now)
         for a, b, v, bf, sc, mg, ka, kb in found],
    )
    con.commit()
    con.close()
    return found


def get_fp_info(path: str):
    """取某个已建指纹文件的 (kind, duration)；没有则 (None, None)。"""
    try:
        con = _init_db()
        row = con.execute("SELECT kind, duration FROM fingerprints WHERE path=?",
                          (path,)).fetchone()
        con.close()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        return (None, None)


def related_paths(paths):
    """给定命中路径，返回与它们「同一首歌」的其它素材 {path: [证据, ...]}。"""
    if not paths:
        return {}
    try:
        con = _init_db()
        rows = con.execute("SELECT a,b,votes,base_frames,score FROM song_links").fetchall()
        con.close()
    except Exception:
        return {}
    want = set(paths)
    out = {}
    for a, b, votes, base, score in rows:
        for src, dst in ((a, b), (b, a)):
            if src in want and dst not in want:
                out.setdefault(dst, []).append(
                    {"via": src, "votes": votes,
                     "offset_sec": round(base * LIB_FRAME, 2), "score": score})
    return out


def search(query_path: str, threshold: float = 0.30, top_k: int = 10,
           kinds: tuple | None = ("audio", "video")):
    """用音频片段检索匹配的文件。

    threshold: 位错误率阈值（0~1，越小越严格；0.30 表示允许 30% 位错误）。
    kinds:     限定素材类型，默认只查音频与视频——指纹只对这两类有意义，
               限定后未来即使给别的类型建了指纹，也不会悄悄扩大检索范围。
               传 None 表示不限类型。
    返回 [{path, kind, score(相似度 0~1), offset_sec, duration}] 按相似度降序。
    """
    if not os.path.exists(query_path):
        return []
    # 短录音 / 平稳信号会得到常量指纹，拿它去滑动匹配必然擦出 1.000 的假阳性
    q, _why = query_quality(query_path)
    if not q:
        return []
    qfp = q.get("fingerprint") or []
    con = _init_db()
    if kinds:
        ph = ",".join("?" * len(kinds))
        rows = con.execute(
            f"SELECT path,kind,duration,fingerprint FROM fingerprints WHERE kind IN ({ph})",
            tuple(kinds)).fetchall()
    else:
        rows = con.execute("SELECT path,kind,duration,fingerprint FROM fingerprints").fetchall()
    con.close()

    FRAME = 0.123  # chromaprint 每项约 0.123 秒
    results = []
    for path, kind, dur, fpjson in rows:
        try:
            full = json.loads(fpjson)
        except Exception:
            continue
        if not full:
            continue
        m = _best_offset(full, qfp)
        if not m:
            continue
        off, err = m
        similarity = 1.0 - err      # 位错误率 → 相似度
        if err > threshold:
            continue
        results.append({
            "path": path,
            "kind": kind,
            "score": round(similarity, 4),
            "offset_sec": round(off * FRAME, 2),
            "duration": round(dur, 2),
            "query_duration": round(q.get("duration", 0), 2),
        })
    results.sort(key=lambda x: -x["score"])
    return results[:top_k]
