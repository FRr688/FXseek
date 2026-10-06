#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
快速检索层 —— 与 WeMM 深度语义检索配合，减少模型调用与系统资源占用。

设计：三级递进检索（够用即止）
  ① L0 元数据/文件名精确匹配（0 模型调用，微秒级）
      - 文件名/路径包含查询词
      - 类型、扩展名筛选手
  ② L1 向量快速检索（复用已编码的查询向量缓存 + numpy 矩阵运算）
      - 查询向量 LRU 缓存（常见查询重复搜时零模型开销）
      - numpy 批量余弦（比纯 Python 快 20-50 倍）
  ③ L2 WeMM 深度语义（仅在 L0/L1 结果不足或用户要求时启用）
      - 查询扩展 + 多指令融合，精度最高

优点：
  * 简单查询（文件名/常见词）完全不碰模型 → 省 CPU/GPU
  * 向量检索用 numpy + 缓存 → 毫秒级
  * 仅在必要时加载/调用 WeMM → 大幅降低常驻压力
"""
import hashlib
import json
import math
import os
import sqlite3
import struct
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 查询向量 LRU 缓存 ----------------
_QRY_CACHE = {}          # key -> (vec_list, ts)
_QRY_CACHE_MAX = 128
_QRY_TTL = 3600          # 1 小时


def _qkey(query: str, instruction: str) -> str:
    return hashlib.sha1(f"{query}|{instruction}".encode()).hexdigest()[:20]


def qcache_get(query: str, instruction: str):
    k = _qkey(query, instruction)
    v = _QRY_CACHE.get(k)
    if not v:
        return None
    vec, ts = v
    if time.time() - ts > _QRY_TTL:
        _QRY_CACHE.pop(k, None)
        return None
    return vec


def qcache_put(query: str, instruction: str, vec):
    if len(_QRY_CACHE) >= _QRY_CACHE_MAX:
        oldest = min(_QRY_CACHE.items(), key=lambda kv: kv[1][1])[0]
        _QRY_CACHE.pop(oldest, None)
    _QRY_CACHE[_qkey(query, instruction)] = (vec, time.time())


def qcache_stats():
    return {"entries": len(_QRY_CACHE), "max": _QRY_CACHE_MAX}


# ---------------- 向量矩阵缓存（numpy） ----------------
_MAT = {"stamp": None, "ids": None, "paths": None, "kinds": None, "mat": None}


def _db_stamp(db_path):
    try:
        st = os.stat(db_path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_matrix(db_path):
    """把全部向量读成 numpy 矩阵（带缓存，索引不变时不重读）。"""
    import numpy as np
    stamp = _db_stamp(db_path)
    if _MAT["stamp"] == stamp and _MAT["mat"] is not None:
        return _MAT
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT id,path,kind,chunk_idx,dim,vec FROM items").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    ids, paths, kinds, chunks, vecs, dim = [], [], [], [], [], 0
    for rid, path, kind, cidx, d, blob in rows:
        if not blob:
            continue
        v = np.frombuffer(blob, dtype="<f4")
        if dim == 0:
            dim = len(v)
        if len(v) != dim:
            continue
        vecs.append(v)
        ids.append(rid); paths.append(path); kinds.append(kind); chunks.append(cidx)

    mat = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype="<f4")
    _MAT.update({"stamp": stamp, "ids": ids, "paths": paths, "kinds": kinds,
                 "chunks": chunks, "mat": mat})
    return _MAT


# 口语里的虚词/语气词：命中片段必须含 ≥3 个实词字才算数，避免「帮我找一下」这种
# 套话在转写文本里蒙到一个 5 字片段就被当成命中。
STOP_CHARS = set("的一只在是了和与及于把被为地得着过有上下了里外中个这那我你他她它"
                 "们吗呢吧啊呀嘛哦很就都也还要会能可以之其找想请给帮")


def content_run(q: str, txt: str, need: int = 5) -> int:
    """q 在 txt（ASR 转写文本）里的最长连续命中片段长度；<need 或实词不足返回 0。

    单一 need 长度扫一遍，命中后向前延长到真实片段长度，所以既快又准。
    这样「帮我找一下手心里的汗珠那首歌」也能命中歌词「…手心里的汗珠在闪烁…」。
    """
    if not q or not txt or len(q) < need or len(txt) < need:
        return 0
    grams = {q[i:i + need] for i in range(len(q) - need + 1)}
    n, m = len(txt), len(q)
    for i in range(n - need + 1):
        seg = txt[i:i + need]
        if seg not in grams:
            continue
        k = q.index(seg)
        j = i + need
        while j < n and (k + (j - i)) < m and txt[j] == q[k + (j - i)]:
            j += 1
        run = txt[i:j]
        if len({c for c in run if c not in STOP_CHARS}) >= 3:
            return j - i
    return 0


# ---------------- L0 元数据匹配 ----------------
def _kind_sql(kind, col="kind"):
    """把 kind（单个字符串，或字符串序列）转成 (SQL 片段, 参数)。

    片段总以 " AND " 开头，便于追加到已有 WHERE 之后；None / 空序列返回 ("", [])。
    """
    if not kind:
        return "", []
    ks = list(kind) if isinstance(kind, (list, tuple, set, frozenset)) else [kind]
    ks = [k for k in ks if k]
    if not ks:
        return "", []
    return " AND %s IN (%s)" % (col, ",".join("?" * len(ks))), ks


def _kind_set(kind):
    """归一成集合；不过滤（None/空）时返回 None。"""
    if not kind:
        return None
    ks = set(kind) if isinstance(kind, (list, tuple, set, frozenset)) else {kind}
    ks.discard("")
    return ks or None


def search_lexical(query: str, db_path: str, kind=None, limit: int = 200):
    """文件名/路径包含匹配（零模型调用）。

    返回 [(score, path, kind, chunk_idx, meta)]，按匹配质量排序。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    con = sqlite3.connect(db_path)
    sql = "SELECT path,kind,chunk_idx,meta,mtime FROM items WHERE 1=1"
    _kf, _ka = _kind_sql(kind)
    sql += _kf
    try:
        rows = con.execute(sql, _ka).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    # 去重到文件粒度，保留最大匹配分
    agg = {}
    for path, k, idx, meta, mtime in rows:
        if idx and idx >= 9000:      # 跳过 AI 标签槽
            continue
        name = os.path.basename(path).lower()
        full = path.lower()
        score = 0.0
        if q == name or q == os.path.splitext(name)[0]:
            score = 1.0
        elif q in name:
            score = 0.85 + min(0.1, len(q) / max(1, len(name)))
        elif q in full:
            score = 0.55
        if score <= 0:
            continue
        try:
            m = json.loads(meta or "{}")
        except Exception:
            m = {}
        if path not in agg or score > agg[path][0]:
            agg[path] = (score, path, k, idx or 0, m)

    # 内容关键词匹配：文件名没命中时，再扫一遍音视频的 ASR 转写文本（歌词/台词）。
    # 用精确子串 + 长公共片段，而不是靠向量 —— 短查询搜长歌词时向量会把无关文档
    # 排到前面（实测搜「手心里的汗珠」原文歌词只有 0.449，还不如一个空文档 0.507）。
    # 给 0.86~0.88 是为了触发 L0 短路（indexer.search 里 lex[0][0] >= 0.85 直接返回），
    # 这样精确歌词/台词检索是零模型调用、毫秒级返回。
    _ks = _kind_set(kind)
    if len(q) >= 2 and (_ks is None or (_ks & {"audio", "video"})):
        _kf2, _ka2 = _kind_sql(kind)
        if _ks is None:
            # 不限类别时，ASR 文本只可能存在于音视频里
            sql2 = ("SELECT path,kind,chunk_idx,meta,mtime FROM items "
                    "WHERE kind IN ('audio','video') AND meta LIKE '%asr_text%'")
            args2 = ()
        else:
            sql2 = ("SELECT path,kind,chunk_idx,meta,mtime FROM items "
                    "WHERE meta LIKE '%asr_text%'" + _kf2)
            args2 = tuple(_ka2)
        rows2 = []
        try:
            con2 = sqlite3.connect(db_path)
            try:
                rows2 = con2.execute(sql2, args2).fetchall()
            finally:
                con2.close()
        except sqlite3.OperationalError:
            rows2 = []
        for path, k, idx, meta, mtime in rows2:
            if idx and idx >= 9000:
                continue
            try:
                m = json.loads(meta or "{}")
            except Exception:
                m = {}
            txt = (m.get("asr_text") or "").lower()
            if not txt:
                continue
            if q in txt:
                score = 0.88          # 精确命中 → 触发 L0 短路
            elif content_run(q, txt):
                score = 0.84          # 近似命中 → 排前面但不短路，其余结果照常参与
            else:
                continue
            if path not in agg or score > agg[path][0]:
                agg[path] = (score, path, k, idx or 0, m)

    out = sorted(agg.values(), key=lambda x: -x[0])
    return out[:limit]


# ---------------- L1 向量快速检索 ----------------
def search_vector(qvec, db_path: str, kind=None, limit: int = 300, with_chunk=False):
    """numpy 批量余弦检索（不含模型调用）。

    with_chunk=True 时，额外回填「最匹配的那一帧/分块」的 chunk_idx 与 meta，
    用于让视频封面/起播点定位到最相似的画面。
    """
    import numpy as np
    data = load_matrix(db_path)
    mat = data["mat"]
    if mat.size == 0 or qvec is None:
        return []
    q = np.asarray(qvec, dtype="<f4")
    if q.shape[0] != mat.shape[1]:
        return []

    sims = mat @ q                      # 向量已 L2 归一，点积即余弦
    order = np.argsort(-sims)

    con = sqlite3.connect(db_path)
    # 取每个文件的 chunk 明细（用于定位最匹配帧）。
    # 注意：必须按 chunk_idx 建字典，不能靠列表下标对齐 —— 矩阵行的顺序是
    # items 的 rowid 顺序（AI 打标时 chunk 0 被 INSERT OR REPLACE 重写过），
    # 和这条 SQL 的返回顺序并不一致，早先用下标对齐导致定位错帧。
    chunk_map = {}
    if with_chunk:
        try:
            for path, cidx, meta in con.execute(
                    "SELECT path, chunk_idx, meta FROM items WHERE chunk_idx < 9000"):
                try:
                    md = json.loads(meta or "{}")
                except Exception:
                    md = {}
                chunk_map.setdefault(path, {})[cidx] = md
        except sqlite3.OperationalError:
            pass

    # 建 path -> 矩阵行号（该文件的各个向量行）
    path_rows = {}
    for n, p in enumerate(data["paths"]):
        path_rows.setdefault(p, []).append(n)

    _ks = _kind_set(kind)
    _chunks = data.get("chunks") or []
    out = {}
    for i in order:
        i = int(i)
        path = data["paths"][i]
        k = data["kinds"][i]
        if _ks and k not in _ks:
            continue
        if path in out:
            continue
        # 该文件所有向量行：总分取「含 AI 标签向量」的最大值，
        # 但定位用的「最匹配帧」只在真实帧行里选 —— 标签向量是整段视频拼图生成的，
        # 它不对应任何一帧，拿它当命中帧会把封面钉死在片头。
        top_s = None
        best_idx, best_meta = 0, {}
        best_frame_s = None
        for r in path_rows.get(path, []):
            rs = float(sims[r])
            c = _chunks[r] if r < len(_chunks) else 0
            if c >= 9000:
                # AI 标签向量：去掉通用方向后的点积，尺度不同，需映射
                rs = map_tag_score(rs)
            else:
                if best_frame_s is None or rs > best_frame_s:
                    best_frame_s, best_idx = rs, c
            if top_s is None or rs > top_s:
                top_s = rs
        if top_s is None:
            continue
        if best_frame_s is not None:
            best_meta = (chunk_map.get(path) or {}).get(best_idx, {})
        out[path] = (top_s, path, k, best_idx, best_meta)
    con.close()
    res = sorted(out.values(), key=lambda x: -x[0])
    return res[:limit]


# ---------------------------------------------------------------------------
# AI 标签向量（chunk_idx >= 9000）的分数映射
# ---------------------------------------------------------------------------
# 标签向量在索引期已经做了「去掉通用标签方向」的居中（见 indexer.TAG_CENTER_TERMS），
# 居中后的点积实测：真匹配 0.454~0.522、句子式查询 0.286~0.478、无关查询 0.000~0.097。
# 这里把它线性映射到与文档分数可比的区间，并让「无关查询」落到 0.46 门槛之下。
TAG_RAW_FLOOR = 0.20      # 居中点积低于此值＝无关
TAG_MAP_A = 1.15
TAG_MAP_B = 0.45 - TAG_MAP_A * TAG_RAW_FLOOR     # 0.20 → 0.45（刚好在门槛下）


def map_tag_score(s):
    """把「居中后的标签点积」映射成与文档分数可比的分数。"""
    if s is None:
        return s
    return max(0.0, min(0.99, TAG_MAP_A * float(s) + TAG_MAP_B))


def score_quality(results, min_count: int = 1, min_top: float = 0.0):
    """判断当前结果是否已经「够好」，无需再上深度模型。

    规则：
      * 有结果且条数 >= min_count
      * 最高分 >= min_top（若指定）
      * 且最高分显著高于第二名（区分度够）
    """
    if not results:
        return False
    if len(results) < min_count:
        return False
    top = results[0][0]
    if min_top and top < min_top:
        return False
    if len(results) >= 2:
        second = results[1][0]
        # 明显断层（领先 20% 以上）视为可靠
        if top > 0 and (top - second) / max(top, 1e-6) >= 0.20:
            return True
        # 或者绝对分已经很高
        if top >= 0.75:
            return True
    else:
        return True
    return False
