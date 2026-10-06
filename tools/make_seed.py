# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""重新生成 seed/ 里的预置向量库、指纹库和元数据。

什么时候要跑
------------
`seed/assets/` 里的示例素材改动过、或者想更新示例的 AI 描述 / 转写文本时。
（普通改代码不需要跑这个，种子库是静态资源。）

它做三件事
----------
1. 用 **本机模型** 给 seed/assets/ 下所有文件建一份全新的向量库 → seed/index.db
2. 给其中的音频建指纹库 → seed/fingerprints.db
3. 把「AI 描述 / 标签 / 转写文本」从**现有用户库**里捞出来，补给新库里的同名文件

第 3 步是必须的，原因见下。

为什么要把 AI 描述从旧库里抄过来
--------------------------------
向量库里的 AI 描述和标签是靠**用户自己填的 API Key** 调视觉模型生成的，打包机上
没法在构建时现算（不能替用户花钱，也不该把某人的 key 塞进安装包）。但描述对演示
效果又至关重要——没有它，示例图片在详情页里就是一片空白，语义搜索「穿米色针织衫
的女人」这种查询也搜不到东西，教程价值直接减半。

所以做法是：**从开发机现有的库里把这些文本捞出来，连同向量一起打包**。这些描述
是示例素材本身的内容（「一只小狗在跑道上奔跑」），不涉及任何用户隐私数据，可以
随包分发。捞不到的（比如新加的素材）就留空，不编造。
"""
import json
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

SEED = os.path.join(HERE, "seed")
ASSETS = os.path.join(SEED, "assets")
OUT_INDEX = os.path.join(SEED, "index.db")
OUT_FP = os.path.join(SEED, "fingerprints.db")

# AI 元数据的来源库：开发机上的用户库（找不到就跳过这一步）
LIVE_INDEX = os.path.expanduser("~/Library/Application Support/FXseek/index.db")

# 要从旧库抄过来的 meta 字段
CARRY_KEYS = ("ai_description", "ai_tags", "ai_model", "asr_text", "asr_model")


def build_index():
    print("=" * 60)
    print("1/3  建向量库（用本机模型）")
    print("=" * 60)
    if os.path.exists(OUT_INDEX):
        os.remove(OUT_INDEX)
    import indexer as ix
    t0 = time.time()

    def prog(d):
        if d.get("phase") in ("scanning", "done"):
            print("   ", d.get("message"))
        elif d.get("current") == d.get("total"):
            print("   ", d.get("message"))

    r = ix.build_index([ASSETS], db_path=OUT_INDEX, force=True,
                       model_path=os.path.join(HERE, "model"), progress=prog)
    print("    → %s  (%.1fs)" % (r, time.time() - t0))
    return r


def carry_ai_meta():
    """把旧库里同名文件的 AI 描述 / 标签 / 转写文本抄进新库。"""
    print("=" * 60)
    print("2/3  搬运 AI 描述 / 标签 / 转写文本")
    print("=" * 60)
    if not os.path.exists(LIVE_INDEX):
        print("    ! 找不到来源库 %s，跳过（示例将没有 AI 描述）" % LIVE_INDEX)
        return 0
    src = sqlite3.connect(LIVE_INDEX)
    dst = sqlite3.connect(OUT_INDEX)
    # 旧库按 basename 建索引：示例素材以前可能放在别的目录里
    want = {}
    for path, meta, cidx in src.execute("SELECT path, meta, chunk_idx FROM items"):
        try:
            d = json.loads(meta or "{}")
        except Exception:
            continue
        if not any(k in d for k in CARRY_KEYS):
            continue
        base = os.path.basename(path)
        # chunk_idx 0 是「文件本体」那条，带着完整描述；优先用它
        if base not in want or cidx == 0:
            want[base] = d
    src.close()

    n = 0
    for (path, meta, cidx) in dst.execute("SELECT path, meta, chunk_idx FROM items").fetchall():
        base = os.path.basename(path)
        got = want.get(base)
        if not got:
            continue
        try:
            cur = json.loads(meta or "{}")
        except Exception:
            cur = {}
        for k in CARRY_KEYS:
            if k in got:
                cur[k] = got[k]
        # 关键：按 (path, chunk_idx) 更新，不能只按 path —— 视频的每一帧是独立的一条
        # items 记录（共用 path、不同 chunk_idx），只按 path 会把所有帧的 meta 覆盖成
        # 循环最后那一帧的 meta，导致「timestamp 全部=最后一帧时间」，以图搜图封面抽错帧。
        dst.execute("UPDATE items SET meta=? WHERE path=? AND chunk_idx=?",
                    (json.dumps(cur, ensure_ascii=False), path, cidx))
        n += 1
    dst.commit()
    dst.close()
    print("    → 补上了 %d 条记录的 AI 元数据" % n)
    return n


def rebuild_vectors_for_meta():
    """描述补进去之后，把描述和标签也编码成向量入库。

    只补 meta 不改向量的话，详情页能看到描述，但**搜不到**——检索靠的是向量。
    编号沿用 indexer.py 里约定的两段：
      · AI_TAG_BASE(9000)+i —— 每个标签一条向量，让「针织衫」这类词能命中
      · AI_DESC_IDX(9500)   —— 整段描述一条向量，让「穿米色针织衫的女人」能命中
    用的是同一个 `_center_text_vec`，和线上跑出来的分布对齐。
    """
    print("=" * 60)
    print("3/3  把 AI 描述 / 标签编码成向量")
    print("=" * 60)
    import embed as we
    import indexer as ix
    model_path = os.path.join(HERE, "model")
    model, processor = we.load_model(model_path)
    con = sqlite3.connect(OUT_INDEX)
    rows = con.execute("SELECT path, kind, meta FROM items WHERE chunk_idx=0").fetchall()
    n_tag = n_desc = 0
    for path, kind, meta in rows:
        try:
            d = json.loads(meta or "{}")
        except Exception:
            continue
        # 先清掉这个文件已有的 AI 块，避免重复跑时越堆越多
        con.execute("DELETE FROM items WHERE path=? AND (chunk_idx BETWEEN ? AND ? OR chunk_idx=?)",
                    (path, ix.AI_TAG_BASE, ix.AI_TAG_BASE + 500, ix.AI_DESC_IDX))
        tags = [t for t in (d.get("ai_tags") or []) if t]
        desc = (d.get("ai_description") or "").strip()
        slots = [(ix.AI_TAG_BASE + i, t, True) for i, t in enumerate(tags)]
        if desc:
            slots.append((ix.AI_DESC_IDX, desc, False))
        for cidx, text, is_tag in slots:
            try:
                vec = ix._center_text_vec(model, processor, text)
                if not vec:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO items"
                    " (path,kind,chunk_idx,meta,dim,vec,mtime,indexed_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (path, kind, cidx,
                     json.dumps({"_ai_tag_slot" if is_tag else "_ai_desc_slot": True},
                                ensure_ascii=False),
                     len(vec), ix._to_blob(vec), 0, time.time()))
                n_tag += 1 if is_tag else 0
                n_desc += 0 if is_tag else 1
            except Exception as e:
                print("    !!  %s #%d %s" % (os.path.basename(path), cidx, e))
    con.commit()
    n_units = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    con.close()
    print("    → %d 条标签向量 + %d 条描述向量，库里共 %d 条" % (n_tag, n_desc, n_units))
    return n_tag + n_desc


def build_fingerprints():
    print("=" * 60)
    print("   建音频指纹库")
    print("=" * 60)
    if os.path.exists(OUT_FP):
        os.remove(OUT_FP)
    import fingerprint as fpr
    fpr.FP_DB = OUT_FP
    fpr._init_db().close()
    n = 0
    for fn in sorted(os.listdir(ASSETS)):
        if not fn.lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg")):
            continue
        p = os.path.join(ASSETS, fn)
        try:
            fpr.index_file(p, "audio", os.path.getmtime(p))
            n += 1
            print("    ok  %s" % fn)
        except Exception as e:
            print("    !!  %s  %s" % (fn, e))
    print("    → %d 首" % n)
    return n


if __name__ == "__main__":
    if not os.path.isdir(ASSETS) or not os.listdir(ASSETS):
        sys.exit("seed/assets/ 是空的，先把示例素材放进去")
    build_index()
    carry_ai_meta()
    rebuild_vectors_for_meta()
    build_fingerprints()
    print("\n种子库已重新生成：")
    for f in ("index.db", "fingerprints.db"):
        p = os.path.join(SEED, f)
        print("  %-20s %8.1f KB" % (f, os.path.getsize(p) / 1024))
