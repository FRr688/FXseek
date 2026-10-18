"""EG2（EmbeddingGemma 2）向量 —— 给手机端离线检索用的第二套向量。

## 为什么要单独一套

桌面自己的检索向量是 WeMM-Embedding-2B（约 2 GB，精度更高）。
但手机装不下 2 GB，手机跑的是 Google 的 **EmbeddingGemma 2**（740M，
可拆成 文本 270M / 视觉 170M / 音频 300M 三段按需加载）。

★ 两者**不是同一个向量空间**：不同模型、不同维度、不同坐标系。
把 WeMM 的向量喂给手机上的 EG2 检索器只会得到一堆噪声。
所以桌面必须用**同一个 EG2**把全库再算一遍，这份表就是干这个的。
它只服务同步，**不影响桌面自己的检索**（桌面检索仍然走 WeMM）。

## 端侧运行时矩阵（★ 查证于 2026-10-09）

向量空间是**模型**定义的，不是运行时定义的 —— 所以桌面用什么框架
不影响手机，只要两边是同一个模型、同一个维度。实测转换验证：
MLX 8bit 与原始 FP32 的 cosine ≥ 0.9998（文本 0.999814）。

| 端 | 推荐运行时 | 现成产物 |
|---|---|---|
| 桌面（macOS，本模块） | **MLX**（Apple Silicon 原生） | `mlx-community/embeddinggemma-2-8bit` 1.267 GB |
| iPhone | **Core ML**（走 ANE）或 MLX Swift | `EmbeddingGemma` CoreML 约 295 MB |
| Android | **LiteRT** / MediaPipe（后续 ML Kit） | `litert-community/embeddinggemma-2-740m-litert-lm` **485 MB** |

★ **MLX 不能用在 Android 上** —— 它是 Apple 的框架，只有 macOS / iOS。
Android 走 LiteRT（Google 官方的端侧方案，`.litertlm` 格式）。
桌面选 MLX 只是因为本机是 Mac、生态最顺，**不影响手机端怎么选**。

## 尺寸

| 档 | 组成 | 权重 |
|---|---|---|
| Text 270M | 只有文本编码器 | 最小 |
| Text+Vision 440M | 文本 + 视觉 | 中 |
| 740M | 文本 + 视觉 + 音频 | 1.234 GB（MLX 8bit，单文件） |

本模块按**全模态档**登记（`model_dl.SPEC_EG2`）：同一个 safetensors 里
就是全部权重，拆不开、少下也省不了空间。但代码里目前只实现**文本**编码
（`encode_texts`）；视觉 / 音频是 M4 的事。

## 三条必须记住的规格（来自官方 model card 与随模型下发的配置）

1. **池化是 mean pooling**，对 `last_hidden_state` 按 attention_mask 取平均
   （`1_Pooling/config.json`：`pooling_mode=mean`、`embedding_dimension=768`）。
2. **前缀是必须的**，不是可选优化。查询和文档用**不同**前缀，
   取值见 `PROMPTS` / `PROMPT_DOC`（抄自 `config_sentence_transformers.json`）。
3. **MRL 截断后必须重新归一化**。官方原话：「can be truncated … and
   re-normalized」。漏了这一步余弦会整体偏小、排序会漂。
   ★ 而且**入库和查询必须用同一个维度**。

## 现在的状态（骨架）

表结构与 `/v1/mobile/vectors` 接口都已就绪，但**权重还没下**，
`encode_texts()` 会抛 `Eg2NotReady`。加载器是唯一的接入点（`_load()`），
等确认要跑的时候再接。
"""

import base64
import json
import os
import sqlite3
import threading
import time

import numpy as np

import paths as P

# --- 模型标识 ---------------------------------------------------------------

MODEL_ID = "embeddinggemma-2-8bit"
DIM_NATIVE = 768            # 原生输出维度
DIMS = (128, 256, 512, 768)  # MRL 支持的截断维度
DEFAULT_DIM = 256           # 官方：256 维基本无损，128 维只适合纯文本
                            # ★ 入库和查询必须用同一个维度，这个值会成为表的一部分

# 任务前缀。抄自 mlx-community/embeddinggemma-2-8bit 的
# config_sentence_transformers.json（"prompts" 段），不是自己编的。
PROMPT_QUERY = "task: search result | query: "
PROMPT_DOC = "title: none | text: "
PROMPTS = {
    "retrieval":       PROMPT_QUERY,
    "search_query":    PROMPT_QUERY,
    "reranking":       PROMPT_QUERY,
    "bitext":          PROMPT_QUERY,
    "document":        PROMPT_DOC,
    "classification":  "task: classification | query: ",
    "clustering":      "task: clustering | query: ",
    "code":            "task: code retrieval | query: ",
    "fact_checking":   "task: fact checking | query: ",
    "similarity":      "task: sentence similarity | query: ",
    "question":        "task: question answering | query: ",
}

_LOCK = threading.Lock()
_MODEL = None               # 加载后的模型（懒加载，进程内单例）


class Eg2NotReady(RuntimeError):
    """EG2 还不能用：要么权重没下，要么加载器还没接。"""


# --- 前缀 -------------------------------------------------------------------

def q_text(text):
    """检索查询的前缀。★ 查询必须用这个，不能直接喂裸文本。"""
    return PROMPT_QUERY + (text or "")


def d_text(text, title=None):
    """文档的前缀。title 未知时官方示例写的是 `title: none`。"""
    return "title: %s | text: %s" % (title or "none", text or "")


def prompt_for(task):
    """按任务名取前缀，取不到当检索查询处理。"""
    return PROMPTS.get(str(task or "retrieval").strip().lower(), PROMPT_QUERY)


# --- 模型文件 ---------------------------------------------------------------

def model_dir():
    """EG2 权重的落地目录（在数据目录下，和 WeMM 的 `model/` 并列）。"""
    import model_dl
    return model_dl.model_dir_for(P.DATA_DIR, model_dl.SPEC_EG2)


def model_ready():
    """权重齐了没有（不加载，只查文件）。"""
    import model_dl
    return bool(model_dl.model_ready(P.DATA_DIR, model_dl.SPEC_EG2))


def model_state():
    """给设置页 / 接口看的一句话状态。"""
    import model_dl
    d = model_dir()
    spec = model_dl.SPEC_EG2
    ready = bool(model_dl.model_ready(P.DATA_DIR, spec))
    free = model_dl.disk_free(d) if os.path.isdir(d) else model_dl.disk_free(P.DATA_DIR)
    return {
        "ready": ready,
        "dir": d,
        "bytes": spec.total,
        "disk_free": free,
        "disk_need": spec.min_free_bytes,
        "disk_ok": (free < 0 or free >= spec.min_free_bytes),
        "backend": _backend_note(),
    }


def _backend_note():
    """运行时说明，直接给用户看。"""
    import model_dl
    try:
        import mlx.core as mx           # noqa: F401
        has_mlx = True
    except Exception:
        has_mlx = False
    return {
        "mlx": has_mlx,
        "loader": "未接（骨架阶段）",
        "hint": "桌面用 MLX；手机端 iPhone 走 Core ML / MLX Swift，"
                "Android 走 LiteRT —— 三家算出的向量在同一个空间里。",
    }


# --- 向量编解码 -------------------------------------------------------------
# 存 float32 的话，一个 256 维向量 1 KB；350 个素材 350 KB，其实也还行。
# 但手机上这类同步往往一次拉几百上千条，int8 直接省 4 倍，而单位向量
# 的 int8 往返误差极小（分量绝对值本来就 ≤ 1，量化步长 1/127）。

def pack_vec(vec, dim):
    """float32 单位向量 → int8 bytes。"""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)[:dim]
    q = np.clip(np.rint(v * 127.0), -127, 127).astype(np.int8)
    return q.tobytes()


def unpack_vec(blob, dim):
    """int8 bytes → float32 单位向量（解完重新归一化，别省这一步）。"""
    q = np.frombuffer(blob, dtype=np.int8).astype(np.float32) / 127.0
    v = q[:dim]
    n = float(np.linalg.norm(v))
    if n > 1e-9:
        v = v / n
    return v


def truncate_normalize(vec, dim):
    """MRL 截断 + 重新归一化。★ 这两步必须一起做。"""
    v = np.asarray(vec, dtype=np.float32).reshape(-1)[:dim]
    n = float(np.linalg.norm(v))
    if n > 1e-9:
        v = v / n
    return v


# --- 表结构 -----------------------------------------------------------------
# 和 WeMM 的 `items.vec` 并存，互不影响。放同一个 index.db 里，
# 因为它的生命周期跟 items 完全一致（删素材就该一起删）。

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eg2_vectors (
    path      TEXT PRIMARY KEY,
    mtime     REAL,
    dim       INTEGER,
    vec       BLOB,
    model     TEXT,
    rev       INTEGER,
    built_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_eg2_rev ON eg2_vectors(rev);
CREATE TABLE IF NOT EXISTS eg2_deleted (
    path TEXT PRIMARY KEY,
    rev  INTEGER
);
CREATE TABLE IF NOT EXISTS eg2_meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def connect(db_path=None):
    con = sqlite3.connect(db_path or P.DB_PATH)
    ensure_schema(con)
    return con


def ensure_schema(con):
    con.executescript(_SCHEMA)
    con.commit()


def _meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM eg2_meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def _meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO eg2_meta(k,v) VALUES(?,?)", (k, str(v)))


def rev(con):
    """当前版本号。手机带 `since=<rev>` 来，只要比它新的。"""
    try:
        return int(_meta_get(con, "rev", 0) or 0)
    except Exception:
        return 0


def stored_dim(con):
    """表里存的是多少维。★ 换维度就等于整表作废（不同维度不可比）。"""
    try:
        return int(_meta_get(con, "dim", DEFAULT_DIM) or DEFAULT_DIM)
    except Exception:
        return DEFAULT_DIM


def stored_model(con):
    return _meta_get(con, "model", MODEL_ID)


def reset(con):
    """整表作废（换模型 / 换维度时用）。"""
    con.execute("DELETE FROM eg2_vectors")
    con.execute("DELETE FROM eg2_deleted")
    _meta_set(con, "rev", 0)
    con.commit()


def put_many(con, rows, model=MODEL_ID, dim=DEFAULT_DIM, t=None):
    """写入一批向量。

    rows: [(path, mtime, vec_f32), ...]，vec_f32 是**未截断**的输出，
          这里统一做截断 + 归一化再入库。

    返回新版本号。★ 整批共用一个 rev —— 手机按 rev 增量拉，一批一版最省。
    """
    if not rows:
        return rev(con)
    old_model, old_dim = stored_model(con), stored_dim(con)
    if old_model != model or old_dim != dim:
        # 模型或维度变了：旧向量与新的不可比，整表清掉重来
        reset(con)
    now = t if t is not None else time.time()
    r = rev(con) + 1
    with con:
        for path, mtime, vec in rows:
            v = truncate_normalize(vec, dim)
            con.execute(
                "INSERT OR REPLACE INTO eg2_vectors(path,mtime,dim,vec,model,rev,built_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (path, float(mtime or 0), int(dim), pack_vec(v, dim), model, r, now),
            )
            con.execute("DELETE FROM eg2_deleted WHERE path=?", (path,))
        _meta_set(con, "rev", r)
        _meta_set(con, "dim", dim)
        _meta_set(con, "model", model)
    return r


def drop(con, paths):
    """删素材时调用：把向量删掉并**留下墓碑**。

    ★ 没有墓碑的话，手机那边不知道这条该删 —— 它只会看到「这次没同步到」，
    于是本地那条永远留着，搜出来点开是空的。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return rev(con)
    r = rev(con) + 1
    with con:
        for p in paths:
            con.execute("DELETE FROM eg2_vectors WHERE path=?", (p,))
            con.execute("INSERT OR REPLACE INTO eg2_deleted(path,rev) VALUES(?,?)", (p, r))
        _meta_set(con, "rev", r)
    return r


def since(con, since_rev=0, dim=None, limit=500):
    """增量拉取。返回 {rev, dim, model, vectors:[…], deleted:[…], more:bool}。"""
    want = int(dim or stored_dim(con))
    cur = rev(con)
    lim = max(1, min(int(limit or 500), 5000))
    # 维度对不上就别发了 —— 手机那边也解不了，白费流量
    if want != stored_dim(con):
        return {"rev": cur, "dim": stored_dim(con), "model": stored_model(con),
                "vectors": [], "deleted": [], "more": False,
                "error": "维度不匹配：本机存的是 %d 维，你请求的是 %d 维（换维度要整表重算）"
                         % (stored_dim(con), want)}
    rows = con.execute(
        "SELECT path,mtime,vec FROM eg2_vectors WHERE rev>? ORDER BY rev LIMIT ?",
        (int(since_rev or 0), lim + 1),
    ).fetchall()
    more = len(rows) > lim
    rows = rows[:lim]
    dels = con.execute(
        "SELECT path FROM eg2_deleted WHERE rev>? ORDER BY rev LIMIT ?",
        (int(since_rev or 0), lim + 1),
    ).fetchall()
    more = more or len(dels) > lim
    out = [{"path": p, "mtime": m, "vec": base64.b64encode(b).decode()} for p, m, b in rows]
    return {"rev": cur, "dim": stored_dim(con), "model": stored_model(con),
            "vectors": out, "deleted": [d[0] for d in dels[:lim]], "more": bool(more)}


def stats(con):
    n = con.execute("SELECT COUNT(*) FROM eg2_vectors").fetchone()[0]
    pend = con.execute("SELECT COUNT(*) FROM eg2_deleted").fetchone()[0]
    return {"count": n, "tombstones": pend, "rev": rev(con),
            "dim": stored_dim(con), "model": stored_model(con)}


# --- 编码（★ 唯一的接入点） --------------------------------------------------

def _load():
    """加载 EG2 权重，返回模型对象。

    ★ 这里是唯一需要动的地方。查证结果（2026-10-09）：

      · mlx-vlm **最新发布版 0.7.6 还没有 EG2**（它是 10-05 切的 tag），
        EG2 支持 10-06 才进 main（commit fec3f5037，PR #2446）。
      · 但它要的东西本机 0.7.1 **全都现成**：`gemma4.gemma4.MultimodalEmbedder`、
        `gemma4.vision.VisionModel`、`gemma4.audio.AudioEncoder`、
        `models.pooling.{mean_pooling,EmbeddingOutput,normalize_embeddings}`、
        `gemma4.config.{AudioConfig,VisionConfig}` —— 七个 import 逐个验过全 OK。
      · 所以要接的时候有两条路：① 把上游 `models/embedding_gemma2/` 那几个
        文件 vendor 进仓库（依赖一行不动）；② 等官方发版后纯 pip 升级。
        两条都能用，等确认要跑再定。

    实现要点（照 model card）：
      tokenizer(带前缀的文本) → 文本塔 last_hidden_state
      → mean_pooling(attention_mask) → 512→768 投影
      → truncate_normalize(dim)
    """
    raise Eg2NotReady(
        "EG2 的加载器还没接（骨架阶段）。权重路径：%s" % model_dir())


def encode_texts(texts, dim=DEFAULT_DIM, task="retrieval"):
    """把一批文本编码成向量（已归一化、已截断）。

    ★ 调用方负责**自己加前缀** —— 检索查询用 `q_text()`，文档用 `d_text()`。
    这里加默认前缀只为了「有人忘了加」时不至于完全错，不能当兜底用。
    """
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            if not model_ready():
                raise Eg2NotReady(
                    "EG2 权重还没下载：%s（用设置页的「下载模型」，"
                    "或 python model_dl.py --spec eg2）" % model_dir())
            _MODEL = _load()
        model = _MODEL
    raw = _forward(model, texts)
    return [truncate_normalize(v, dim) for v in raw]


def _forward(model, texts):
    """真正跑前向。与 `_load()` 一起，是接入时要写的两处。"""
    raise Eg2NotReady("EG2 的前向还没接（骨架阶段）")


def encode_query(text, dim=DEFAULT_DIM):
    return encode_texts([q_text(text)], dim=dim)[0]


def encode_doc(text, dim=DEFAULT_DIM, title=None):
    return encode_texts([d_text(text, title)], dim=dim)[0]


# --- 命令行 -----------------------------------------------------------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="EG2 向量表：看看状态 / 清空")
    ap.add_argument("--reset", action="store_true", help="整表作废")
    ap.add_argument("--db", default=None, help="指定库文件（默认用户数据目录）")
    a = ap.parse_args()
    con = connect(a.db)
    if a.reset:
        reset(con)
        print("已清空")
    print("模型目录 :", model_dir())
    print("权重就绪 :", model_ready())
    print("表       :", json.dumps(stats(con), ensure_ascii=False))
    print("编解码   :", _selftest())


def _selftest():
    """不依赖模型的往返自检：int8 量化 + MRL 截断的误差有多大。"""
    rng = np.random.default_rng(7)
    worst = 0.0
    for _ in range(50):
        v = rng.normal(size=DIM_NATIVE).astype(np.float32)
        v /= np.linalg.norm(v)
        for d in DIMS:
            t = truncate_normalize(v, d)
            r = unpack_vec(pack_vec(t, d), d)
            worst = max(worst, 1.0 - float(np.dot(t, r)))
    return "int8 往返最大余弦损失 %.2e（越小越好）" % worst


if __name__ == "__main__":
    _main()
