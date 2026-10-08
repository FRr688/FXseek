#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
WeMM 本地语义检索应用 —— API + 网页版

★ 独立运行（wemm_app/venv），不碰 oMLX；纯标准库实现，零额外依赖。
★ 内置模型（wemm_app/model），可直接打包。

启动：
    wemm_app/venv/cpython-3.11/bin/python3.11 app.py --port 8231
然后浏览器打开 http://127.0.0.1:8231

接口：
    GET  /                         网页版 UI
    GET  /health
    POST /v1/index                 建索引 {"paths":["/Users/x/Pictures"], "force":false}
    POST /v1/search                语义搜索 {"query":"一只狗在跑","top_k":10,"kind":null}
                                   kind 可以是单个类别字符串，也可以是数组（如
                                   ["audio","video"]），展开成 kind IN (...)
    GET  /v1/stats                 索引统计
    GET  /v1/thumb?path=...        图片/视频帧缩略图
    GET  /v1/file?path=...         原文件（供预览/播放）
    POST /v1/embeddings            (兼容) 向量
    POST /v1/rerank                (兼容) 重排
"""
import argparse
import base64
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import asr_polish
import embed as we
import heif_support  # noqa: F401  注册 HEIC/HEIF 解码器（缩略图、打标都要）
import indexer as ix
import model_dl
import paths as P
import seed as SEED

_STATE = {"model": None, "processor": None, "model_path": None,
          "busy": None, "last_index": None,
          "progress": None, "last_result": None,
          "last_req": 0.0,        # 最近一次 HTTP 请求时间（含轮询，仅用于诊断）
          "last_user_req": 0.0,   # 最近一次「用户真的在操作」的请求时间
          "ai_tagging": None,     # 正在手动/自动打标的文件路径
          "asr_running": None,    # 正在手动/自动转写的文件路径
          "model_unloaded": 0}    # 累计「按需卸载」次数（诊断用）

# 模型按需加载的锁：两个请求同时发现模型没加载时，只让一个真去 load()，
# 否则会同时加载两份 2GB 权重（8G 机器上直接换页卡死）。
_MODEL_LOCK = threading.Lock()

# 纯轮询端点：前端（设置页 / 进度条）会按秒级定时打这些接口，
# 它们只代表「界面开着」不代表「用户在用」。如果拿它们刷新闲置计时，
# 设置页每 8 秒轮询一次 /v1/ai/status 就会把计时不断清零 ——
# 用户看到「空闲 2s / 300s」永远涨不上去，闲置自动打标/转写再也不会触发。
_QUIET_PATHS = ("/health", "/v1/ai/status", "/v1/asr/status", "/v1/idle/status")
# 闲置自动处理的运行状态（不落盘，重启清零）
_IDLE = {"run_tagged": 0, "run_asr": 0, "last_path": None, "last_ts": None,
         "last_error": None, "window": None, "turn": 0, "last_kind": None,
         # 失败账本 {realpath: {"n": 连败次数, "ts": 最后失败时间, "msg": 原因}}。
         # 同一个文件连败 _FAIL_MAX 次就冷藏 _FAIL_COOL 秒 —— 否则它会一直
         # 占着队首，后面的素材永远轮不到（实测：一首撞上代理超时的歌，
         # 把 187 个待转写全卡死在「本轮第 1/10」）。
         "fail": {},
         # 本轮因失败而消耗的名额（见 total_run），以及跨文件连败计数
         "run_fail": 0, "consec_fail": 0,
         # 「立即补全」的一次性任务（不受空闲门槛与本轮上限约束）。
         # 形如 {"what": "tag"|"asr"|"both", "done": 0, "failed": 0,
         #        "started": ts, "stop": False, "finished": ts|None}
         "manual": None}
SERVICE_NAME = "wemm-search"

# 允许索引/预览的根目录（安全边界，可用 --allow 追加）
ALLOW_ROOTS = [os.path.expanduser("~"), "/tmp", "/Volumes"]

SETTINGS_PATH = P.SETTINGS_PATH
TRASH_PATH = P.TRASH_PATH

# ---- 操作日志（内存环形队列，最多 200 条） ----
LOGS = []
LOG_MAX = 200

def op_log(action: str, detail: str = "", level: str = "info"):
    """记录一条操作日志。action: index/search/delete/...；level: info/warn/error"""
    import time as _t
    LOGS.append({"ts": _t.time(), "action": action, "detail": detail, "level": level})
    if len(LOGS) > LOG_MAX:
        del LOGS[:len(LOGS) - LOG_MAX]
SOURCES_PATH = P.SOURCES_PATH

DEFAULT_SETTINGS = {
    "theme": "auto",          # auto | light | dark
    "font_size": "medium",    # small | medium | large
    "cache_limit_mb": 1024,   # 预览缓存上限(MB)
    # ★ 版本号唯一来源：改这里就够了 —— build_app.sh / release.sh 都从这一行 grep，
    # tray_helper 的「关于」兜底也从这里读。别在别处再写死版本号。
    "version": "1.0.6",
    # 点窗口关闭按钮时怎么办：ask = 每次问；quit = 直接退出；tray = 直接最小化到菜单栏。
    # 由 launcher.py 的关闭确认框写入（勾了「记住我的选择」才会变成 quit/tray）。
    "close_action": "ask",
    # ---- AI 描述生成（可为 oMLX 本地 / OpenAI 兼容服务）----
    "ai_enabled": True,
    # 出厂留空：不预设任何服务商地址，避免新用户看到开发机痕迹。
    # 设置页里选「自定义 / 本地」时会提示 oMLX 的默认端口 127.0.0.1:9977/v1。
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_model": "",
    "ai_prompt": "你是素材库打标助手。仔细观察这张图片，生成用于语义检索的描述和标签，让用户日后能用文字精准搜到它。\n\n严格按以下两行输出，不要加任何解释、前后缀或多余文字：\n\n描述：一句简洁中文，概括主体、外观特征、动作与环境。\n\n关键词：逗号分隔 8~12 个短词（每个 2~6 字），要具体、有区分度，覆盖：主体是什么、颜色/材质、动作、地点/时间/天气、画面风格（照片/插画/截图/海报/3D）、画面里的文字或品牌（若有）。禁止用「图片」「场景」「物体」「内容」「人物」这类泛词；同一概念只留最具体的一个词；无法识别时关键词写「无」。",
    "ai_prompt_video": "这是同一段视频按时间顺序抽取的若干关键帧拼成的网格图（从左到右、从上到下）。你是素材库打标助手，请观察整段视频内容，生成用于语义检索的描述和标签。\n\n严格按以下两行输出，不要加任何解释、前后缀或多余文字：\n\n描述：一句简洁中文，概括这段视频的主体、人物、动作与环境。\n\n关键词：逗号分隔 8~12 个短词（每个 2~6 字），要具体、有区分度，覆盖：人物/主体是什么、动作、环境、关键物体、整体风格。禁止用「视频」「场景」「画面」「内容」这类泛词；同一概念只留最具体的一个词；无法识别时关键词写「无」。",
    "ai_max_tags": 12,
    "ai_skip_similar": True,     # 跳过视觉相似的重复图片
    # ---- 闲置时自动处理（打标签 / 转写都摊到闲时，一次一个）----
    # 索引期不再做 AI 打标与 ASR 转写（见 indexer.build_index 顶部注释），
    # 所以这两个开关是「最终全部文件都有标签、都转写过」的默认保障手段。
    "idle_tag": True,            # 闲置时逐个补 AI 标签
    "idle_asr": True,            # 闲置时逐个补音频转写
    "idle_minutes": 5,           # 空闲多久之后开始（分钟）
    "idle_batch": 10,            # 每轮最多补几个（一轮 = 一段连续空闲）
    # ---- 音频转写（ASR，OpenAI /v1/audio/transcriptions 兼容，如 oMLX 的 Qwen3-ASR）----
    "asr_enabled": True,
    # 同上：出厂留空，由用户在设置页自己填。
    "asr_base_url": "",
    "asr_api_key": "",
    "asr_model": "",
    "asr_language": "",          # 留空=自动检测；可填 zh/en 等
    # ★ 默认「不限」。以前是 600 秒，代价是**整张专辑 / 演唱会现场的 FLAC**
    #   （常见 35~65 分钟）一进来就被拒，而且拒得悄无声息：转写根本没启动，
    #   用户只看到「超过设置的上限」，不会想到去改一个自己没动过的设置。
    #   ASR 是本地跑的、不要钱，只在空闲时跑，实测 37 分钟的歌 = 6.2 分钟处理，
    #   完全可以接受；真嫌慢的人再自己填一个秒数。
    "asr_max_duration": 0,       # 超过该秒数的音频跳过转写（0=不限）
    # ---- 智能纠偏（转写文字的 LLM 校对）----
    # 默认「只在空闲时跑」：批量纠偏要反复打本地大模型，用户正敲键盘/搜东西的时候
    # 抢 GPU 会明显卡顿。判定门槛与闲置补全完全一致（距上次用户操作 idle_minutes 分钟）。
    "polish_idle_only": True,
    # 模型按需加载 + 闲置卸载：启动不常驻 3.5 GB 权重，闲置 N 分钟后自动还内存。
    # 8 GB 的 M1 上这是「整机不发涩」和「首次检索多等 1.5~3 秒」之间的取舍，
    # 默认取前者；机器内存宽裕、追求每次检索都秒回的话可以关掉。
    "model_lazy": True,
    "model_idle_minutes": 5,
    # ---- 检索性能 ----
    "search_tier": "auto",       # auto | fast | vector | deep
    "match_level": "balanced",   # 搜索结果匹配度门槛 off | loose | balanced | strict | strictest
    "image_max_side": 420,       # 图片编码前缩放尺寸（越小越快）
    # ---- 视频抽帧 ----
    "video_density": "auto",     # auto(自适应) | dense(密集) | sparse(稀疏) | custom
    "video_custom_gap": 1.0,     # custom 模式：固定帧间隔（秒）
    "video_max_frames": 32,      # 单视频帧数上限
    # ---- 界面动效 ----
    "ui_anim": "auto",           # auto(遵循系统减弱动态效果) | full(始终完整动画) | off(关闭)
    # ---- MCP 服务（把检索能力开放给本机 agent）----
    # MCP 走 stdio：每个 agent 自己拉起一份 mcp_server.py 子进程，主服务这边
    # **不需要常驻监听端口**。所以这个开关管的是「要不要把接入条目写进各 agent
    # 的配置」，关掉时会把写进去的条目摘掉（写入前都会备份成 *.fxseek.bak）。
    "mcp_enabled": False,
    "mcp_agents": [],            # 已启用的 agent id 列表，如 ["dsh","codex"]
    # ---- 界面语言 ----
    "lang": "zh",                # zh(简体中文) | en(English)
}


def load_sources() -> list:
    """已添加的索引源（服务端持久化，不依赖浏览器）。"""
    try:
        with open(SOURCES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sources(items: list):
    os.makedirs(os.path.dirname(SOURCES_PATH), exist_ok=True)
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def add_source(path: str) -> dict:
    """记录一个索引源；返回该源的实时统计。"""
    path = os.path.realpath(os.path.expanduser(path))
    items = load_sources()
    if not any(s["path"] == path for s in items):
        items.append({"path": path, "name": os.path.basename(path) or path,
                      "added_at": time.time()})
        save_sources(items)
    return {"sources": sources_with_stats()}


# 每类文件建索引的粗估耗时（秒/个），用于「立即索引前」给用户一个量级；
# 视频按抽帧数估算，AI 打标另外叠加。误差可能有好几倍，界面上只能说「约」。
SCAN_EST = {"image": 0.7, "video": 11.0, "audio": 2.0, "document": 2.0}
SCAN_AI_TAG = 13.0          # 一次视觉打标实测 ~11~14 秒
SCAN_MAX_FILES = 100000     # 扫描上限，超过就标记 truncated


def scan_source(path: str, st: dict | None = None) -> dict:
    """统计一个目录里有多少可索引文件、大致要跑多久 —— 给「是否立即索引」对话框用。"""
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(p):
        return {"error": "目录不存在或不可读取"}
    st = st or load_settings()
    counts = {"image": 0, "video": 0, "audio": 0, "document": 0, "other": 0}
    total = 0
    nbytes = 0
    truncated = False
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            if fn.startswith("."):
                continue
            total += 1
            if total > SCAN_MAX_FILES:
                truncated = True
                break
            fp = os.path.join(root, fn)
            counts[_kind_of(fp)] += 1
            try:
                nbytes += os.path.getsize(fp)
            except OSError:
                pass
        if truncated:
            break
    indexable = total - counts["other"]
    est = sum(SCAN_EST[k] * counts[k] for k in SCAN_EST)
    # AI 打标与 ASR 转写**不再**算进索引时间 —— 它们已经挪到「闲置时自动处理」和
    # 详情页的手动按钮（见 indexer.build_index 顶部注释）。这里只把积压量报给界面，
    # 让用户知道「索引很快，但标签/转写会慢慢补」。折算比例沿用与 indexer 一致的估算。
    media = counts["image"] + counts["video"]
    backlog = {"tag": media if st.get("ai_enabled") else 0,
               "asr": counts["audio"] + counts["video"] if st.get("asr_enabled") else 0,
               "tag_sec": round(media * SCAN_AI_TAG) if st.get("ai_enabled") else 0}
    # 这个目录里已经索引过的文件数（增量索引只需跑差集）
    indexed = 0
    try:
        con = sqlite3.connect(ix.DB_PATH)
        for (row_path,) in con.execute("SELECT DISTINCT path FROM items"):
            try:
                rp = os.path.realpath(row_path)
            except OSError:
                rp = row_path
            if rp == p or rp.startswith(p.rstrip("/") + "/"):
                indexed += 1
        con.close()
    except Exception:
        pass
    # 增量：只按「还没索引过的文件」折算耗时（改动过的文件会重跑，这里估不到）
    fresh = max(0, total - indexed)
    if total:
        est = est * fresh / total
    return {"path": p, "name": os.path.basename(p) or p, "total": total,
            "indexable": indexable, "indexed": indexed, "fresh": fresh, "bytes": nbytes,
            "kinds": counts, "truncated": truncated, "estimate_sec": int(est),
            "backlog": backlog}


def remove_source(path: str) -> dict:
    """移除索引源，并从索引库删除其下所有条目（不删原文件）。"""
    try:
        rp = os.path.realpath(os.path.expanduser(path))
    except Exception:
        rp = path
    items = [s for s in load_sources() if s["path"] != rp and s["path"] != path]
    save_sources(items)
    # 按规范化路径匹配删除
    con = sqlite3.connect(ix.DB_PATH)
    rows = con.execute("SELECT DISTINCT path FROM items").fetchall()
    victims = []
    for (p,) in rows:
        try:
            rpp = os.path.realpath(p)
        except Exception:
            rpp = p
        if rpp == rp or rpp.startswith(rp.rstrip("/") + "/"):
            victims.append(p)
    for v in victims:
        con.execute("DELETE FROM items WHERE path=?", (v,))
    con.commit(); con.close()
    return {"removed_files": len(victims), "sources": sources_with_stats()}


def reorder_sources(paths: list) -> dict:
    """按给定顺序重排索引源（侧栏拖拽排序）。

    `sources` 文件本身就是个有序数组，`sources_with_stats()` 原样按它的顺序输出，
    所以这里只要把数组重排后写回即可，不动任何索引数据。
    没在 `paths` 里出现的源**保持原来的相对顺序、补在后面** —— 前端万一漏传，
    宁可位置不对，也不能把用户的索引源弄丢。
    """
    items = load_sources()
    if not items:
        return {"sources": []}

    def norm(p):
        try:
            return os.path.realpath(os.path.expanduser(p))
        except Exception:
            return p

    by_path = {}
    for s in items:
        by_path.setdefault(norm(s["path"]), s)

    order, seen = [], set()
    for p in (paths or []):
        k = norm(p)
        if k in by_path and k not in seen:
            order.append(by_path[k])
            seen.add(k)
    for s in items:
        if norm(s["path"]) not in seen:
            order.append(s)

    save_sources(order)
    return {"sources": sources_with_stats()}


# ---------------- 失效索引清理（GC，不动原文件） ----------------

def gc_scan() -> dict:
    """扫描垃圾索引记录，**只读不删**。分三类：

    missing —— 磁盘上文件已经不存在（彻底删除 / 外接盘拔了）
    orphan  —— 文件还在，但不属于任何已配置的索引源
               （典型来自：以前用路径直接索引过，后来索引源被移除）
    fp_only —— **只**残留在指纹库（fingerprints.db）里的失效路径

    为什么要单独扫指纹库：文件夹改名/移动后，重建索引只换掉了 index.db 里的
    items，指纹表没人清。结果是「以音搜素材」同时返回新路径和一条「文件已移动」
    ——用户点了清理也还在，因为以前压根没看指纹库。
    """
    roots = []
    for s in load_sources():
        try:
            roots.append(os.path.realpath(os.path.expanduser(s["path"])).rstrip(os.sep))
        except Exception:
            pass
    con = sqlite3.connect(ix.DB_PATH)
    try:
        rows = con.execute("SELECT DISTINCT path FROM items").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    def under_any(p):
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        return any(rp == r or rp.startswith(r + os.sep) for r in roots)

    missing, orphan = [], []
    seen = set()
    for (p,) in rows:
        seen.add(p)
        if not os.path.exists(p):
            missing.append(p)
        elif not under_any(p):
            orphan.append(p)
    db_missing = list(missing)          # 来自 index.db 的失效条数（kept 要用）

    # 指纹库里的失效路径。已经在 missing 里的合并（两个库都脏），
    # 只存在于指纹库的单独列出来 —— 清理时两者都要删。
    fp_stale = []
    try:
        import fingerprint as fp
        fp_stale = fp.stale_paths()
    except Exception:  # noqa: BLE001
        fp_stale = []
    fp_only = [p for p in fp_stale if p not in seen]
    for p in fp_stale:
        if p not in missing:
            missing.append(p)

    return {"missing": missing, "orphan": orphan, "fp_only": fp_only,
            "db_missing": len(db_missing), "fp_stale": len(fp_stale),
            "known": len(rows), "roots": roots}


def gc_index(orphans: bool = False, dry_run: bool = False) -> dict:
    """清理失效索引记录。orphans=True 时连「源外但文件仍在」的记录一起清。"""
    if _STATE["busy"]:
        return {"error": "索引任务运行中，请稍后再清理"}
    scan = gc_scan()
    victims = list(scan["missing"]) + (list(scan["orphan"]) if orphans else [])
    base = {"missing": len(scan["missing"]), "orphan": len(scan["orphan"]),
            "fp_only": len(scan.get("fp_only") or []),
            "fp_stale": scan.get("fp_stale", 0),
            "known": scan["known"], "paths": P.describe()}
    if dry_run:
        return {**base, "dry_run": True, "would_remove": len(victims)}
    if not victims:
        return {**base, "removed": 0, "vectors_removed": 0, "vacuumed": False,
                "fp_removed": 0, "kept": scan["known"]}

    con = ix._init_db(ix.DB_PATH)
    try:
        before = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        for p in victims:
            con.execute("DELETE FROM items WHERE path=?", (p,))
        con.commit()
        after = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        vacuumed = False
        if before - after >= 20:                 # 删得多才压实，避免频繁重写整库
            try:
                con.execute("VACUUM")
                vacuumed = True
            except sqlite3.OperationalError:
                pass
    finally:
        con.close()

    # 指纹库也要清。两个库是分开的，只清 items 的话「文件已移动」还会继续出现在
    # 以音搜素材的结果里 —— 这正是以前那个 bug。
    fp_removed = 0
    try:
        import fingerprint as fp
        fp_removed = fp.gc_remove(victims)
    except Exception as e:  # noqa: BLE001
        op_log("清理", f"指纹库清理失败：{e}", level="warn")

    op_log("清理", f"移除 {len(victims)} 个失效记录"
                   f"（文件已删除 {len(scan['missing'])}、源外 {len(scan['orphan']) if orphans else 0}）"
                   + (f"，指纹 {fp_removed} 条" if fp_removed else ""))
    # kept 只数 **index.db 里的** 存活条目：fp_only 那些本来就只在指纹库里，
    # 拿它们去减 known 会算出一个偏小的数。
    kept = scan["known"] - scan.get("db_missing", len(scan["missing"]))
    if orphans:
        kept -= len(scan["orphan"])
    return {**base, "removed": len(victims), "vectors_removed": before - after,
            "vacuumed": vacuumed, "fp_removed": fp_removed,
            "kept": max(0, kept)}


# ---------------------------------------------------------------- 失败账本
# 闲置/立即补全跑到某个文件反复失败时，**不能**让它堵住队列：
#   实测一例——一首 4 分钟的歌（26 MB）转写要 60 秒以上，而 macOS 的系统代理
#   （DevSidecar）自己带 60 秒超时，于是稳定返回 504，同一个文件被无限重试，
#   界面上「本轮第 1/10」永远不动，187 个待转写一个也轮不到。
# 处理办法：同一个文件连败 _FAIL_MAX 次就冷藏 _FAIL_COOL 秒，期间跳过它去处理
# 后面的素材；冷藏到期会自动再试一次（文件本身修好了就自动恢复）。
_FAIL_MAX = 3            # 同一文件连败几次就冷藏
_FAIL_COOL = 900         # 冷藏时长（秒）
_FAIL_LOOKAHEAD = 200    # 选任务时最多往后看多少个候选


def _fail_key(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path or ""


def _is_cooled(path: str) -> bool:
    """这个文件刚连败太多次？是的话本轮先跳过它。"""
    f = _IDLE.get("fail", {}).get(_fail_key(path))
    if not f or int(f.get("n", 0)) < _FAIL_MAX:
        return False
    return (time.time() - float(f.get("ts", 0))) < _FAIL_COOL


def _note_fail(path: str, msg: str) -> int:
    """记一次失败，返回该文件的累计连败次数。"""
    k = _fail_key(path)
    ban = _IDLE.setdefault("fail", {})
    f = ban.get(k) or {"n": 0}
    f["n"] = int(f.get("n", 0)) + 1
    f["ts"] = time.time()
    f["msg"] = (msg or "")[:300]
    ban[k] = f
    if len(ban) > 500:                       # 账本别无限长
        for k2, _ in sorted(ban.items(), key=lambda kv: kv[1].get("ts", 0))[:100]:
            ban.pop(k2, None)
    return f["n"]


def _clear_fail(path: str) -> None:
    """成功了就把账销掉，下次它再变坏会重新计数。"""
    _IDLE.get("fail", {}).pop(_fail_key(path), None)


# 这些错误说明「服务整体不可用」，而不是「这个文件有问题」：
#   · 连接被拒 / 服务没起 → 每个文件都会失败，但不是文件的错
#   · 504 / 超时 → macOS 系统代理（DevSidecar）自己带 60 秒超时
# 如果照样把账算在单个文件头上，200 个待办会被**逐个**冷藏 3 次，
# 最后全部进冷藏 → 界面表现就是「整批一直终止，再也不动了」。
# 所以这类失败只做全局退避，不写入单文件黑名单。
_SYSTEMIC_PAT = (
    "无法连接", "connection refused", "econnrefused", "connection reset",
    "timed out", "timeout", "operation timed out", "device not configured",
    "no response from upstream", "upstream", "http 500", "http 502",
    "http 503", "http 504", "service unavailable", "bad gateway",
    "gateway timeout", "urlerror", "not reachable", "network is unreachable",
    # SQLite 写锁竞争：影响的是「当下每一次写」，与具体哪个文件无关。
    # 若按单文件记账，200 个待办会各自连败 3 次再全进冷藏 → 整批停摆。
    "database is locked", "database table is locked",
)


def _is_systemic_fail(msg: str) -> bool:
    """这个失败是「服务整体挂了」还是「单个文件坏了」？"""
    m = (msg or "").lower()
    return any(p in m for p in _SYSTEMIC_PAT)


def _pick_pending(pend: list):
    """从待办列表里挑第一个「没在冷藏中」的路径；全在冷藏里就返回 None。"""
    for it in pend[:_FAIL_LOOKAHEAD]:
        p = it["path"] if isinstance(it, dict) else it
        if not _is_cooled(p):
            return p
    return None


def _cooled_count() -> int:
    n = 0
    for p in list(_IDLE.get("fail", {})):
        if _is_cooled(p):
            n += 1
    return n


def _idle_loop():
    """闲置时自动处理：用户不用的间隙，一次补一个素材的 AI 标签或音频转写。

    为什么是「一个一个来」而不是批量扫全库：
      * 视觉模型调一次要十几到几十秒，语音识别也要几秒到几十秒，批量跑会把 CPU/GPU
        占满、风扇起飞；
      * 每次只处理一个，随时可能被用户的搜索/索引打断（下一轮检查时会跳过去），
        进度是持久化的，中断了下次接着来；
      * 单点失败（模型没起、超时）只影响这一个文件，不会让整批卡住。
    触发条件（全部满足）：至少开了一个处理开关 + 对应的服务配置好 +
      空闲超过 idle_minutes 分钟 + 没有在建索引/没有正在处理 + 本轮还没到 idle_batch 个。
    打标签与转写**轮流**取（_IDLE["turn"] 每完成一个就翻转），否则待打标的图片一多，
    转写会被一直饿着。
    """
    while True:
        try:
            time.sleep(20)
            st = load_settings()
            idle_tag = bool(st.get("idle_tag")) and bool(st.get("ai_enabled"))
            idle_asr = bool(st.get("idle_asr")) and bool(st.get("asr_enabled"))
            if not (idle_tag or idle_asr):
                continue
            # 打标/转写/纠偏/建索引互斥 —— 顺带把正在跑的「智能纠偏」也算进来，
            # 否则用户点了「优化全部」之后，闲置补全还会照旧开跑，两边同时打 oMLX。
            heavy = _heavy_running(exclude="idle")
            if heavy:
                _IDLE["window"] = None          # 用户回来了/正在忙 → 本轮计数作废
                continue
            # ---- 「立即补全」模式 ----
            # 用户在设置页点了「立即补全」时，**不看空闲时长、不看本轮上限**，
            # 一路补到没有待处理的为止。这是必要的：默认要连续空闲
            # idle_minutes 分钟才动一个，而用户只要打开着页面、时不时搜一下，
            # 那个条件可能一整天都凑不齐，结果就是「加进来的图永远没有标签，
            # 用文字搜画面搜不到」。
            man = _IDLE.get("manual")
            manual_on = bool(man) and not man.get("stop") and not man.get("finished")
            if manual_on:
                want_kind = man.get("what") or "both"
                idle_tag = idle_tag and want_kind in ("tag", "both")
                idle_asr = idle_asr and want_kind in ("asr", "both")
                if not (idle_tag or idle_asr):
                    _IDLE["manual"] = None      # 开关在跑的过程中被关掉了
                    continue
                need, limit, total_run = 0.0, 0, 0
            else:
                idle_sec = time.time() - _STATE.get("last_user_req",
                                                   _STATE.get("last_req", 0))
                need = float(st.get("idle_minutes", 5) or 5) * 60
                if idle_sec < need:
                    continue
                # 一段连续空闲算「一轮」；窗口编号变了就把本轮计数清零
                win = int(time.time() // max(need, 60))
                if _IDLE.get("window") != win:
                    _IDLE["window"] = win
                    _IDLE["run_tagged"] = 0
                    _IDLE["run_asr"] = 0
                    _IDLE["run_fail"] = 0
                limit = int(st.get("idle_batch", 10) or 10)
                # 失败也占名额：不然同一批失败文件会被无限重试、
                # 计数永远停在 0/10（用户看到的就是「卡住不动」）
                total_run = (_IDLE.get("run_tagged", 0) + _IDLE.get("run_asr", 0)
                             + _IDLE.get("run_fail", 0))
                if limit > 0 and total_run >= limit:
                    continue

            # 轮流取：上一次是打标签，这次就先看转写，反之亦然
            order = ["tag", "asr"] if (_IDLE.get("turn", 0) % 2 == 0) else ["asr", "tag"]
            job = None
            all_cooled = False
            for want in order:
                if want == "tag" and idle_tag:
                    pend = ix.ai_tag_status()["pending"]
                elif want == "asr" and idle_asr:
                    pend = _asr_pending_list()
                else:
                    continue
                if not pend:
                    continue
                left = len(pend)
                pick = _pick_pending(pend)
                if pick is None:
                    all_cooled = True        # 这一类全在冷藏里，换另一类看看
                    continue
                job = (want, pick, left)
                break
            if not job:
                if all_cooled:
                    n = _cooled_count()
                    _IDLE["last_error"] = (f"{n} 个素材连续失败，已冷藏 "
                                           f"{_FAIL_COOL // 60} 分钟后再试")
                    # ★ 不要把「立即补全」标记为完成 —— 那会让用户点了补全却
                    # 看到它「结束了」，而 187 个待办一个没动。冷藏到期会自动
                    # 接着跑，这里只是继续等（外圈 20 秒一轮）。
                    print(f"[闲置处理] 待办全部处于失败冷藏中（{n} 个），等冷藏过期再试")
                    continue
                if manual_on:
                    man["finished"] = time.time()
                    print(f"[立即补全] 完成（成功 {man.get('done', 0)} 个"
                          f"、失败 {man.get('failed', 0)} 个）")
                continue

            kind, path, left = job
            label = "打标" if kind == "tag" else "转写"
            tag = "立即补全" if manual_on else f"闲置{label}"
            if manual_on:
                _IDLE["last_path"] = path
                _IDLE["last_kind"] = kind
                _IDLE["last_ts"] = time.time()
                print(f"[立即补全] {os.path.basename(path)}（剩 {left} 个）")
            else:
                _IDLE["last_path"] = path
                _IDLE["last_kind"] = kind
                _IDLE["last_ts"] = time.time()
                print(f"[闲置{label}] {os.path.basename(path)}"
                      f"（剩 {left} 个，本轮第 {total_run + 1}/{limit}）")
            try:
                r = (ai_tag_one(path, st) if kind == "tag"
                     else asr_transcribe_one(path, st))
            except Exception as e:
                # 单个文件炸了不许带走整批：包装成普通失败，交给下面的账本
                r = {"ok": False, "message": f"{type(e).__name__}: {e}"}
            _IDLE["turn"] = _IDLE.get("turn", 0) + 1
            if r.get("ok"):
                _clear_fail(path)
                _IDLE["consec_fail"] = 0
                key = "run_tagged" if kind == "tag" else "run_asr"
                _IDLE[key] = _IDLE.get(key, 0) + 1
                _IDLE["last_error"] = None
                if manual_on:
                    man["done"] = man.get("done", 0) + 1
                if kind == "tag":
                    print(f"[{tag}] 完成 {os.path.basename(path)}"
                          f" · {len(r.get('tags') or [])} 个标签 · {r.get('seconds')}s")
                else:
                    print(f"[{tag}] 完成 {os.path.basename(path)}"
                          f" · {r.get('chars')} 字 · {r.get('seconds')}s")
            else:
                msg = r.get("message") or "未知错误"
                _IDLE["last_error"] = msg
                _IDLE["run_fail"] = _IDLE.get("run_fail", 0) + 1
                _IDLE["consec_fail"] = _IDLE.get("consec_fail", 0) + 1
                if manual_on:
                    man["failed"] = man.get("failed", 0) + 1
                # ★ 分开记账：服务整体不可用（连接失败/504/超时）**不写单文件黑名单**。
                # 否则 200 个待办会被逐个冷藏 3 次，最后全进冷藏 → 「整批再也不动」。
                if _is_systemic_fail(msg):
                    _IDLE["skip_count"] = _IDLE.get("skip_count", 0) + 1
                    _IDLE["last_error"] = f"{msg}（服务不可用，稍后整体重试）"
                    print(f"[{tag}] 服务不可用，跳过 {os.path.basename(path)}: {msg}")
                else:
                    n = _note_fail(path, msg)
                    if n >= _FAIL_MAX:
                        print(f"[{tag}] 失败 {os.path.basename(path)}"
                              f"（已连败 {n} 次，冷藏 {_FAIL_COOL // 60} 分钟再试）: {msg}")
                    else:
                        print(f"[{tag}] 失败 {os.path.basename(path)}: {msg}")
                # 只对「服务整个没起」这种连续失败做全局退避；
                # 单个坏文件不再连累整批陪等 2 分钟。
                if _IDLE.get("consec_fail", 0) >= 3:
                    print(f"[闲置处理] 连续失败 {_IDLE['consec_fail']} 次，退避 120 秒")
                    time.sleep(120)
                    _IDLE["consec_fail"] = 0
        except Exception as e:
            _IDLE["last_error"] = f"{type(e).__name__}: {e}"
            print(f"[闲置处理] 跳过：{e}")


def _idle_autostart():
    st = load_settings()
    now0 = time.time()
    # 刚启动一律算「用户在用」，别一开机就抢算力
    _STATE["last_req"] = now0
    _STATE["last_user_req"] = now0
    threading.Thread(target=_idle_loop, daemon=True,
                     name="idle-worker").start()
    if st.get("model_lazy", True):
        threading.Thread(target=_model_reaper, daemon=True,
                         name="model-reaper").start()
        print(f"[模型] 按需加载已开启：闲置 {st.get('model_idle_minutes', 5)} 分钟"
              f"自动卸载，把内存还给系统")
    else:
        print("[模型] 常驻模式：加载后不再自动卸载")
    print(f"[闲置处理] 已启动（打标签 {'开' if st.get('idle_tag') else '关'}"
          f" · 转写 {'开' if st.get('idle_asr') else '关'}"
          f" · 空闲 {st.get('idle_minutes', 5)} 分钟后开始"
          f" · 每轮最多 {st.get('idle_batch', 10)} 个）")


def _gc_autostart(delay: float = 6.0):
    """启动后延迟几秒做一次静默自检，只清「文件已不在」的记录。"""
    def run():
        time.sleep(delay)
        try:
            if _STATE["busy"]:
                return
            r = gc_index(orphans=False)
            if r.get("error"):
                print(f"[启动自检] 跳过：{r['error']}")
                return
            n = r.get("removed") or 0
            fp_n = r.get("fp_removed") or 0
            if n:
                print(f"[启动自检] 清理 {n} 个已删除文件的失效记录"
                      f"（向量 -{r.get('vectors_removed', 0)}"
                      + (f"、指纹 -{fp_n}" if fp_n else "") + "）")
            elif fp_n:
                print(f"[启动自检] 清理 {fp_n} 条指纹库残留（文件已不存在）")
            else:
                print(f"[启动自检] 索引记录正常（{r.get('kept', 0)} 条，无失效项）")
            if r.get("orphan"):
                print(f"[启动自检] 另有 {r['orphan']} 条记录文件仍在、但不属于任何索引源"
                      f"（未自动删除，可在设置页手动清理）")
        except Exception as e:
            print(f"[启动自检] 跳过：{e}")
    threading.Thread(target=run, daemon=True, name="gc-autostart").start()


def sources_with_stats() -> list:
    """给每个索引源附上文件数/向量数（路径做 realpath 规范化后比对）。"""
    srcs = load_sources()
    try:
        con = sqlite3.connect(ix.DB_PATH)
        rows = con.execute("SELECT path, COUNT(*) FROM items GROUP BY path").fetchall()
        con.close()
    except Exception:
        rows = []
    # 全部索引路径先规范化，避免 /tmp vs /private/tmp 这类差异
    norm = {}
    for p, c in rows:
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        norm[rp] = norm.get(rp, 0) + c

    out = []
    for s in srcs:
        try:
            base = os.path.realpath(s["path"]).rstrip("/")
        except Exception:
            base = s["path"].rstrip("/")
        files = [fp for fp in norm if fp == base or fp.startswith(base + "/")]
        out.append({**s, "files": len(files),
                    "vectors": sum(norm[fp] for fp in files),
                    "exists": os.path.isdir(s["path"])})
    return out


def index_sources(kinds=None) -> dict:
    """对全部已添加索引源执行增量索引（刷新新增/变更文件）。

    kinds: 可选 list —— 只扫这些类型（image/video/audio/document）。
           由「刷新」按钮按当前选中的分类传入；None/空 = 全局扫描。
    """
    srcs = [s["path"] for s in load_sources() if os.path.isdir(s["path"])]
    if not srcs:
        return {"error": "还没有添加任何索引目录"}
    return do_index(srcs, kinds=kinds)



def _path_allowed(path: str) -> bool:
    rp = os.path.realpath(os.path.expanduser(path))
    for root in ALLOW_ROOTS:
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _ix_asr_status() -> dict:
    """`ix.asr_status()` 的包装：把**当前的时长上限**一并传进去。

    ★ 必须包一层。indexer 那份清单要拿上限才能判断「这个跳过标记还作不作数」，
    不传的话，因「超过老上限 600 秒」被跳过的长音频（整张专辑 / 演唱会现场）
    会永远停留在待转写清单之外 —— 用户把上限调到不限也救不回来。
    见 `indexer._asr_skip_lifted`。
    """
    try:
        mx = int(load_settings().get("asr_max_duration") or 0)
    except Exception:
        mx = 0
    return ix.asr_status(max_duration=mx)


def _asr_pending_list() -> list:
    return _ix_asr_status().get("pending") or []



    """把整份设置写盘（0600）。save_settings 与一次性迁移共用。"""
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    # 设置里有 API Key，权限收到 0600（只有当前用户可读写）
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


def _migrate_settings(s: dict) -> bool:
    """对老设置做一次性修正。返回是否改动过（需要回写）。

    ★ 为什么要机制而不是直接改 DEFAULT_SETTINGS：`load_settings` 是
    「默认值 ← 用户已存的值」，用户那份里**冻结着老默认值**，改 DEFAULT_SETTINGS
    对他们完全无效。实测：asr_max_duration 老默认 600 被写进 settings.json 后，
    把默认改成 0 也救不了他们 —— 除非把冻结的 600 也搬走。

    每条迁移跑过一次就记进 `_migrations`，不再重复（用户之后自己改回 600 不会被再动）。
    """
    done = s.get("_migrations")
    if not isinstance(done, list):
        done = []
    changed = False

    if "asr_max_duration:600->0" not in done:
        # 600 曾经**只是**默认值（界面上从没建议过这个数），所以存着 600 基本等于
        # 「没动过」。这一步是放宽，不会让任何人少转写；真想要上限的人自己填。
        if int(s.get("asr_max_duration") or 0) == 600:
            s["asr_max_duration"] = 0
        done.append("asr_max_duration:600->0")
        changed = True

    if changed:
        s["_migrations"] = done
    return changed


def load_settings() -> dict:
    """合并「代码默认值」与「用户已保存的设置」。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    # ★ 版本号只认代码，不认用户数据目录里那份。
    # 为什么必须强制覆盖：save_settings 从来不写 version（见下方 `k != "version"`），
    # 但**旧版本曾经写进去过**，于是 settings.json 里冻结着一个陈旧值（实测是
    # "1.0.0"）。它会在上面的 update() 里把 DEFAULT_SETTINGS 的新版本号盖掉，
    # 结果就是「界面显示 v1.0.0、安装包却是新版本」——版本号看着永远不同步。
    s["version"] = DEFAULT_SETTINGS["version"]
    try:
        if _migrate_settings(s):
            _persist_settings(s)
    except Exception:
        pass
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in patch and k != "version":
            s[k] = patch[k]
    # 尺寸设置即时生效
    try:
        import embed as _we
        if "image_max_side" in s:
            _we.set_image_max_side(int(s["image_max_side"]))
    except Exception:
        pass
    _persist_settings(s)
    return s


def cache_info() -> dict:
    """统计预览缓存占用。"""
    d = P.THUMB_DIR
    files, total = 0, 0
    if os.path.isdir(d):
        for root, _, fs in os.walk(d):
            for fn in fs:
                try:
                    total += os.path.getsize(os.path.join(root, fn)); files += 1
                except OSError:
                    pass
    return {"files": files, "bytes": total,
            "mb": round(total / 1048576, 2),
            "limit_mb": load_settings().get("cache_limit_mb", 1024)}


def seed_thumb(name: str, width) -> dict:
    """把示例素材里的某个文件缩成小图，返回 dataURL —— 给「最近上传」演示条目用。

    只认 seed/assets、seed/tryme 下的文件名（不允许带路径），所以不存在路径穿越问题。
    """
    try:
        w = max(48, min(480, int(width)))
    except (TypeError, ValueError):
        w = 160
    base = os.path.basename(name or "")
    if not base:
        return {"thumb": ""}
    path = ""
    # 两个种子目录都找一遍；再找不到就去用户机器上铺好的那两个目录找
    for d in (os.path.join(SEED.SEED_DIR, "assets"),
              os.path.join(SEED.SEED_DIR, "tryme"),
              SEED.DEMO_DIR, SEED.TRYME_DIR):
        cand = os.path.join(d, base)
        if os.path.exists(cand):
            path = cand
            break
    if not path:
        return {"thumb": ""}
    try:
        from PIL import Image
        im = Image.open(path)
        im.thumbnail((w, w))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=78)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"thumb": "data:image/jpeg;base64," + b64}
    except Exception as e:
        return {"thumb": "", "error": str(e)}


def seed_bytes(name: str) -> dict:
    """把体验素材整个读出来，返回 dataURL —— 给「最近上传」演示条目跑真检索用。

    只认文件名（os.path.basename 兜一层），不接受路径，所以不存在路径穿越。
    """
    base = os.path.basename(name or "")
    if not base:
        return {"data": ""}
    for d in (os.path.join(SEED.SEED_DIR, "tryme"),
              os.path.join(SEED.SEED_DIR, "assets"),
              SEED.TRYME_DIR, SEED.DEMO_DIR):
        cand = os.path.join(d, base)
        if os.path.exists(cand):
            try:
                ext = os.path.splitext(base)[1].lower()
                mime = {".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".webp": "image/webp",
                        ".gif": "image/gif", ".mp3": "audio/mpeg",
                        ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(
                            ext, "application/octet-stream")
                with open(cand, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                return {"data": "data:%s;base64,%s" % (mime, b64),
                        "name": base}
            except OSError as e:
                return {"data": "", "error": str(e)}
    return {"data": ""}


def clear_cache() -> dict:
    import shutil
    d = P.THUMB_DIR
    n = 0
    if os.path.isdir(d):
        n = len(os.listdir(d))
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return {"removed": n, "cache": cache_info()}


# ---------- 界面历史（搜索记录 / 最近上传）的服务端副本 ----------
# 为什么要有它：这两个列表原来只存在浏览器的 localStorage 里，而桌面版跑在
# WKWebView 里 —— 那份存储是跟着 WebKit 走的：换一种启动方式、清一次站点数据、
# 或者 pywebview 用默认的 private_mode（每次建窗清空整站数据）都会没。用户在
# 桌面版里「删过的记录又回来 / 打开一次全没了」就是这么来的。
# 所以真正的家放在用户数据目录的 history.json 里，localStorage 只当打开页面
# 立刻能渲染的本地缓存；前端启动时先拉一次、改完再写回来。
#
# 演示记录（那 4 条搜索 + 2 条上传）也由**后端**在首次启动时写进这个文件，
# 不再由前端往 localStorage 里注：前端注的话，用户清空之后「是不是补过了」
# 只记在浏览器那边，一旦那边被清就又补回来 —— 删了等于没删。

_UI_HIST_FILE = "history.json"
_UI_HIST_MAX_SEARCH = 50
_UI_HIST_MAX_UPLOADS = 30


def _ui_hist_path() -> str:
    return os.path.join(P.DATA_DIR, _UI_HIST_FILE)


def _ui_hist_read() -> dict:
    """原样读 history.json（不带 seed）。读坏了当空 dict。"""
    try:
        with open(_ui_hist_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _ui_hist_write(d: dict) -> str:
    """原子写（先 .tmp 再 os.replace）。返回错误信息，空串 = 成功。"""
    p = _ui_hist_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
        return ""
    except OSError as e:
        return str(e)


def ui_history_get() -> dict:
    """读界面历史。文件不存在/写坏了都当空，不让它拖垮首页。"""
    d = _ui_hist_read()
    return {
        "search": d.get("search") if isinstance(d.get("search"), list) else [],
        "uploads": d.get("uploads") if isinstance(d.get("uploads"), list) else [],
        "seedOff": bool(d.get("seedOff")),
        "seeded": bool(d.get("seeded")),
        "path": _ui_hist_path(),
    }


def ui_history_put(body: dict) -> dict:
    """整体覆盖式保存（前端手里就是全量列表）。

    只认识的键才收；`seeded` 是「演示记录已经注过了」的标记，原样带过去 ——
    丢了它下次启动就会再注一遍，用户清空的记录又回来了。
    """
    body = body if isinstance(body, dict) else {}
    raw = _ui_hist_read()
    cur = ui_history_get()
    out = {"search": cur["search"], "uploads": cur["uploads"],
           "seedOff": cur["seedOff"], "seeded": cur["seeded"]}
    if isinstance(body.get("search"), list):
        out["search"] = body["search"][:_UI_HIST_MAX_SEARCH]
    if isinstance(body.get("uploads"), list):
        out["uploads"] = body["uploads"][:_UI_HIST_MAX_UPLOADS]
    if "seedOff" in body:
        out["seedOff"] = bool(body.get("seedOff"))
    for k in ("seeded", "seeded_at"):
        if k in raw:                      # 只保留已有的，不允许前端改
            out[k] = raw[k]

    p = _ui_hist_path()
    err = _ui_hist_write(out)
    if err:
        return {"error": "写入失败：%s" % err, "path": p}
    return {"ok": True, "path": p,
            "search": len(out["search"]), "uploads": len(out["uploads"])}


def ui_history_seed_if_needed() -> dict:
    """首次启动把演示记录写进用户数据目录（**后端**注，不走前端）。

    判据是文件里的 `seeded` 标记，而不是「列表是不是空的」：
    - 没有标记且两个列表都空 → 注一份演示记录，并打上 seeded；
    - 没有标记但列表非空（老版本前端注过的那批）→ 只补标记，绝不覆盖用户数据；
    - 有标记 → 什么都不做。所以用户清空之后**删了就是删了**，
      重启、清 WebKit 数据、换浏览器都不会再冒出来。
    """
    d = _ui_hist_read()
    if d.get("seeded"):
        return {"seeded": False, "reason": "已注过"}

    has_search = isinstance(d.get("search"), list) and len(d["search"]) > 0
    has_uploads = isinstance(d.get("uploads"), list) and len(d["uploads"]) > 0
    if has_search or has_uploads:
        d["seeded"] = True                # 认下现状，别覆盖
        _ui_hist_write(d)
        return {"seeded": False, "reason": "已有记录，只补标记"}

    demo = {}
    try:
        demo = SEED.demo_history() or {}
    except Exception as e:
        return {"seeded": False, "reason": "读种子失败：%s" % e}

    now = int(time.time() * 1000)
    search = []
    for i, x in enumerate(demo.get("searchHistory") or []):
        if isinstance(x, dict) and x.get("t"):
            # 种子里第一条就是「最近搜的」，所以第一条给最新的 ts（倒过来会给反）
            search.append({"t": x["t"], "k": x.get("k") or "text",
                           "ts": x.get("ts") or (now - i * 1000)})
    if len(search) > _UI_HIST_MAX_SEARCH:
        search = search[:_UI_HIST_MAX_SEARCH]

    uploads = []
    for i, it in enumerate(demo.get("recentUploads") or []):
        if not (isinstance(it, dict) and it.get("name")):
            continue
        thumb = ""
        if it.get("type") == "image":
            try:
                thumb = (seed_thumb(it["name"], "160") or {}).get("thumb") or ""
            except Exception:
                thumb = ""
        uploads.append({"type": it.get("type") or "image", "name": it["name"],
                        "thumb": thumb, "ts": now - i * 1000, "seed": True})
    if len(uploads) > _UI_HIST_MAX_UPLOADS:
        uploads = uploads[:_UI_HIST_MAX_UPLOADS]

    out = {"search": search, "uploads": uploads, "seedOff": False,
           "seeded": True, "seeded_at": now}
    err = _ui_hist_write(out)
    if err:
        return {"seeded": False, "reason": "写入失败：%s" % err}
    return {"seeded": True, "search": len(search), "uploads": len(uploads),
            "path": _ui_hist_path()}


def about_info() -> dict:
    """「关于」页要的一小组事实：版本、构建时间、引擎、数据位置、规模。

    全部本机现算，不联网 —— 这个应用没有在线更新源，所以「检查更新」
    只能告诉用户「你跑的就是当前这份本地构建」，不能假装去比对了远端。
    """
    import platform as _pl
    ap = os.path.join(HERE, "app.py")
    try:
        built = int(os.path.getmtime(ap))
    except OSError:
        built = 0
    try:
        st = scoped_stats()
        n_files, n_vec = st.get("files", 0), st.get("vectors", 0)
    except Exception:
        n_files, n_vec = 0, 0
    mp = _STATE.get("model_path") or ""
    lang = (load_settings() or {}).get("lang") or "zh"
    eng = "未加载（首次检索时载入）" if lang == "zh" else "Not loaded (loads on first search)"
    if mp:
        # 尽量报一个人类看得懂的名字：从 config.json 里取架构/量化，
        # 取不到就退回目录名。完整路径放在 engine_path 里给 title 用。
        try:
            with open(os.path.join(mp, "config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            arch = (cfg.get("architectures") or [""])[0]
            ver = {"qwen3_5": "Qwen3.5", "qwen3": "Qwen3", "qwen2_5": "Qwen2.5",
                   "qwen2": "Qwen2"}.get(cfg.get("model_type", ""), arch or "")
            bits = (cfg.get("quantization") or {}).get("bits")
            eng = ver or os.path.basename(mp.rstrip("/"))
            if bits:
                eng += (" · %sbit 量化" if lang == "zh" else " · %s-bit quantized") % bits
        except Exception:
            eng = os.path.basename(os.path.dirname(mp.rstrip("/")).rstrip("/")) \
                  + "/" + os.path.basename(mp.rstrip("/"))
    # 图片类自带资源的「版本号」= assets/ 里最新的 mtime。
    # 前端把它当 ?v= 拼到图片 URL 上，换了图 URL 就变，浏览器必然重新拉 ——
    # 光靠 Cache-Control 救不了：用户浏览器里可能已经存了旧的「新鲜」缓存，
    # 那份旧缓存根本不会回服务器问，也就看不到我们后来改的头。
    adv = 0
    adir = os.path.join(HERE, "assets")
    try:
        for fn in os.listdir(adir):
            try:
                adv = max(adv, int(os.path.getmtime(os.path.join(adir, fn))))
            except OSError:
                pass
    except OSError:
        pass
    return {
        "app": SERVICE_NAME,
        "name": "FXseek 媒体库",
        "version": load_settings().get("version", DEFAULT_SETTINGS["version"]),
        "built_at": built,
        "asset_ver": adv,
        "python": _pl.python_version(),
        "platform": "%s %s (%s)" % (_pl.system(), _pl.release(), _pl.machine()),
        "engine": eng,
        "engine_path": mp,
        "data_dir": P.DATA_DIR,
        "files": n_files,
        "vectors": n_vec,
        # 用户当前配置的 AI 打标模型（「推理引擎」那格显示的是它，而非内置嵌入模型）
        "ai_enabled": bool(load_settings().get("ai_enabled")),
        "ai_model": (load_settings().get("ai_model") or "").strip(),
    }


# ---------------------------------------------------------------- 检查更新
# ★ 项目地址只有这一处（前端那个 Star 卡片的 href 是另一处，改版本仓库时两边都要动）。
GH_REPO = "FRr688/FXseek"
GH_RELEASES = f"https://github.com/{GH_REPO}/releases"

_UPDATE_TTL = 600.0                     # 10 分钟内不重复联网（用户可能连点）
_UPDATE_CACHE = {"at": 0.0, "data": None}


def _ver_key(s: str):
    """把版本号变成可排序的元组。

    ★ 关键点：带后缀的（1.0.4test10）必须排在同号正式版（1.0.4）**前面**，
    否则装了 test 版的用户会一直被告知"有新版 1.0.4"却下不动。
    """
    import re
    s = (s or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", s)
    if not m:
        return (0, 0, 0, 0, "")
    a, b, c = (int(x or 0) for x in m.group(1, 2, 3))
    tail = (m.group(4) or "").strip("-_. ")
    return (a, b, c, 0 if tail else 1, tail)


def _gh_fetch(url, timeout=10):
    """拉一个 URL 的正文（GitHub API / atom feed）。

    ★ 为什么用 /usr/bin/curl 而不是 urllib：
      - 这台机器（以及不少开了代理/VPN 的环境）走的是 TLS 中间人，服务端证书
        由本地根签发；Python 的 OpenSSL **只认 certifi / openssl 的 CA 目录，
        不读 macOS 钥匙串**，于是必然 `CERTIFICATE_VERIFY_FAILED`（certifi、
        /private/etc/ssl/cert.pem、load_default_certs 三条路实测全失败）。
      - 系统 curl 用钥匙串，同一时刻 `curl` 拿到 HTTP 200。
      只跑 macOS、curl 是系统自带，所以直接用它，既不用打包 CA、也天然尊重
      用户机器上已经装好的根证书。
    超时必须短 —— 用户点了按钮在等。
    """
    import subprocess
    cmd = ["/usr/bin/curl", "-sSL", "--fail", "--max-time", str(int(timeout)),
           "--compressed",
           "-H", "Accept: application/vnd.github+json",
           "-H", "User-Agent: FXseek/%s" % DEFAULT_SETTINGS["version"],
           url]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError("curl 退出码 %d：%s" % (r.returncode, msg or "未知错误"))
    return r.stdout.decode("utf-8", "replace")


def _gh_json(url, timeout=10):
    return json.loads(_gh_fetch(url, timeout=timeout))


def _gh_latest_via_atom(timeout=10):
    """兜底：解析 releases.atom。

    GitHub API 有匿名限流（每小时 60 次，共享出口 IP 时很容易撞上），
    而 atom feed 不限流、也不用 token，代价是只有 tag 没有下载链接。

    ★ entry 的 <title> 是**发布标题**（可能是「FXseek 1.0.3」这种自由文本），
    不是 tag。所以优先从 <link href="…/releases/tag/v1.0.3"> 里取真正的 tag，
    取不到再退回从标题里抠一个版本号形状的串 —— 否则 "FXseek 1.0.3" 会被
    _ver_key 解析成 0.0.0，害得用户永远看不到更新。
    """
    import re as _re
    xml = _gh_fetch("https://github.com/%s/releases.atom" % GH_REPO, timeout=timeout)
    entry = _re.search(r"<entry>(.*?)</entry>", xml, _re.S)
    if not entry:
        return None
    body = entry.group(1)
    tag = ""
    m = _re.search(r'<link[^>]*href="([^"]*?/releases/tag/([^"/]+))"', body, _re.S)
    if m:
        tag = m.group(2).strip()
    if not tag:
        m = _re.search(r"<title>(.*?)</title>", body, _re.S)
        if m:
            raw = _re.sub(r"<[^>]+>", "", m.group(1)).strip()
            v = _re.search(r"v?\d+(?:\.\d+)+(?:[-._]?[A-Za-z0-9]+)*", raw)
            tag = v.group(0) if v else raw
    if not tag:
        return None
    url = m.group(1) if m else GH_RELEASES
    return {"tag": tag, "url": url, "notes": "", "at": "", "assets": []}


def update_check(force=False) -> dict:
    """查一下 GitHub 上有没有比本机新的版本。

    三种结果都如实返回，前端照实说：
      ok=True,  has_update=True   —— 有新版本
      ok=True,  has_update=False  —— 已是最新
      ok=False                    —— 查不动（没网 / 被墙 / 限流 / 仓库还没有 release）
                                     前端这时会亮出「releases」按钮，让人自己去翻。
    """
    cur = load_settings().get("version", DEFAULT_SETTINGS["version"])
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and (now - _UPDATE_CACHE["at"]) < _UPDATE_TTL:
        return _UPDATE_CACHE["data"]

    rel, err = None, ""
    # ① 首选官方 API（有下载链接、更新说明、发布时间）
    try:
        d = _gh_json("https://api.github.com/repos/%s/releases/latest" % GH_REPO)
        rel = {"tag": d.get("tag_name") or d.get("name") or "",
               "url": d.get("html_url") or GH_RELEASES,
               "notes": (d.get("body") or "").strip(),
               "at": d.get("published_at") or "",
               "prerelease": bool(d.get("prerelease")),
               "assets": [{"name": a.get("name"), "url": a.get("browser_download_url"),
                           "size": a.get("size")}
                          for a in (d.get("assets") or [])]}
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
        # ② 退到 atom feed（不限流）
        try:
            rel = _gh_latest_via_atom()
        except Exception as e2:
            err = "%s / %s" % (err, e2)

    if not rel or not rel.get("tag"):
        out = {"ok": False, "current": cur, "releases_url": GH_RELEASES,
               "error": err or "没有读到版本信息（仓库可能还没有 Release）"}
        _UPDATE_CACHE.update({"at": now, "data": out})
        return out

    latest = rel["tag"].strip()
    out = {"ok": True, "current": cur, "latest": latest,
           "has_update": _ver_key(latest) > _ver_key(cur),
           "url": rel.get("url") or GH_RELEASES,
           "notes": rel.get("notes") or "",
           "published_at": rel.get("at") or "",
           "prerelease": bool(rel.get("prerelease")),
           "assets": rel.get("assets") or [],
           "releases_url": GH_RELEASES}
    _UPDATE_CACHE.update({"at": now, "data": out})
    return out


def _load_trash() -> list:
    try:
        with open(TRASH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_trash(items: list):
    os.makedirs(os.path.dirname(TRASH_PATH), exist_ok=True)
    with open(TRASH_PATH, "w", encoding="utf-8") as f:
        json.dump(items[-500:], f, ensure_ascii=False, indent=2)


TRASH_TTL_DAYS = 30      # 最近删除保留天数
TRASH_TTL_SEC = TRASH_TTL_DAYS * 86400


def _purge_expired(items):
    """清理超过 30 天的条目，并真正删除其原文件（移到废纸篓）。"""
    now = time.time()
    expired = [it for it in items
               if (it.get("expires_at") and now > it["expires_at"])]
    keep = [it for it in items if it not in expired]
    for it in expired:
        try:
            _trash_file(it["path"])      # 30 天后彻底删除（移废纸篓）
        except Exception as e:
            print(f"  [到期删除失败] {it['path']}: {e}")
    return keep


def _trash_file(path: str):
    """把文件移到 macOS 废纸篓。"""
    import subprocess
    if not os.path.exists(path):
        return
    # 优先用原生 trash（若有），否则用 osascript 移到废纸篓
    subprocess.run(["/usr/bin/osascript", "-e",
                    'tell application "Finder" to delete POSIX file "{}"'.format(path)],
                   capture_output=True, timeout=30)


def trash_add(paths: list) -> dict:
    op_log("暂存", "、".join(os.path.basename(p) for p in paths))
    """把文件暂存到「最近删除」（30 天不恢复则自动删除，仅从索引移除，不删原文件）。"""
    items = _load_trash()
    items = _purge_expired(items)         # 先清到期项
    now = time.time()
    for p in paths:
        items.append({"path": p, "removed_at": now,
                      "expires_at": now + TRASH_TTL_SEC})
    _save_trash(items)
    # 从索引中移除
    con = ix._init_db(ix.DB_PATH)
    for p in paths:
        con.execute("DELETE FROM items WHERE path=?", (p,))
    con.commit(); con.close()
    return {"moved": len(paths), "total": len(items), "ttl_days": TRASH_TTL_DAYS}


def trash_list() -> dict:
    items = _load_trash()
    items = _purge_expired(items)
    _save_trash(items)
    now = time.time()
    out = []
    for it in items:
        p = it["path"]
        exp = it.get("expires_at", 0)
        days_left = max(0, int((exp - now) / 86400)) if exp else 0
        out.append({"path": p, "name": os.path.basename(p),
                    "kind": _kind_of(p), "removed_at": it.get("removed_at"),
                    "expires_at": exp, "days_left": days_left,
                    "exists": os.path.exists(p)})
    out.reverse()          # 最新的在前
    return {"count": len(out), "items": out, "ttl_days": TRASH_TTL_DAYS}


def trash_restore(paths: list) -> dict:
    """从最近删除恢复（重新加入索引）。"""
    op_log("恢复", "、".join(os.path.basename(p) for p in paths))
    items = _load_trash()
    keep, restored = [], []
    for it in items:
        if it["path"] in paths:
            restored.append(it["path"])
        else:
            keep.append(it)
    _save_trash(keep)
    # 恢复的文件重新建立索引（若文件仍存在）
    if restored:
        existing = [p for p in restored if os.path.exists(p)]
        if existing:
            do_index(existing)
    return {"restored": restored, "total": len(keep)}


def trash_delete_permanent(paths: list) -> dict:
    """彻底删除：从最近删除列表移除，并把原文件移到废纸篓。"""
    op_log("删除", "、".join(os.path.basename(p) for p in paths), level="warn")
    items = _load_trash()
    keep, deleted = [], []
    for it in items:
        if it["path"] in paths:
            deleted.append(it["path"])
            try:
                _trash_file(it["path"])     # 移到废纸篓
            except Exception as e:
                print(f"  [废纸篓失败] {it['path']}: {e}")
        else:
            keep.append(it)
    _save_trash(keep)
    return {"deleted": len(deleted), "total": len(keep)}


def reveal_in_folder(path: str) -> dict:
    """在访达中打开文件所在目录并选中该文件。"""
    import subprocess
    if not path or not os.path.exists(path):
        return {"error": "文件不存在"}
    subprocess.run(["/usr/bin/open", "-R", path], capture_output=True, timeout=15)
    return {"ok": True}


def open_folder(path: str) -> dict:
    """在访达中打开目录。"""
    import subprocess
    target = path if os.path.isdir(path) else os.path.dirname(path)
    if not target or not os.path.isdir(target):
        return {"error": "目录不存在"}
    subprocess.run(["/usr/bin/open", target], capture_output=True, timeout=15)
    return {"ok": True}


# 外链白名单：页面上的「去点个 Star」是唯一的站外链接，就只开这一个口子。
# 前端本来就是本机页面，但没必要让这个接口变成「任意 URL 都能叫系统打开」的通用开关。
_URL_ALLOW = ("github.com", "www.github.com")


def open_url(url: str) -> dict:
    """用系统默认浏览器打开外链（走 /usr/bin/open，交给用户的默认浏览器）。"""
    import subprocess
    from urllib.parse import urlparse
    u = (url or "").strip()
    try:
        p = urlparse(u)
    except Exception:
        return {"error": "链接不合法"}
    if p.scheme not in ("http", "https") or (p.hostname or "") not in _URL_ALLOW:
        return {"error": "不支持的链接"}
    subprocess.Popen(["/usr/bin/open", u],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


def trash_purge() -> dict:
    items = _load_trash()
    items = _purge_expired(items)
    _save_trash([])
    return {"purged": len(items)}


def _kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in ix.IMAGE_EXT: return "image"
    if ext in ix.VIDEO_EXT: return "video"
    if ext in ix.AUDIO_EXT: return "audio"
    if ext in ix.DOC_EXT: return "document"
    return "other"


def export_config() -> dict:
    """导出可携带配置（不含原始资产与路径敏感信息之外的数据）。"""
    s = load_settings()
    return {
        "app": SERVICE_NAME, "version": DEFAULT_SETTINGS["version"],
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "settings": s,
        "stats": ix.stats(),
        "trash": trash_list(),
    }


def export_config_to_file() -> dict:
    """弹出 macOS 原生保存面板，让用户选导出位置，然后把配置 JSON 写进去。

    WKWebView 里 `<a download>` 不触发下载（会跳成一段 JSON 代码页），所以
    走 osascript 的 `choose file name`（跟 pick_folder 同一套跨进程做法），
    拿回用户挑的路径后由后端直接落盘。浏览器 / 桌面版都适用。
    """
    import subprocess
    payload = export_config()
    default_name = "fxseek-config-%s.json" % time.strftime("%Y%m%d-%H%M%S")
    # 原生保存面板的 prompt 吃不到 i18n.js，按设置语言双语
    _en = ((load_settings() or {}).get("lang") or "zh") == "en"
    prompt_txt = "Export configuration to…" if _en else "导出配置到…"
    script = (
        'try\n'
        '  set f to choose file name with prompt "%s" '
        'default name "%s"\n'
        '  return POSIX path of f\n'
        'on error number -128\n'
        '  return "__CANCELLED__"\n'
        'end try'
    ) % (prompt_txt, default_name)
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script],
                             capture_output=True, text=True, timeout=300)
        path = (out.stdout or "").strip()
        if path == "__CANCELLED__":
            return {"cancelled": True}
        if not path:
            return {"cancelled": True, "error": (out.stderr or "").strip()}
        # 用户可能没输 .json 后缀，补上
        if not path.lower().endswith(".json"):
            path += ".json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"ok": True, "path": path}
    except subprocess.TimeoutExpired:
        return {"cancelled": True, "error": "选择超时"}
    except Exception as e:
        return {"cancelled": True, "error": str(e)}


def import_config(payload: dict) -> dict:
    s = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(s, dict):
        raise ValueError("无效的配置：缺少 settings 字段")
    saved = save_settings(s)
    return {"applied": True, "settings": saved}



# --------------------------------------------------------------------------
# 业务逻辑
# --------------------------------------------------------------------------
def open_with_system(path: str) -> dict:
    """用 macOS 默认应用打开本地文件。

    用于浏览器无法内联预览的格式（WPS / 老式 Office 等）——
    交给系统上装的 WPS / Office 打开，不在应用内重复实现渲染。

    安全：只允许打开「已加入索引源」目录下的文件，且必须是普通文件。
    """
    import subprocess
    if not path:
        return {"error": "缺少文件路径"}
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except Exception as e:
        return {"error": str(e)}
    if not os.path.exists(real):
        return {"error": "文件不存在"}
    if os.path.isdir(real):
        return {"error": "这是一个目录"}

    # 必须在某个索引源目录内（防止被当成任意文件打开器利用）
    roots = []
    for s in load_sources():
        try:
            roots.append(os.path.realpath(s["path"]).rstrip("/"))
        except Exception:
            roots.append(str(s.get("path", "")).rstrip("/"))
    if not roots or not any(real == r or real.startswith(r + os.sep) for r in roots):
        return {"error": "该文件不在已添加的索引源内"}

    try:
        subprocess.Popen(["/usr/bin/open", real],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        op_log("打开", os.path.basename(real))
        return {"status": "ok", "path": real}
    except Exception as e:
        return {"error": str(e)}


def pick_folder() -> dict:
    """弹出 macOS 原生文件夹选择对话框（通过 osascript）。"""
    import subprocess
    _en = ((load_settings() or {}).get("lang") or "zh") == "en"
    script = (
        'try\n'
        '  set f to choose folder with prompt "%s"\n'
        % ("Choose a folder to index" if _en else "选择要建立索引的文件夹") +
        '  return POSIX path of f\n'
        'on error number -128\n'
        '  return "__CANCELLED__"\n'
        'end try'
    )
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script],
                             capture_output=True, text=True, timeout=300)
        path = (out.stdout or "").strip()
        if path == "__CANCELLED__":
            return {"cancelled": True}
        if not path:
            return {"cancelled": True, "error": (out.stderr or "").strip()}
        return {"path": path.rstrip("/"), "cancelled": False}
    except subprocess.TimeoutExpired:
        return {"cancelled": True, "error": "选择超时"}
    except Exception as e:
        return {"cancelled": True, "error": str(e)}


def read_text_preview(path: str, max_chars: int = 200000) -> dict:
    """读取文本类文件内容用于预览（带编码嗅探）。

    注意：绝不把 latin-1 放进自动探测链 —— 它能映射任意字节、永不抛
    UnicodeDecodeError，会把 PDF/DOCX 之类的二进制文件「成功」解成乱码。
    二进制文件必须在调用前被拦下，由前端走下载或 PDF 内联预览。
    """
    if not path or not _path_allowed(path) or not os.path.exists(path):
        raise ValueError("文件不存在或不可访问")
    if os.path.isdir(path):
        raise ValueError("这是一个目录")

    # 扩展名白名单：非文本格式直接拒绝，不尝试解码
    ext = os.path.splitext(path)[1].lower()
    BINARY_EXT = {".pdf", ".doc", ".docx", ".wps", ".rtf", ".xls", ".xlsx", ".xlsm",
                  ".et", ".ppt", ".pptx", ".dps", ".zip", ".gz", ".tar", ".7z",
                  ".rar", ".dmg", ".exe", ".so", ".dylib", ".bin",
                  ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".heic",
                  ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
                  ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
    if ext in BINARY_EXT:
        raise ValueError("这是二进制文件，无法以文本方式预览，请下载后查看")

    # 内容嗅探：出现 NUL 字节或无法严格按 utf-8 解码 → 判为二进制
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except Exception as e:
        raise ValueError(str(e))
    if b"\x00" in head:
        raise ValueError("这是二进制文件，无法以文本方式预览，请下载后查看")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # 允许 GBK/BIG5 等中文编码，但要求整段可解且不是随机字节
        for enc in ("gbk", "big5"):
            try:
                head.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("这不是文本文件，无法预览，请下载后查看")

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    text, enc_used = "", None
    # latin-1 仅在最后兜底，且此时已通过上面的二进制嗅探
    for enc in ("utf-8", "gbk", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read(max_chars)
            enc_used = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception as e:
            raise ValueError(str(e))
    if enc_used is None:
        raise ValueError("无法以文本方式读取（可能是二进制文件）")
    lines = text.splitlines()
    return {
        "path": path,
        "name": os.path.basename(path),
        "encoding": enc_used,
        "size": size,
        "size_text": ix._fmt_size(size),
        "lines": len(lines),
        "chars": len(text),
        "truncated": size > max_chars,
        "ext": os.path.splitext(path)[1].lower(),
        "text": text,
    }


def logs_list() -> dict:
    """返回操作日志（新的在前）。"""
    import time as _t
    items = [{"ts": l["ts"], "time": _t.strftime("%m-%d %H:%M:%S", _t.localtime(l["ts"])),
              "action": l["action"], "detail": l["detail"], "level": l["level"]}
             for l in reversed(LOGS)]
    return {"count": len(items), "items": items}


def logs_clear() -> dict:
    n = len(LOGS)
    LOGS.clear()
    op_log("日志", f"清空了 {n} 条日志")
    return {"cleared": n}


def cache_stats() -> dict:
    """检索相关缓存统计（供设置页展示）。"""
    import fastsearch as fs
    thumb_dir = P.THUMB_DIR
    tf, tb = 0, 0
    if os.path.isdir(thumb_dir):
        for fn in os.listdir(thumb_dir):
            try:
                tb += os.path.getsize(os.path.join(thumb_dir, fn)); tf += 1
            except OSError:
                pass
    vecs = 0
    try:
        con = sqlite3.connect(ix.DB_PATH)
        vecs = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        con.close()
    except Exception:
        pass
    return {"query_cache": fs.qcache_stats(), "vectors": vecs,
            "thumb_files": tf, "thumb_mb": round(tb / 1048576, 2)}


def asr_test(body: dict) -> dict:
    """测试 ASR 服务连通性（可传临时配置）。"""
    import asr_desc
    st = load_settings()
    base = (body.get("base_url") or st.get("asr_base_url") or "").strip()
    key = body.get("api_key") if body.get("api_key") is not None else st.get("asr_api_key", "")
    model = (body.get("model") or st.get("asr_model") or "").strip()
    if not base or not model:
        return {"ok": False, "message": "请先填写 ASR 地址与模型"}
    return asr_desc.test_connection(base, key or "", model)


def _linked_rows(results: list) -> list:
    """把「同一首歌」的其它载体补进结果。

    指纹只能匹配「同一段录音」：视频里用了某首歌的开头、而你拿这首歌的中段去搜，
    是匹配不上的（实测视频用 忆_FR 第 6~19 秒、查询是该曲第 84~91 秒）。
    索引时用「多窗投票 + 底对齐一致」把这些关系挖出来存进 song_links，
    检索命中其中任一个时，就把同一首歌的其它载体一并带出来。
    这些条目带 linked=True，前端要单独标「同一首歌」，不该被匹配门槛藏掉。
    """
    import fingerprint as fp
    if not results:
        return []
    have = {r.get("path") for r in results}
    try:
        rel = fp.related_paths([p for p in have if p])
    except Exception:
        return []
    extra = []
    for path, ev in rel.items():
        if path in have or not os.path.exists(path):
            continue
        kind, dur = fp.get_fp_info(path)
        if kind not in ("audio", "video"):
            continue
        best = max(ev, key=lambda e: e.get("score") or 0)
        extra.append({
            "path": path, "kind": kind,
            "duration": round(dur or 0, 2),
            "score": best.get("score"), "offset_sec": best.get("offset_sec"),
            "name": os.path.basename(path), "exists": True,
            "linked": True, "link_note": "同一首歌",
            "linked_via": os.path.basename(best.get("via") or ""),
            "link_votes": best.get("votes"),
        })
    extra.sort(key=lambda r: -(r.get("score") or 0))
    return extra


def audio_search(body: dict) -> dict:
    """以音搜素材：接收音频片段（base64 或临时路径），用指纹匹配库内文件。"""
    import fingerprint as fp
    import base64 as b64
    import tempfile
    threshold = float(body.get("threshold", 0.30))
    top_k = int(body.get("top_k", 10))

    # 1) 音频来源：base64 数据 或 已存在的临时文件路径
    tmp = None
    data = body.get("audio")
    if data:
        try:
            raw = b64.b64decode(data.split(",", 1)[-1])
        except Exception as e:
            return {"error": f"音频数据解析失败：{e}"}
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    elif body.get("path"):
        tmp = body["path"]
    if not tmp or not os.path.exists(tmp):
        return {"error": "缺少音频数据"}

    try:
        # 以音搜素材：范围就是音频与视频这两类（指纹只对它们有意义）
        results = fp.search(tmp, threshold=threshold, top_k=top_k,
                            kinds=("audio", "video"))
        # 附上文件名与存在性。**文件已经不在的直接丢掉** —— 指纹库是按 path
        # 存的，文件夹改名/移动后老路径会残留在里面；不丢的话结果里会同时出现
        # 「新路径」和「老路径（文件已移动）」两条同名记录。清理由 gc_index()
        # 负责治本，这里是检索侧的兜底。
        out, ghosts = [], 0
        for r in results:
            if not os.path.exists(r["path"]):
                ghosts += 1
                continue
            r["name"] = os.path.basename(r["path"])
            r["exists"] = True
            out.append(r)
        if ghosts:
            op_log("以音搜素材", f"忽略 {ghosts} 条文件已不存在的指纹记录", level="warn")
        linked = _linked_rows(out)
        total = len(out) + len(linked)
        op_log("以音搜素材",
               f"{len(out)} 个匹配" + (f" + {len(linked)} 个同一首歌" if linked else ""))
        if not total:
            # 可能是这段录音本身就提取不出有区分度的指纹（太短 / 太平稳）
            _q, _why = fp.query_quality(tmp)
            if _why:
                return {"count": 0, "results": [], "message": _why}
        return {"count": total, "results": out + linked,
                "linked_count": len(linked)}
    except Exception as e:
        return {"error": f"指纹检索失败：{e}"}
    finally:
        if data and tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# 麦克风录音两条路都没搜到时的说明文案（前端提示卡直接用，保证口径一致）
HUM_MISS_MSG = (
    "哼唱没能匹配到库里已有的音频。指纹只能认出「同一段音频」——滤波、压缩、外放录音都"
    "没问题，但哼成旋律时音高变了就认不出。可以把原曲外放给麦克风听，或者改用文字描述来搜。"
)
BOTH_MISS_MSG = (
    "这段录音没能转成文字，也没匹配到库里已有的音频。"
    "如果是在唱歌，试着把原曲外放给麦克风听（指纹认同一段录音最准）；"
    "如果是在说话，靠近麦克风再说一次。"
)
# ASR 只哼出「嗯。」这类语气词时，拿这两个字去搜只会撞出一屏无关结果
HUM_NO_LYRIC_MSG = (
    "听起来你在哼旋律、没有唱词，转写只得到语气词。"
    "声学指纹认的是「同一段录音」（外放原曲一定能搜到），哼唱的旋律它认不出——"
    "想按旋律找歌的话，得给歌词或者唱出词来。"
)

# 指纹检索的判定已经搬进 fingerprint.search_robust()（逐帧容忍 + 多窗口投票 +
# 对齐一致性三层判据）。这里不再用单一分数阈值——实测外放原曲给麦克风听时，
# 真阳性 0.61~0.78、假阳性 0.71~0.79，两者完全重叠，任何单一阈值都分不开。

_VOICE_FILLER = set("嗯啊哦噢喔呃唉哎诶呀吧哈呵嘿嗨嗯呐咯喽哟")


def _voice_text_ok(t: str) -> bool:
    """ASR 转出来的东西是不是一句能拿去检索的话。

    「嗯。」「啊」这类语气词去搜只会命中一屏无关素材，不如直接告诉用户没听清。
    """
    import re
    s = re.sub(r"[。，、！？…,.!?；;：:\s\"'‘’“”（）()\[\]【】~—-]", "", t or "")
    if len(s) < 2:
        return False
    if set(s) <= _VOICE_FILLER:
        return False
    return len(set(s)) >= 2


def voice_search(body: dict) -> dict:
    """麦克风录音检索：先判断意图（说话 / 哼唱），再走对应链路。

    - 说话 → 调 ASR 转成文字，把 text 交回前端，沿用既有语义检索链路
             （等于用嘴代替键盘，类型词/格式词/门槛这些规则全部照旧生效）
    - 哼唱 → 走 Chromaprint 指纹，只在音频与视频里找同一段音频

    请求：{audio: dataURL 或 path, threshold?, top_k?}
    返回：{ok, intent:'hum'|'speech', ...}
    """
    import base64 as b64
    import tempfile
    import voice_intent as vi
    import fingerprint as fp

    threshold = float(body.get("threshold", 0.30))
    top_k = int(body.get("top_k", 20))

    # 1) 落临时文件；扩展名尽量贴近真实类型（ffmpeg 靠内容探测，扩展名只是保险）
    tmp = None
    auto_tmp = False
    data = body.get("audio")
    if data:
        head = data[:64].lower()
        ext = ".webm"
        if "wav" in head:
            ext = ".wav"
        elif "mpeg" in head or "mp3" in head:
            ext = ".mp3"
        elif "mp4" in head or "m4a" in head or "aac" in head:
            ext = ".m4a"
        elif "ogg" in head:
            ext = ".ogg"
        try:
            raw = b64.b64decode(data.split(",", 1)[-1])
        except Exception as e:
            return {"ok": False, "message": f"录音数据解析失败：{e}"}
        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        auto_tmp = True
    elif body.get("path"):
        tmp = body["path"]
    if not tmp or not os.path.exists(tmp):
        return {"ok": False, "message": "缺少录音数据"}

    def _try_asr():
        """跑一次 ASR，返回 (文本, 错误码, 用到的模型)。未启用时错误码为 'need_asr'。"""
        import asr_desc
        st = load_settings()
        base = (body.get("base_url") or st.get("asr_base_url") or "").strip()
        key = (body.get("api_key") if body.get("api_key") is not None
               else st.get("asr_api_key", ""))
        model = (body.get("model") or st.get("asr_model") or "").strip()
        if not st.get("asr_enabled", False) or not (base and model):
            return None, "need_asr", ""
        try:
            r = asr_desc.transcribe(base, key or "", model, tmp,
                                    language=st.get("asr_language", ""))
        except asr_desc.ASRError as e:
            return None, f"语音转写失败：{e}", model
        return (r.get("text") or "").strip(), None, r.get("model", model)

    try:
        # 2) 指纹优先：同段录音是这里最强也最便宜的证据，所以无条件先跑，命中即定论。
        #
        #    旧流程是「先判意图（说话/哼唱）再二选一」，而「把原曲外放给麦克风听」
        #    的声学特征恰好卡在意图阈值上（实测 hum_score 0.44~0.49，阈值 0.46，
        #    连库里原曲的干净截取都会判成 speech）。一旦判成说话就走 ASR，而原曲
        #    有歌词、ASR 转得出文字，于是永远轮不到指纹 —— 指纹等于没参与。
        #
        #    现在改成：只要用户没明确指定走哪条路（force 为空），就先按最严阈值
        #    查指纹。命中说明「这段录音就是库里某个文件的同一段」，直接返回，
        #    连 ASR 都不用跑（又快又准，还省一次转写）。
        # 诊断开关：body.keep = 服务器本地路径时，把这段录音另存一份，
        # 方便事后分析「真机到底录成了什么样」。默认不留盘（隐私）。
        _keep = (body.get("keep") or "").strip()
        if _keep:
            try:
                import shutil
                shutil.copyfile(tmp, _keep)
                op_log("语音检索", f"录音已留档 → {_keep}")
            except Exception as e:
                op_log("语音检索", f"留档失败：{e}")

        force = (body.get("force") or "").strip()
        if force == "":
            pre, diag = fp.search_robust(tmp, top_k=top_k, kinds=("audio", "video"))
            _b = diag.get("best")
            _ds = (f"{_b[0]} 票{_b[1]}/{diag['windows']} 均分{_b[2]:.3f} "
                   f"对齐{_b[4]} 抖动{_b[6]}") if _b else "无候选"
            if pre:
                # 文件已不在的直接丢（指纹库残留，见 audio_search 里同样的处理）
                pre = [r for r in pre if os.path.exists(r["path"])]
                for r in pre:
                    r["name"] = os.path.basename(r["path"])
                    r["exists"] = True
            if pre:
                linked = _linked_rows(pre)
                op_log("语音检索",
                       f"指纹精确命中（{_ds}）" + (f" + {len(linked)} 个同一首歌" if linked else ""))
                return {"ok": True, "intent": "fp", "count": len(pre) + len(linked),
                        "results": pre + linked, "threshold": 0.30, "certain": True,
                        "suggest": ["text"], "diag": diag,
                        "linked_count": len(linked),
                        "confidence": round(float(pre[0]["score"]), 3)}
            # 没命中也要把「差多远」记下来，否则真机排查只能靠猜
            op_log("语音检索", f"指纹未达线（{_ds} 票{diag.get('votes')}）")

        # 3) 意图判断（纯声学特征，见 voice_intent.py）
        vi_res = vi.analyze(tmp)
        if not vi_res.get("ok"):
            return {"ok": False, "message": vi_res.get("message", "无法识别这段录音"),
                    "features": vi_res.get("features")}

        intent = {"text": "speech", "fp": "hum"}.get(force) or vi_res["intent"]

        # 3a) 哼唱 → 指纹匹配，只在音频与视频里找（kinds 在 SQL 层就收窄）
        # 自动判成哼唱时用更严的阈值：实测真人哼的旋律会跟无关音视频擦出
        # 0.63~0.74 的假阳性（纯音调更甚），而「同一段录音」稳定在 0.86~0.99。
        # 卡在 0.82 以上既挡掉假阳性，又保住外放原音频给麦克风听的场景。
        # 用户手动按「改用指纹搜」(force='fp') 时不收紧，尊重他的选择。
        hum_thr = threshold if force == "fp" else min(threshold, 0.18)
        if intent == "hum":
            out = []
            for r in fp.search(tmp, threshold=hum_thr, top_k=top_k,
                               kinds=("audio", "video")):
                if not os.path.exists(r["path"]):
                    continue            # 指纹库残留的失效路径，直接丢
                r["name"] = os.path.basename(r["path"])
                r["exists"] = True
                out.append(r)
            if not out:
                # 这段录音本身提取不出有区分度的指纹（太短 / 太平稳）时，
                # 先说清楚原因，别再让用户去点「改用指纹搜」空转。
                _q, _qwhy = fp.query_quality(tmp)
                # 指纹 0 命中 → 顺手试一把 ASR，把文字交回前端自动改走文字检索。
                # 用户明确按了「改用指纹搜」(force='fp') 时不兜底，尊重选择。
                alt_text, alt_err, _ = ("", None, None) if force == "fp" else _try_asr()
                alt_filler = bool(alt_text) and not _voice_text_ok(alt_text)
                if alt_filler:
                    op_log("哼唱检索", f"转写只有语气词（「{alt_text[:8]}」），不拿去搜")
                    alt_text, alt_err = "", None
                if alt_text:
                    op_log("哼唱检索", f"指纹 0 命中 → 转文字兜底「{alt_text[:24]}」")
                    return {"ok": True, "intent": "hum", "count": 0, "results": [],
                            "threshold": hum_thr, "fallback": "asr",
                            "alt_text": alt_text, "suggest": ["fp"],
                            "hum_score": vi_res["hum_score"],
                            "confidence": vi_res["confidence"],
                            "features": vi_res.get("features")}
                op_log("哼唱检索", f"0 个匹配 · 哼唱得分 {vi_res['hum_score']}")
                return {"ok": True, "intent": "hum", "count": 0, "results": [],
                        "threshold": hum_thr,
                        # 收紧阈值挡掉了假阳性，所以给一个「用宽松阈值再试一次指纹」的出口
                        "suggest": [] if (force == "fp" or _qwhy) else ["fp"],
                        "need_asr": (alt_err == "need_asr") or None,
                        "message": _qwhy or (HUM_NO_LYRIC_MSG if alt_filler else HUM_MISS_MSG),
                        "hum_score": vi_res["hum_score"],
                        "confidence": vi_res["confidence"],
                        "features": vi_res.get("features")}
            op_log("哼唱检索", f"{len(out)} 个匹配 · 哼唱得分 {vi_res['hum_score']}")
            return {"ok": True, "intent": "hum", "count": len(out),
                    "results": out, "threshold": hum_thr, "suggest": ["text"],
                    "hum_score": vi_res["hum_score"],
                    "confidence": vi_res["confidence"],
                    "features": vi_res.get("features")}

        # 3b) 说话 → ASR 转文字，交回前端走语义检索
        text, err, model_used = _try_asr()
        if err == "need_asr":
            return {"ok": True, "intent": "speech", "need_asr": True,
                    "hum_score": vi_res["hum_score"],
                    "message": "语音转文字还没启用：请到「设置 → 音频转写」打开开关并填好地址与模型，"
                               "之后对着麦克风说话就能直接搜素材。"}
        if err:
            return {"ok": False, "intent": "speech", "message": err}
        text = (text or "").strip()
        filler = bool(text) and not _voice_text_ok(text)
        if filler:
            op_log("语音检索", f"转写只有语气词（「{text[:8]}」），按没听清处理")
            text = ""
        if not text:
            # 兜底：可能其实是哼唱被误判成了说话 → 再试一次指纹匹配。
            # 转不出文字时试一把不花额外代价，能救回误判的情况（唱歌时最常见）。
            # 但用户明确按了「改用文字搜」(force='text') 时不兜底，否则按钮点下去
            # 又被指纹结果顶掉，看起来像没反应。
            out = []
            if force != "text":
                for x in fp.search(tmp, threshold=threshold, top_k=top_k,
                                   kinds=("audio", "video")):
                    if x.get("kind") not in ("audio", "video"):
                        continue
                    x["name"] = os.path.basename(x["path"])
                    x["exists"] = os.path.exists(x["path"])
                    out.append(x)
            if out:
                op_log("语音检索", f"转写为空，指纹兜底命中 {len(out)} 个")
                return {"ok": True, "intent": "hum", "count": len(out),
                        "results": out, "threshold": threshold,
                        "fallback": True, "suggest": ["text"],
                        "hum_score": vi_res["hum_score"],
                        "confidence": vi_res["confidence"],
                        "features": vi_res.get("features")}
            op_log("语音检索", "两条路都没结果")
            return {"ok": True, "intent": "speech", "count": 0, "results": [],
                    "threshold": hum_thr, "tried": "fp+asr",
                    # 走文字路时指纹用了宽松阈值，没必要再试；反过来则还有余地
                    "suggest": ["fp"] if force == "text" else [],
                    "message": HUM_NO_LYRIC_MSG if filler else BOTH_MISS_MSG,
                    "hum_score": vi_res["hum_score"],
                    "confidence": vi_res["confidence"],
                    "features": vi_res.get("features")}
        op_log("语音检索", f"「{text[:30]}」")
        return {"ok": True, "intent": "speech", "text": text,
                "model": model_used,
                "hum_score": vi_res["hum_score"],
                "confidence": vi_res["confidence"]}
    finally:
        if auto_tmp and tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def ai_test(body: dict) -> dict:
    """测试 AI 服务连通性（可传未保存的临时配置）。"""
    import ai_desc
    st = load_settings()
    base = (body.get("base_url") or st.get("ai_base_url") or "").strip()
    key = body.get("api_key") if body.get("api_key") is not None else st.get("ai_api_key", "")
    model = (body.get("model") or st.get("ai_model") or "").strip()
    if not base:
        return {"ok": False, "message": "请先填写 API 地址"}
    r = ai_desc.test_connection(base, key or "", model)
    return r


def ai_models(body: dict) -> dict:
    """列出 AI 服务可用模型。kind=vision 只留能看图的，kind=asr 只留语音转写。"""
    import ai_desc
    st = load_settings()
    base = (body.get("base_url") or st.get("ai_base_url") or "").strip()
    key = body.get("api_key") if body.get("api_key") is not None else st.get("ai_api_key", "")
    kind = (body.get("kind") or "").strip()
    if not base:
        return {"ok": False, "models": [], "message": "请先填写 API 地址"}
    try:
        return {"ok": True, "models": ai_desc.list_models(base, key or "", kind=kind)}
    except ai_desc.AIError as e:
        return {"ok": False, "models": [], "message": str(e)}


def ai_describe(body: dict) -> dict:
    """给指定的图片文件生成描述与标签（用于预览效果 / 单张补标）。"""
    import ai_desc
    from PIL import Image
    path = body.get("path", "")
    if not path or not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    st = load_settings()
    base = (body.get("base_url") or st.get("ai_base_url") or "").strip()
    key = body.get("api_key") if body.get("api_key") is not None else st.get("ai_api_key", "")
    model = (body.get("model") or st.get("ai_model") or "").strip()
    if not (base and model):
        return {"ok": False, "message": "请先在设置中配置 API 地址与模型"}
    try:
        with Image.open(path) as im:
            r = ai_desc.describe_image(base, key or "", model, im.convert("RGB"),
                                       prompt=st.get("ai_prompt", ""),
                                       max_tags=int(st.get("ai_max_tags", 12) or 12))
        r["ok"] = True
        return r
    except ai_desc.AIError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


def ai_cfg_from_settings(st: dict = None) -> dict:
    """把设置里的 AI 相关键组装成 indexer 要的 ai_cfg。"""
    st = st or load_settings()
    return {
        "enabled": bool(st.get("ai_enabled")),
        "base_url": (st.get("ai_base_url") or "").strip(),
        "api_key": st.get("ai_api_key", "") or "",
        "model": (st.get("ai_model") or "").strip(),
        "prompt": st.get("ai_prompt", ""),
        "prompt_video": st.get("ai_prompt_video", ""),
        "max_tags": int(st.get("ai_max_tags", 12) or 12),
    }


def asr_cfg_from_settings(st: dict = None) -> dict:
    """把设置里的 ASR 相关键组装成 indexer 要的 asr_cfg。"""
    st = st or load_settings()
    return {
        "enabled": bool(st.get("asr_enabled")),
        "base_url": (st.get("asr_base_url") or "").strip(),
        "api_key": st.get("asr_api_key", "") or "",
        "model": (st.get("asr_model") or "").strip(),
        "language": (st.get("asr_language") or "").strip(),
        "max_duration": int(st.get("asr_max_duration", 0) or 0),
    }


def ai_tag_one(path: str, st: dict = None) -> dict:
    """给单个素材立即打标（详情页按钮 / 闲置自动打标共用）。"""
    if _STATE["busy"]:
        return {"ok": False, "message": "正在建索引，等它跑完再打标"}
    if not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    st = st or load_settings()
    if not st.get("ai_enabled"):
        return {"ok": False, "message": "AI 描述生成还没开启（设置 → 智能服务）"}
    try:
        model, processor = _ensure_model()
    except Exception:
        return {"ok": False, "message": "模型还没加载完，稍后再试"}
    _STATE["ai_tagging"] = path
    try:
        r = ix.tag_file(path, model, processor, ai_cfg_from_settings(st))
    finally:
        _STATE["ai_tagging"] = None
    if r.get("ok"):
        op_log("AI 打标", f"{os.path.basename(path)} · {len(r.get('tags') or [])} 个标签"
                          f" · {r.get('seconds')}s")
    return r


def asr_transcribe_one(path: str, st: dict = None) -> dict:
    """给单个音频/视频立即转写（详情页按钮 / 闲置自动处理共用）。"""
    if _STATE["busy"]:
        return {"ok": False, "message": "正在建索引，等它跑完再转写"}
    if not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    st = st or load_settings()
    if not st.get("asr_enabled"):
        return {"ok": False, "message": "音频转写还没开启（设置 → 智能服务）"}
    cfg = asr_cfg_from_settings(st)
    if not (cfg["base_url"] and cfg["model"]):
        return {"ok": False, "message": "请先在设置里填语音识别服务的地址与模型"}
    # 视频的转写只写 meta（关键词路），不需要 WeMM 模型；音频要重编码 chunk 0，才需要。
    model = processor = None
    if os.path.splitext(path)[1].lower() not in ix.VIDEO_EXT:
        try:
            model, processor = _ensure_model()
        except Exception:
            return {"ok": False, "message": "模型还没加载完，稍后再试"}
    _STATE["asr_running"] = path
    try:
        r = ix.transcribe_file(path, cfg, model, processor)
    finally:
        _STATE["asr_running"] = None
    if r.get("ok"):
        op_log("音频转写", f"{os.path.basename(path)} · {r.get('chars')} 字"
                            f" · {r.get('seconds')}s")
    return r


def asr_status() -> dict:
    """未转写清单 + 闲置处理运行状态 + 智能纠偏进度。"""
    s = _ix_asr_status()
    st = load_settings()
    return {
        "total": s["total"], "done": s["done"],
        "pending": len(s["pending"]), "items": s["pending"][:200],
        "running": bool(_STATE.get("asr_running")),
        "enabled": bool(st.get("asr_enabled")),
        "polish": polish_view(),
        "polish_progress": ix.polish_progress(),
        "ai_on": bool(st.get("ai_enabled")) and bool((st.get("ai_model") or "").strip()),
    }


# ------------------------------------------------------ 转写文字智能纠偏
# 用已配置的 LLM 把 ASR 生文字读顺。结果是**另存** asr_clean，asr_text 原文
# 一个字不动 —— 用户常常正是照着他听到的那个错字去搜，改掉原文等于把那条
# 检索路径砍了（详见 asr_polish.py 顶部）。检索两路都同时吃原文与优化版。
_POLISH = {"running": False, "stop": False, "done": 0, "failed": 0,
           "total": 0, "current": None, "started": None, "finished": None,
           "last_error": None,
           # 空闲门（polish_idle_only）用到：正在等用户闲下来 / 本轮到点还要等几秒
           "waiting": False, "need_idle": 0, "idle_sec": 0}


def _polish_idle_wait() -> bool:
    """批量纠偏的分文件闸门：确认「用户现在没在用」再继续。返回 False = 被中止。

    判据与闲置补全同一套：`_STATE["last_user_req"]`（只有用户真的在操作才刷新，
    设置页那种纯轮询端点被 `_QUIET_PATHS` 排除了）距今超过 idle_minutes 分钟。
    每 5 秒看一次，期间随时响应「停止」；门槛每轮重读设置，
    这样用户在等待期间把「空闲几分钟」改小能立刻生效，不用先停再开。
    """
    announced = False
    while True:
        if _POLISH["stop"]:
            return False
        st = load_settings()
        need = float(st.get("idle_minutes", 5) or 5) * 60
        _POLISH["need_idle"] = need
        idle_sec = time.time() - _STATE.get("last_user_req",
                                           _STATE.get("last_req", 0))
        _POLISH["idle_sec"] = idle_sec
        if idle_sec >= need:
            if announced:
                print("[智能纠偏] 用户闲下来了，继续")
            _POLISH["waiting"] = False
            return True
        if not announced:
            announced = True
            _POLISH["waiting"] = True
            print(f"[智能纠偏] 用户正在用，等空闲 {need / 60:.0f} 分钟再继续")
        time.sleep(5)


def _is_local_url(url: str) -> bool:
    """这个服务地址是不是跑在**本机**上？

    只有本机服务才会真占这台机器的内存/显存。填官方（远端）地址时模型在
    人家服务器上跑，本地只剩网络 I/O —— 那就不该跟别的任务互斥。

    认的东西：localhost / 127.0.0.1 / 127.x.x.x / ::1 / 0.0.0.0。
    局域网别的机器（192.168.x.x、*.local）不算 —— 它不占本机内存，
    虽然会占网络，但那不是「重活」要防的东西。
    """
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        u = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
        host = (u.hostname or "").lower().strip("[]")
    except Exception:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    return host.startswith("127.")


def _model_in_use() -> str:
    """谁正攥着**本进程的 WeMM 模型**（空串 = 没人用，可以放心卸载）。

    ★ 给 `_model_reaper` 用，判据和 `_heavy_running` **不一样**：这里问的是
    「这个任务会不会用到 WeMM」，跟模型服务在哪台机器上无关 ——
      * 打标签即使把视觉模型填成远端官方 API，最后仍要用 WeMM 把生成的描述和
        标签编成向量写进索引（`indexer.tag_file` 的 `model, processor` 参数）；
      * 转写只有**音频**要重编码 chunk 0，**视频只写 meta（关键词路），压根不碰
        WeMM**（见 `asr_transcribe_one` 里那个 `if ext not in VIDEO_EXT`）；
      * 建索引、智能纠偏、一键补全都得 encode。
    如果这里错判成「没人用」把模型卸了，正在跑的编码会拿着已被丢弃的引用继续
    跑完 —— 不崩，但那几 GB 根本不会释放，卸载等于白做。
    """
    if _STATE.get("busy"):
        return "建索引"
    if _STATE.get("ai_tagging"):
        return "打标签"
    p = _STATE.get("asr_running")
    if p and os.path.splitext(p)[1].lower() not in ix.VIDEO_EXT:
        return "转写"
    if _POLISH.get("running"):
        return "智能纠偏"
    man = _IDLE.get("manual")
    if man and not man.get("stop") and not man.get("finished"):
        return "一键补全"
    return ""


def _heavy_running(exclude: str = "") -> str:
    """谁在抢**本机**的资源（空串 = 都闲着）。用于重任务互斥。

    ★ 关键是判「模型跑在哪台机器上」，而不是「有没有任务在跑」：
      * 打标签 / 转写如果填的是**官方（远端）API**，模型在人家服务器上，本地
        只是网络等待，内存占用可以忽略 —— 那**不算重活**，不该把「智能纠偏」
        和「闲置补全」一起挡住干等。用户机器内存宽裕，没必要让两边互相等。
      * 填的是**本机地址**（127.0.0.1 / localhost，比如 oMLX / Ollama）时才要
        互斥：打标要驻留视觉模型、转写要驻留 ASR 模型、纠偏要驻留文本模型，
        三个一起上就是好几 G，16G 机器风扇直接起飞。
      * 建索引 / 智能纠偏 / 一键补全本身就要用本进程的 WeMM（2G 级）吃 CPU，
        不管模型在哪都算重活。

    exclude 用来忽略「自己」，避免自己把自己挡住。
    """
    if _STATE.get("busy"):
        return "建索引"
    st = load_settings()
    if _STATE.get("ai_tagging") and _is_local_url(st.get("ai_base_url")):
        return "打标签"
    if _STATE.get("asr_running") and _is_local_url(st.get("asr_base_url")):
        return "转写"
    if _POLISH.get("running") and exclude != "polish":
        return "智能纠偏"
    man = _IDLE.get("manual")
    if (man and not man.get("stop") and not man.get("finished")
            and exclude != "idle"):
        return "一键补全"
    return ""


def polish_view() -> dict:
    p = _POLISH
    return {
        "running": bool(p["running"]),
        "done": p["done"], "failed": p["failed"], "total": p["total"],
        "current": os.path.basename(p["current"]) if p["current"] else None,
        "started": p["started"], "finished": p["finished"],
        "last_error": p["last_error"],
        # 空闲门：前端据此显示「正在等你闲下来 · 还有 N 分 M 秒」
        "waiting": bool(p["waiting"]),
        "need_idle": p["need_idle"],
        "idle_sec": round(p["idle_sec"], 1),
    }


def asr_polish_one(path: str) -> dict:
    """给一个音频/视频做智能纠偏（播放面板按钮）。"""
    if not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    st = load_settings()
    if not (bool(st.get("ai_enabled")) and (st.get("ai_base_url") or "").strip()
            and (st.get("ai_model") or "").strip()):
        return {"ok": False, "message": "请先在「设置 → 智能服务」配置文本模型"}
    model = processor = None
    # 只有音频需要重编码 chunk 0；视频的转写只走关键词路，不必加载 WeMM。
    if os.path.splitext(path)[1].lower() not in ix.VIDEO_EXT:
        try:
            model, processor = _ensure_model()
        except Exception:
            return {"ok": False, "message": "模型还没加载完，稍后再试"}
    r = ix.polish_file(path, ai_cfg_from_settings(st), model, processor)
    if r.get("ok"):
        op_log("智能纠偏", f"{os.path.basename(path)} · {r.get('chars')} 字"
                          f" · {asr_polish.KIND_NAMES.get(r.get('kind'), '')}"
                          f" · {r.get('seconds')}s")
    return r


def asr_unpolish_one(path: str) -> dict:
    """丢掉优化版、回到只看原文。

    音频要重编码 chunk 0（只留原文）—— 当初 polish 是把「原文 + 优化版」一起拼进
    向量的，不重编码就会出现「点了还原，但搜优化版才有的词还能搜到这个文件」。
    视频的转写不走向量路，直接删元数据即可。
    """
    if not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    model = processor = None
    if os.path.splitext(path)[1].lower() not in ix.VIDEO_EXT:
        try:
            model, processor = _ensure_model()
        except Exception:
            model = processor = None   # 模型没就绪也让它删：元数据先干净，刷新索引会自愈
    r = ix.unpolish_file(path, model, processor)
    if r.get("ok"):
        op_log("智能纠偏", f"{os.path.basename(path)} · 还原原文")
    return r


def asr_polish_all(action: str = "status") -> dict:
    """一键优化全部：把「有转写、还没优化」的逐个过一遍。"""
    if action == "stop":
        if _POLISH["running"]:
            _POLISH["stop"] = True
            print(f"[智能纠偏] 用户中止（已优化 {_POLISH['done']} 个）")
        return {"ok": True, "polish": polish_view(), "progress": ix.polish_progress()}

    if action == "start":
        if _POLISH["running"]:
            return {"ok": False, "message": "已经有一轮在跑了"}
        # 和打标/转写/建索引互斥：三个一起跑会同时占住 oMLX 的两个模型 + 本进程的
        # WeMM，内存和风扇都受不了（见 _heavy_running 的注释）。
        busy = _heavy_running(exclude="polish")
        if busy:
            return {"ok": False, "message": f"正在{busy}，等它跑完再优化"}
        st = load_settings()
        if not (bool(st.get("ai_enabled")) and (st.get("ai_base_url") or "").strip()
                and (st.get("ai_model") or "").strip()):
            return {"ok": False, "message": "请先在「设置 → 智能服务」配置文本模型"}
        todo = ix.polish_pending()
        if not todo:
            return {"ok": False, "message": "没有待优化的转写文字（都是最新的）"}
        _POLISH.update({"running": True, "stop": False, "done": 0, "failed": 0,
                        "total": len(todo), "started": time.time(),
                        "finished": None, "last_error": None,
                        "waiting": False, "need_idle": 0, "idle_sec": 0})
        idle_only = bool(st.get("polish_idle_only", True))
        print(f"[智能纠偏] 开始（待优化 {len(todo)} 个"
              + ("，只在空闲时跑" if idle_only else "") + "）")
        op_log("智能纠偏", f"开始批量优化，待处理 {len(todo)} 个")

        def _worker():
            try:
                model = processor = None
                for p in todo:
                    if _POLISH["stop"]:
                        break
                    # 空闲门：用户在用就先挂着，别抢 GPU。放在**每个文件之前**，
                    # 所以用户中途回来用电脑，这一批会就地暂停、等人走了接着跑
                    # —— 进度是留在库里（asr_clean）的，暂停不丢东西。
                    if idle_only and not _polish_idle_wait():
                        break
                    _POLISH["current"] = p
                    try:
                        if os.path.splitext(p)[1].lower() not in ix.VIDEO_EXT:
                            if model is None:
                                model, processor = _ensure_model()
                        r = ix.polish_file(p, ai_cfg_from_settings(), model, processor)
                        if r.get("ok"):
                            _POLISH["done"] += 1
                        else:
                            _POLISH["failed"] += 1
                            _POLISH["last_error"] = r.get("message")
                    except Exception as e:      # noqa: BLE001
                        _POLISH["failed"] += 1
                        _POLISH["last_error"] = f"{type(e).__name__}: {e}"
            finally:
                _POLISH["current"] = None
                _POLISH["running"] = False
                _POLISH["finished"] = time.time()
                op_log("智能纠偏",
                       f"结束：成功 {_POLISH['done']} 个，失败 {_POLISH['failed']} 个")
                print(f"[智能纠偏] 结束（成功 {_POLISH['done']}，失败 {_POLISH['failed']}）")

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "polish": polish_view(), "progress": ix.polish_progress()}

    return {"ok": True, "polish": polish_view(), "progress": ix.polish_progress()}


def ai_backfill(action: str = "status", what: str = "tag"):
    """「立即补全」：不再等空闲，立刻把待打标（/待转写）的素材逐个处理掉。

    索引期本来就**不打标**（见 indexer.build_index 顶部注释），打标只有两条路：
    详情页的单文件按钮，和闲置守护线程。而闲置那条要连续空闲 idle_minutes
    分钟才动一个——用户开着页面搜几次就永远凑不齐，于是「加进来的图片一直
    没有标签，用文字搜画面搜不到」。这个接口就是那条「不等了」的路。

    实现上不新开线程：复用闲置循环，只把它的两个门槛（空闲时长、本轮上限）
    在 manual 模式下绕开。这样同一时刻仍然只有一个打标任务，不会两路并发
    抢 GPU，也不会出现两份进度。
    """
    man = _IDLE.get("manual")
    if action == "stop":
        if man and not man.get("finished"):
            man["stop"] = True
            man["finished"] = time.time()
            print(f"[立即补全] 用户中止（已补 {man.get('done', 0)} 个）")
        return {"ok": True, "backfill": _backfill_view()}

    if action == "start":
        st = load_settings()
        what = what if what in ("tag", "asr", "both") else "tag"
        # 和「智能纠偏」/建索引互斥：补全走的是 oMLX 的视觉或 ASR 模型，
        # 纠偏走的是文本模型 + 本进程的 WeMM，撞一起就是几个 G 的内存。
        busy = _heavy_running(exclude="idle")
        if busy:
            return {"ok": False, "message": f"正在{busy}，等它跑完再补全"}
        if man and not man.get("stop") and not man.get("finished"):
            return {"ok": False, "message": "已经有一轮补全在跑了"}
        if what in ("tag", "both") and not (bool(st.get("idle_tag")) and bool(st.get("ai_enabled"))):
            return {"ok": False, "message": "AI 打标没开：先去「智能服务」打开「AI 描述与标签」。"}
        if what in ("asr", "both") and not (bool(st.get("idle_asr")) and bool(st.get("asr_enabled"))):
            if what == "asr":
                return {"ok": False, "message": "音频转写没开：先去「智能服务」打开转写。"}
            what = "tag"          # 只补标签
        _IDLE["manual"] = {"what": what, "done": 0, "failed": 0,
                           "started": time.time(), "stop": False, "finished": None}
        left = len(ix.ai_tag_status()["pending"]) if what in ("tag", "both") else len(_asr_pending_list())
        print(f"[立即补全] 开始（{'标签' if what == 'tag' else '转写'}，待处理 {left} 个）")
        op_log("立即补全", f"开始补{'标签' if what == 'tag' else '转写'}，待处理 {left} 个")
        return {"ok": True, "pending": left, "backfill": _backfill_view()}

    return {"ok": True, "backfill": _backfill_view()}


def _backfill_view():
    """把 _IDLE["manual"] 收拾成前端好用的形状。"""
    man = _IDLE.get("manual")
    if not man:
        return {"active": False}
    return {
        "active": bool(not man.get("finished")),
        "what": man.get("what"),
        "done": man.get("done", 0),
        "failed": man.get("failed", 0),
        "started": man.get("started"),
        "finished": man.get("finished"),
    }


def ai_tag_status() -> dict:
    """未打标清单 + 闲置处理运行状态。"""
    s = ix.ai_tag_status()
    st = load_settings()
    idle_for = time.time() - _STATE.get("last_user_req",
                                      _STATE.get("last_req", time.time()))
    return {
        "total": s["total"], "tagged": s["tagged"],
        "pending": len(s["pending"]), "items": s["pending"][:200],
        "idle_tag": bool(st.get("idle_tag")),
        "idle_asr": bool(st.get("idle_asr")),
        "idle_minutes": st.get("idle_minutes", 5),
        "idle_batch": st.get("idle_batch", 10),
        "idle_for": round(idle_for, 1),
        "running": bool(_STATE.get("ai_tagging") or _STATE.get("asr_running")),
        "run_tagged": _IDLE.get("run_tagged", 0),
        "run_asr": _IDLE.get("run_asr", 0),
        "run_fail": _IDLE.get("run_fail", 0),
        "skip_count": _cooled_count(),
        "failed_total": len(_IDLE.get("fail", {})),
        "last_path": _IDLE.get("last_path"),
        "last_kind": _IDLE.get("last_kind"),
        "last_ts": _IDLE.get("last_ts"),
        "last_error": _IDLE.get("last_error"),
        "backfill": _backfill_view(),
        "asr_pending": len(_asr_pending_list()),
    }


# ---------------------------------------------------------------- MCP 服务
# 把检索能力开放给本机 agent。FXseek 这边只负责「往各 agent 的配置文件里
# 写一条 stdio 接入条目」，检索本身仍然由本服务提供（agent 拉起 mcp_server.py
# 子进程，子进程再回连 127.0.0.1:8231）。所以主服务不需要新开端口。
try:
    import mcp_agents as _mcp
except Exception as _e:  # noqa: BLE001
    _mcp = None
    _MCP_IMPORT_ERR = str(_e)
else:
    _MCP_IMPORT_ERR = None


def mcp_state() -> dict:
    """当前 MCP 接入状态：每个 agent 是否装、是否已接入。"""
    st = load_settings()
    if _mcp is None:
        return {"available": False, "error": _MCP_IMPORT_ERR,
                "enabled": False, "agents": [],
                "connected": 0, "installed": 0}
    rows = _mcp.detect_all()
    enabled_ids = set(st.get("mcp_agents") or [])
    for r in rows:
        # 配置里真的写进去了才算「已接入」；设置里勾了但文件被用户改回去，
        # 这里会如实显示成未接入 —— 以文件为准，不以我们的记忆为准。
        r["enabled_in_settings"] = r["id"] in enabled_ids
    return {
        "available": True,
        "error": None,
        "enabled": bool(st.get("mcp_enabled")),
        "agents": rows,
        "connected": sum(1 for r in rows if r.get("connected")),
        "installed": sum(1 for r in rows if r.get("installed")),
        "skilled": sum(1 for r in rows if r.get("skill")),
        "skill_name": _mcp.SKILL_NAME,
        "server": {"name": _mcp.SERVER_KEY,
                   "python": _mcp.PYTHON,
                   "script": _mcp.SERVER_PY},
    }


def mcp_skill(ids=None, force=False) -> dict:
    """把配套技能补进各 agent 的 skills 目录。

    开启 MCP 时 enable() 已经顺带做了，这个函数是给「我自己把技能删了」
    这种情况准备的手动入口，也是设置页那个「重新注入技能」按钮的实现。
    默认**只补缺失的**，不碰已存在的（用户可能改过）；force=True 才覆盖。
    """
    if _mcp is None:
        return {"ok": False, "error": "MCP 模块不可用：%s" % _MCP_IMPORT_ERR,
                "results": []}
    rows = _mcp.detect_all()
    pick = ids if ids else [r["id"] for r in rows if r.get("installed")]
    results = _mcp.inject_skills(pick, force=bool(force))
    created = [r["id"] for r in results if r.get("action") == "created"]
    if created:
        op_log("MCP 技能注入", "新写入 " + "、".join(created))
    return {"ok": all(r.get("ok") for r in results), "results": results,
            "created": len(created),
            "kept": sum(1 for r in results if r.get("action") in ("kept", "same"))}


def mcp_set(enabled: bool, ids=None) -> dict:
    """开/关 MCP 接入。

    enabled=True  → 给选中的 agent 写配置（默认全部已安装的）
    enabled=False → 把之前写进去的条目摘掉
    每一步都返回逐 agent 的成功/失败原因，前端如实展示，不吞错误。
    """
    st = load_settings()
    if _mcp is None:
        return {"ok": False, "error": "MCP 模块不可用：%s" % _MCP_IMPORT_ERR,
                "results": []}
    rows = _mcp.detect_all()
    pick = ids if ids else [r["id"] for r in rows if r.get("installed")]
    results = _mcp.enable(pick) if enabled else _mcp.disable(pick)
    ok_ids = [r["id"] for r in results if r.get("ok")]
    prev = set(st.get("mcp_agents") or [])
    if enabled:
        st["mcp_agents"] = sorted(prev | set(ok_ids))
    else:
        st["mcp_agents"] = sorted(prev - set(ok_ids))
    st["mcp_enabled"] = bool(enabled) and bool(st["mcp_agents"])
    save_settings(st)
    failed = [r for r in results if not r.get("ok")]
    if failed:
        op_log("MCP 接入", "部分失败：" + "；".join(
            "%s(%s)" % (r["id"], r.get("error") or "未知") for r in failed),
            level="warn")
    else:
        op_log("MCP 接入", ("已接入 " if enabled else "已断开 ") +
                            "、".join(ok_ids) if ok_ids else "无变化")
    out = mcp_state()
    out["ok"] = not failed
    out["results"] = results
    # 断开时如实说明技能没被删 —— 免得用户以为关掉就顺便清理了；
    # 接入时汇报这次是新建还是沿用，用户能看出「我删了它又补回来了」。
    if enabled:
        out["skill_created"] = [r["id"] for r in results
                                if r.get("skill") == "created"]
    else:
        out["skill_kept"] = sum(1 for r in results if r.get("skill_kept"))
    return out


def search_by_image(image, top_k=20, kind=None, db_path=None):
    """以图搜图：把上传图片编码成向量，在索引里找最相似的素材。

    复用 WeMM 的图像编码能力，无需额外模型。
    返回 [(score, path, kind, chunk_idx, meta)]
    """
    import mlx.core as mx
    import embed as we
    import fastsearch as fs

    model, processor = _ensure_model()
    v = we.embed(model, processor, image=image, instruction=we.QUERY_INSTRUCTION)
    mx.eval(v)
    qvec = v.tolist()

    db = db_path or ix.DB_PATH
    # with_chunk=True：回填「最匹配帧」，用于视频封面与起播定位
    res = fs.search_vector(qvec, db, kind=kind, limit=max(top_k * 4, 40),
                           with_chunk=True)

    # 只保留已添加索引源下的文件
    roots = []
    for s in load_sources():
        try:
            roots.append(os.path.realpath(s["path"]).rstrip("/"))
        except Exception:
            roots.append(s["path"].rstrip("/"))

    def under(p):
        if not roots:
            return False
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        return any(rp == r or rp.startswith(r + "/") for r in roots)

    out = []
    seen_paths = set()
    for score, path, k, idx, meta in res:
        if not under(path):
            continue
        if path in seen_paths:      # 文件级去重
            continue
        seen_paths.add(path)
        m = dict(meta or {})
        # 补充媒体元数据
        try:
            extra = ix.probe_media_meta(path)
            for kk, vv in extra.items():
                m.setdefault(kk, vv)
        except Exception:
            pass
        matched_ts = m.get("timestamp")
        out.append({"score": round(float(score), 4), "path": path, "kind": k,
                    "chunk_idx": idx, "meta": m,
                    "name": os.path.basename(path),
                    "size": m.get("size", 0),
                    "matched_timestamp": matched_ts,      # 最匹配帧的时间点（秒）
                    "exists": os.path.exists(path)})
        if len(out) >= top_k:
            break
    op_log("以图搜图", f"{len(out)} 项相似结果")
    return out


def file_detail(path: str) -> dict:
    """单个文件的完整详情（供右侧详情栏使用）。"""
    if not path or not _path_allowed(path) or not os.path.exists(path):
        raise ValueError("文件不存在或不可访问")
    ext = os.path.splitext(path)[1].lower()
    kind = _kind_of(path)
    meta = ix.probe_media_meta(path)
    try:
        st = os.stat(path)
        ctime, mtime, atime = st.st_ctime, st.st_mtime, st.st_atime
    except OSError:
        ctime = mtime = atime = 0
    indexed, chunks = False, 0
    index_meta = {}
    try:
        con = sqlite3.connect(ix.DB_PATH)
        row = con.execute("SELECT COUNT(*) FROM items WHERE path=?", (path,)).fetchone()
        chunks = row[0] if row else 0
        indexed = chunks > 0
        # 读取索引里存的 AI 标签 / ASR 转写文本
        mrow = con.execute("SELECT meta FROM items WHERE path=? LIMIT 1", (path,)).fetchone()
        if mrow and mrow[0]:
            try:
                index_meta = json.loads(mrow[0]) or {}
            except Exception:
                index_meta = {}
        con.close()
    except Exception:
        pass
    # 合并索引元数据（asr_text / ai_tags 等）
    for kk, vv in index_meta.items():
        if kk not in meta:
            meta[kk] = vv
    return {
        "path": path,
        "name": os.path.basename(path),
        "dir": os.path.dirname(path),
        "ext": ext.lstrip(".").upper(),
        "kind": kind,
        "kind_name": {"image": "图片", "video": "视频", "document": "文档",
                      "audio": "音频"}.get(kind, "其他"),
        "meta": meta,
        "indexed": indexed,
        "chunks": chunks,
        "times": {"created": ctime, "modified": mtime, "accessed": atime},
    }


def scoped_stats(source=None) -> dict:
    """索引统计（只统计已添加索引源下的文件，与浏览/搜索口径一致）。

    source: 可选，限定到某个索引源目录。
    """
    roots = []
    for s in load_sources():
        try:
            roots.append(os.path.realpath(s["path"]).rstrip("/"))
        except Exception:
            roots.append(s["path"].rstrip("/"))
    scope_root = None
    if source:
        try:
            scope_root = os.path.realpath(source).rstrip("/")
        except Exception:
            scope_root = source.rstrip("/")
    con = sqlite3.connect(ix.DB_PATH)
    try:
        rows = con.execute("SELECT path,kind,chunk_idx FROM items").fetchall()
    except sqlite3.OperationalError:
        rows = []          # 索引库尚未建立
    finally:
        con.close()
    if not roots:
        return {"vectors": 0, "files": 0, "by_kind": {},
                "vectors_by_kind": {}, "ai_tagged": 0}
    files, vectors = set(), 0
    kind_files = {}          # kind -> set(path)
    kind_vectors = {}        # kind -> 向量数
    ai_tagged = set()
    hit_roots = set()        # 索引里有东西的来源根
    for p, k, ci in rows:
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        if scope_root:
            if not (rp == scope_root or rp.startswith(scope_root + "/")):
                continue
        else:
            ok = False
            for r in roots:
                if rp == r or rp.startswith(r + "/"):
                    hit_roots.add(r); ok = True; break
            if not ok:
                continue
        files.add(p); vectors += 1
        kind_files.setdefault(k, set()).add(p)
        kind_vectors[k] = kind_vectors.get(k, 0) + 1
        if (ci or 0) >= 9000:
            ai_tagged.add(p)          # 该文件带 AI 标签/描述向量

    # 一个文件都没索引过的来源：把目录里能看见的素材也计进来。
    # 否则浏览视图里有 23 张卡片、侧栏却写 22，用户会以为丢了一个文件。
    # 注意只补「文件数/分类数」——vectors 只属于索引，不该被兜底撑大。
    for r in roots:
        if r in hit_roots:
            continue
        if scope_root and r != scope_root:
            continue
        for it in fs_browse(r):
            if it["path"] in files:
                continue
            files.add(it["path"])
            kind_files.setdefault(it["kind"], set()).add(it["path"])

    by_kind = {k: len(v) for k, v in kind_files.items()}
    return {"vectors": vectors, "files": len(files),
            "by_kind": by_kind,
            "vectors_by_kind": kind_vectors,
            "ai_tagged": len(ai_tagged)}


# ---------------- 未索引来源的「看得见」兜底 ----------------
# 用户新增一个文件夹、选了「稍后再索引」，以前的结果是：浏览视图里空空如也，
# 连缩略图都没有，再点一下那个来源又弹一次「要不要现在索引」——等于逼用户立刻索引。
# 其实「没索引」只该意味着「搜不到」，不该意味着「看不见」。
# 所以这里提供一个纯文件系统的兜底列表：只列出目录里有哪些素材，不写任何索引数据。
FS_BROWSE_MAX = 2000          # 单次兜底最多列这么多文件，避免超大目录把页面拖死
FS_BROWSE_TTL = 20.0          # 扫描结果缓存秒数，连续浏览不重复走目录
_FS_BROWSE_CACHE = {}         # {root: (ts, items)}


def fs_browse(root, limit=FS_BROWSE_MAX):
    """不查索引，直接列目录下的素材文件（返回与 browse_all 同形状的条目）。

    只做「看得见」：条目带 `unindexed: True`，前端据此显示「未索引」角标与提示条。
    不写 items 表、不生成向量——用户真去点「立即建立索引」才走索引流程。
    """
    try:
        root = os.path.realpath(root).rstrip("/")
    except Exception:
        root = root.rstrip("/")
    if not os.path.isdir(root):
        return []
    now = time.time()
    hit = _FS_BROWSE_CACHE.get(root)
    if hit and (now - hit[0]) < FS_BROWSE_TTL:
        return hit[1][:limit]

    items = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("._"):
                    continue
                p = os.path.join(dirpath, fn)
                k = _kind_of(p)
                if k not in ("image", "video", "audio", "document"):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items.append({"path": p, "kind": k, "name": fn, "score": 0.0,
                              "size": st.st_size, "mtime": st.st_mtime,
                              "chunks": 0, "meta": {"size": st.st_size},
                              "exists": True, "unindexed": True})
                if len(items) >= FS_BROWSE_MAX:
                    break
            if len(items) >= FS_BROWSE_MAX:
                break
    except Exception:
        pass
    _FS_BROWSE_CACHE[root] = (now, items)
    # 缓存只留最近若干个根，别无限涨
    if len(_FS_BROWSE_CACHE) > 16:
        for k in sorted(_FS_BROWSE_CACHE, key=lambda x: _FS_BROWSE_CACHE[x][0])[:-16]:
            _FS_BROWSE_CACHE.pop(k, None)
    return items[:limit]


def _fs_merge(seen, roots, scope_root, kind):
    """把「一个文件都没索引过」的来源目录，用文件系统兜底补进 seen。"""
    indexed_roots = set()
    for p in seen:
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        for r in roots:
            if rp == r or rp.startswith(r + "/"):
                indexed_roots.add(r)
                break
    scan_roots = [r for r in roots if r not in indexed_roots]
    if scope_root:
        scan_roots = [r for r in scan_roots if r == scope_root]
    added = 0
    for r in scan_roots:
        for it in fs_browse(r):
            if kind and it["kind"] != kind:
                continue
            if it["path"] in seen:
                continue
            seen[it["path"]] = it
            added += 1
    return added


FIND_MAX_VISIT = 40000        # 按名字找时最多走访这么多文件（含未索引的来源目录）
FIND_MAX_HITS = 300           # 命中上限，防止搜「e」把整个库倒出来

# ---- 分词兜底 ------------------------------------------------------------
# 用户记文件名常常只记个大意，中间还夹着别的字：
#   「季度报告」  ↔  季度经营分析报告.docx
# 整串匹配必然落空，可这明明就是他要找的文件。所以整串没命中时再做一次
# **松一点**的匹配，但松到哪一步要卡住，否则搜「报」能把半个库倒出来。
_FIND_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_FIND_LATIN_RE = re.compile(r"[0-9a-zA-Z]+")
FIND_COVER_FLOOR = 0.6        # 查询里的「字」至少六成要在文件名里出现
FIND_FUZZY_MIN_UNITS = 2      # 至少两个单位才敢兜底——单字查询太危险，一律不兜


def _find_fuzzy(name_l, q):
    """整串没命中时的分词兜底。返回 (rank, ratio) 或 None。

    中文按**字集合覆盖率**看（不看顺序，容忍中间夹字），
    拉丁按**整词**看（report 和 2024 分开算，认出一个算一个）。

    命中的给 rank 3/4（排在「名字里含它」之后、「只在路径里含它」之前），
    并带 ratio 供同档排序。
    """
    cjk_q = set(_FIND_CJK_RE.findall(q))
    latin_q = _FIND_LATIN_RE.findall(q.lower())
    units = len(cjk_q) + len(latin_q)
    if units < FIND_FUZZY_MIN_UNITS:
        return None                     # 「女」「a」这种单字查询不兜底
    hit = len(cjk_q & set(_FIND_CJK_RE.findall(name_l)))
    hit += sum(1 for w in latin_q if w in name_l)
    ratio = hit / float(units)
    if ratio < FIND_COVER_FLOOR:
        return None
    return (3 if ratio >= 1.0 else 4), round(ratio, 3)


def find_by_name(name, kind=None, limit=30, source=None):
    """按文件名/路径找素材 —— 和 do_search 的语义检索**完全分开**。

    为什么单独一条路：文件名可能是随手起的、甚至写错的，所以它不该参与
    「按内容找」的相似度排序（那会污染排序，让「最终版.png」压过真正相关的东西）。
    但用户明确报出一个文件名时，「按名字找」就是他要的。两条路分开，互不污染。

    会在**索引表**里找，也会去**未索引的来源目录**里扫一遍 —— 用户没建索引的
    文件夹，按名字照样该找得到（返回里带 `unindexed: True` 标记）。
    """
    q = (name or "").strip().lower()
    if not q:
        return {"name": name, "count": 0, "results": [], "hint": "请给出要查找的文件名"}

    srcs = load_sources()
    if not srcs:
        return {"name": name, "count": 0, "results": [], "no_source": True}

    roots = []
    for s in srcs:
        try:
            roots.append((os.path.realpath(s["path"]).rstrip("/"), s.get("name") or ""))
        except Exception:
            pass
    scope = None
    if source:
        try:
            scope = os.path.realpath(source).rstrip("/")
        except Exception:
            scope = None
    if scope:
        roots = [r for r in roots if r[0] == scope or r[0].startswith(scope + "/")
                 or scope.startswith(r[0] + "/")]

    hits = {}       # realpath -> entry

    def _rank(basename_l, full_l):
        """越小越靠前。返回 (rank, ratio)。

        0 精确同名 > 1 名字以它开头 > 2 名字里含它
        > 3/4 分词兜底（ratio 高者靠前）> 5 只在路径里含它 > 9 没命中
        """
        if basename_l == q:
            return 0, 1.0
        if basename_l.startswith(q):
            return 1, 1.0
        if q in basename_l:
            return 2, 1.0
        fz = _find_fuzzy(basename_l, q)
        if fz:
            return fz
        if len(q) >= 2 and q in full_l:   # 单字不撞 path——~/… 里全是 a
            return 5, 1.0
        return 9, 0.0

    # ---------- 1) 索引表（已索引的文件，能立刻给出 kind 与是否存在） ----------
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            for p, k in con.execute("SELECT DISTINCT path, kind FROM items"):
                if not p:
                    continue
                bl, fl = os.path.basename(p).lower(), p.lower()
                r, rat = _rank(bl, fl)
                if r > 5:            # 只路径里含它 → 也收，但排最后
                    continue
                if kind and k != kind:
                    continue
                try:
                    rp = os.path.realpath(p)
                except Exception:
                    rp = p
                if scope and not (rp == scope or rp.startswith(scope + "/")):
                    continue
                hits[rp] = {"path": p, "name": os.path.basename(p), "kind": k,
                            "rank": r, "ratio": rat, "approx": 3 <= r <= 4,
                            "unindexed": False}
        finally:
            con.close()
    except Exception as e:
        op_log("按名字查找", "读索引表失败：%s" % e, level="warn")

    # ---------- 2) 文件系统（未索引的来源目录也要找得到） ----------
    visited = 0
    truncated = False
    for root, _sname in roots:
        if scope and not (root == scope or root.startswith(scope + "/")
                          or scope.startswith(root + "/")):
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("._"):
                    continue
                visited += 1
                if visited > FIND_MAX_VISIT:
                    truncated = True
                    break
                p = os.path.join(dirpath, fn)
                r, rat = _rank(fn.lower(), p.lower())
                if r > 5:
                    continue
                try:
                    rp = os.path.realpath(p)
                except Exception:
                    rp = p
                if rp in hits:
                    continue
                if scope and not (rp == scope or rp.startswith(scope + "/")):
                    continue
                try:
                    k = _kind_of(p)
                except Exception:
                    k = "other"
                if k == "other":
                    continue
                if kind and k != kind:
                    continue
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                hits[rp] = {"path": p, "name": fn, "kind": k, "rank": r,
                            "ratio": rat, "approx": 3 <= r <= 4,
                            "unindexed": True, "size": sz}
            if truncated:
                break
        if truncated:
            break

    # 同档内：先按 rank，再按覆盖率（兜底命中要排得靠谱），最后按名字
    out = sorted(hits.values(),
                 key=lambda x: (x["rank"], -x.get("ratio", 1.0), x["name"].lower()))
    total = len(out)
    out = out[:max(1, min(FIND_MAX_HITS, int(limit)))]
    res = {"name": name, "count": len(out), "total": total,
           "results": out, "visited": visited, "truncated": truncated}
    if any(x.get("approx") for x in out):
        res["approx_note"] = ("带 \"approx\": true 的是**分词兜底**命中的——文件名里没有"
                              "你要的整串，但查询里的字/词基本都在其中，多半就是它。"
                              "报给用户时把完整文件名念出来让他确认。")
    return res


def browse_all(kind=None, limit=300, sort="recent", source=None):
    """列出已索引的资产（不搜索，用于浏览视图）。

    ★ 只返回「已添加索引源」目录下的文件；未添加任何索引源时返回空列表，
      前端展示引导页（不展示任何默认/残留内容）。
    source: 可选，限定到某个索引源目录。
    """
    srcs = load_sources()
    if not srcs:
        return {"count": 0, "items": [], "no_source": True}

    # 索引源根目录（realpath 规范化）
    roots = []
    for s in srcs:
        try:
            roots.append(os.path.realpath(s["path"]).rstrip("/"))
        except Exception:
            roots.append(s["path"].rstrip("/"))

    con = sqlite3.connect(ix.DB_PATH)
    sql = "SELECT path,kind,chunk_idx,meta,mtime FROM items"
    params = []
    if kind:
        sql += " WHERE kind=?"
        params.append(kind)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    scope_root = None
    if source:
        try:
            scope_root = os.path.realpath(source).rstrip("/")
        except Exception:
            scope_root = source.rstrip("/")

    def under_sources(p: str) -> bool:
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        if scope_root:
            return rp == scope_root or rp.startswith(scope_root + "/")
        return any(rp == r or rp.startswith(r + "/") for r in roots)

    # ★ 先并行预热元数据缓存：没命中缓存的文件才真的跑 ffprobe/PIL。
    #   串行 272 个文件实测 ~19s（用户看到的「加载素材…」几乎全在这儿），
    #   8 线程降到 2~3s，且第二趟开始全部命中缓存（毫秒级）。
    #   缓存实现见 indexer.py 顶部 _PROBE_MEM 注释。
    try:
        ix.warm_meta_cache(list({r[0] for r in rows}), workers=16)
    except Exception:
        pass

    # 按文件聚合：同一文件的多帧/多块只显示一次
    seen = {}
    for path, k, idx, meta, mtime in rows:
        if not under_sources(path):
            continue
        if path not in seen:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            m = {}
            try:
                m = ix.probe_media_meta(path)
            except Exception:
                m = {"size": size}
            seen[path] = {"path": path, "kind": k, "name": os.path.basename(path),
                          "score": 0.0, "size": size, "mtime": mtime or 0,
                          "chunks": 0, "meta": m,
                          "exists": os.path.exists(path)}
        seen[path]["chunks"] += 1

    # ★ 一个文件都没索引过的来源：用文件系统兜底补进来。
    #   否则「稍后再索引」就等于「什么都看不见」，用户被逼着必须马上索引。
    unidx = _fs_merge(seen, roots, scope_root, kind)

    items = list(seen.values())
    if sort == "name":
        items.sort(key=lambda x: x["name"])
    elif sort == "size":
        items.sort(key=lambda x: -x["size"])
    elif sort == "kind":
        items.sort(key=lambda x: (x["kind"], -x["mtime"]))
    elif sort == "none":
        pass                      # 保持原始顺序（由前端排序）
    else:  # recent
        items.sort(key=lambda x: -x["mtime"])
    return {"count": len(items), "items": items[:limit], "unindexed": unidx}


def _ensure_model():
    """拿到 WeMM，没有就现场加载（按需加载）。

    启动时不再无条件把 3.5 GB 权重拉进内存 —— 8 GB 的 M1 上那会直接拖慢整机。
    第一次检索/编码多等约 1.5~3 秒，之后一直复用，直到闲置卸载（见 `_model_reaper`）。
    """
    if _STATE["model"] is not None:
        we.touch_model()
        return _STATE["model"], _STATE["processor"]
    with _MODEL_LOCK:
        # 双检：等锁期间可能已经有别的线程加载好了
        if _STATE["model"] is None:
            path = _STATE["model_path"] or we.DEFAULT_MODEL
            _STATE["model"], _STATE["processor"] = we.load_model(path)
        return _STATE["model"], _STATE["processor"]


def _model_reaper():
    """闲置守护线程：没人用模型就把它卸掉，把几 GB 还给系统。

    判据用 `embed.model_idle_seconds()`（缓存层计时，indexer 自己 load 也覆盖到），
    所以「用户一分钟内搜好几次」只会不断刷新计时，不会来回加载卸载。

    ★ 这里用 `_model_in_use()` 而不是 `_heavy_running()`：只要还有任务在用 WeMM
    就不能卸 —— 哪怕那个任务打的是远端官方 API。反过来，只跑视频转写（不碰
    WeMM）时是允许卸的，`_model_in_use` 认得出来。
    """
    while True:
        time.sleep(20)
        try:
            st = load_settings()
            if not st.get("model_lazy", True):
                continue
            if not we.model_loaded():
                continue
            if _model_in_use():
                continue
            mins = float(st.get("model_idle_minutes", 5) or 5)
            idle = we.model_idle_seconds()
            if idle < mins * 60:
                continue
            we.unload_model()
            _STATE["model"] = None
            _STATE["processor"] = None
            _STATE["model_unloaded"] += 1
            print(f"[模型] 已闲置 {idle / 60:.0f} 分钟，卸载释放内存"
                  f"（下次使用会重新加载，约 1.5~3 秒）", flush=True)
        except Exception as e:
            print(f"[模型] 闲置卸载跳过：{e}", flush=True)


def do_index(paths, force=False, kinds=None):
    """后台建索引（同一时间只允许一个任务），实时上报进度。

    kinds: 可选 list —— 只索引这些类型（image/video/audio/document），
           由「刷新」按钮按当前分类传入；None/空 = 全部类型。
    """
    if _STATE["busy"]:
        return {"error": "已有索引任务在运行", "since": _STATE["busy"]["started"]}
    for p in paths:
        if not _path_allowed(p):
            return {"error": f"路径不在允许范围: {p}"}
    kinds = [k for k in (kinds or []) if k in ("image", "video", "audio", "document")] or None

    def run():
        t0 = time.time()

        def on_progress(d):
            _STATE["progress"] = {**d, "updated": time.time()}

        result, status = None, "done"
        try:
            st = load_settings()
            # 索引期不做 AI 打标 / ASR 转写（见 indexer.build_index 顶部注释）：
            # 这两件事改由闲置守护线程与详情页按钮逐个完成，所以这里不再传配置。
            ai_cfg = None
            asr_cfg = None
            # 注入视频抽帧策略
            ix.FRAME_OPTS.update({
                "mode": st.get("video_density", "auto") or "auto",
                "custom_gap": st.get("video_custom_gap"),
                "max_frames": st.get("video_max_frames") or None,
            })
            result = ix.build_index(paths, force=force,
                                    model_path=_STATE["model_path"],
                                    progress=on_progress, ai_config=ai_cfg,
                                    asr_config=asr_cfg, kinds=kinds)
        except Exception as e:
            status = f"error: {e}"

        elapsed = round(time.time() - t0, 1)
        indexed = (result or {}).get("indexed")
        skipped = (result or {}).get("skipped")
        units = (result or {}).get("units")
        removed = (result or {}).get("removed") or 0
        failed = (result or {}).get("failed") or 0
        _STATE["last_index"] = {"status": status, "paths": paths, "kinds": kinds,
                                "elapsed": elapsed, "at": time.time(),
                                "indexed": indexed, "skipped": skipped,
                                "units": units, "removed": removed, "failed": failed}
        _STATE["busy"] = None
        _KN = {"image": "图片", "video": "视频", "audio": "音频", "document": "文档"}
        scope = ("／".join(_KN.get(k, k) for k in kinds)) if kinds else ""
        if status == "done":
            # 索引完顺手重算「同一首歌」关联（只比指纹切片、不解码音频，秒级）
            try:
                import fingerprint as fp
                links = fp.detect_links()
                if links:
                    op_log("同一首歌", f"建立 {len(links)} 条关联")
            except Exception as e:
                op_log("同一首歌", f"关联扫描失败：{e}", level="warn")
            if indexed:
                msg = f"完成：新增 {indexed} 个文件 / {units} 个向量"
            elif failed:
                msg = f"完成：没有新增文件（{failed} 个文件无法解析）"
            elif scope:
                msg = f"完成：{scope}没有发现新文件"
            else:
                msg = "完成：没有发现新文件"
            if indexed and failed:
                msg += f"，{failed} 个文件无法解析"
            if removed:
                msg += f"，清理 {removed} 个失效记录"
            op_log("索引", f"{msg}，耗时 {elapsed}s")
            _STATE["progress"] = {"phase": "done", "message": msg,
                                  "indexed": indexed, "units": units,
                                  "elapsed": elapsed, "updated": time.time()}
        else:
            op_log("索引", f"索引异常：{status}", level="warn")
            _STATE["progress"] = {"phase": "error", "message": status,
                                  "updated": time.time()}

    _STATE["last_result"] = None
    _STATE["progress"] = {"phase": "starting", "message": "启动索引…",
                          "total": 0, "current": 0, "updated": time.time()}
    _STATE["busy"] = {"paths": paths, "kinds": kinds, "started": time.time()}
    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "paths": paths, "kinds": kinds}


def do_search(query, top_k=10, kind=None, threshold=0.0, tier=None, source=None, lexical=True):
    """检索：默认按设置里的 search_tier（auto=三级递进，省资源）。

    source:  可选，限定在某个索引源目录内搜索。
    lexical: False 时不做文件名/路径加成，只按内容相似度算分。
             MCP 那条路永远传 False —— 文件名可能是随手起的、甚至写错的，
             Agent 应该按「文件里到底有什么」来找，而不是按名字。
    """
    if not tier:
        tier = load_settings().get("search_tier", "auto") or "auto"
    res = ix.search(query, top_k=top_k, kind=kind,
                    model_path=_STATE["model_path"], threshold=threshold,
                    tier=tier, lexical=lexical)

    # 只返回已添加索引源下的文件（与浏览视图口径一致）
    roots = []
    for s in load_sources():
        try:
            roots.append(os.path.realpath(s["path"]).rstrip("/"))
        except Exception:
            roots.append(s["path"].rstrip("/"))

    # source 指定时，进一步限定到该目录
    scope_root = None
    if source:
        try:
            scope_root = os.path.realpath(source).rstrip("/")
        except Exception:
            scope_root = source.rstrip("/")

    def under_sources(p):
        if not roots:
            return False
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        if scope_root:
            return rp == scope_root or rp.startswith(scope_root + "/")
        return any(rp == r or rp.startswith(r + "/") for r in roots)

    out = []
    meta_cache = {}
    for score, path, k, idx, meta in res:
        if not under_sources(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # 媒体元数据（同一文件只探测一次）
        if path not in meta_cache:
            try:
                meta_cache[path] = ix.probe_media_meta(path)
            except Exception:
                meta_cache[path] = {"size": size}
        m = dict(meta_cache[path])
        # 保留索引自带的帧时间戳（视频命中的是哪一帧，前端据此换封面 + 从该帧起播）
        matched_ts = None
        if meta and "timestamp" in meta:
            m["timestamp"] = meta["timestamp"]
            matched_ts = meta["timestamp"]
        out.append({"score": round(score, 4), "path": path, "kind": k,
                    "chunk_idx": idx, "meta": m, "size": size,
                    "matched_timestamp": matched_ts,
                    "name": os.path.basename(path),
                    "exists": os.path.exists(path)})
    op_log("检索", f"「{query[:24]}」{len(out)} 项 · {tier}")
    return {"query": query, "count": len(out), "results": out, "tier": tier}


def _decode_image_url(url):
    from PIL import Image
    if url.startswith("data:"):
        return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    if os.path.exists(url):
        return Image.open(url).convert("RGB")
    import urllib.request
    with urllib.request.urlopen(url, timeout=20) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def do_embeddings(body):
    import mlx.core as mx
    model, processor = _ensure_model()
    inp = body.get("input")
    dim = body.get("dimensions")
    items = [inp] if isinstance(inp, (str, dict)) else list(inp or [])
    data = []
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get("type") in ("image_url", "image"):
            u = item.get("image_url") or item.get("image")
            url = u.get("url") if isinstance(u, dict) else u
            vec = we.embed(model, processor, image=_decode_image_url(url),
                           instruction=we.QUERY_INSTRUCTION, dim=dim)
        else:
            text = item if isinstance(item, str) else (item.get("text") or "")
            vec = we.embed(model, processor, text=text,
                           instruction=we.QUERY_INSTRUCTION, dim=dim)
        mx.eval(vec)
        data.append({"object": "embedding", "index": i, "embedding": vec.tolist()})
    return {"object": "list", "data": data, "model": SERVICE_NAME}


def do_rerank(body):
    import mlx.core as mx
    model, processor = _ensure_model()
    q = body.get("query")
    docs = body.get("documents") or []
    top_n = body.get("top_n")
    if not q or not docs:
        raise ValueError("'query' 和 'documents' 必填")

    def emb(t, instr):
        v = we.embed(model, processor, text=t, instruction=instr)
        mx.eval(v)
        return v

    vq = emb(q, we.QUERY_INSTRUCTION)
    scored = []
    for i, d in enumerate(docs):
        t = d if isinstance(d, str) else (d.get("text") or "")
        scored.append({"index": i, "relevance_score": float(mx.sum(vq * emb(t, we.DOC_INSTRUCTION))),
                       "document": {"text": t}})
    scored.sort(key=lambda x: -x["relevance_score"])
    return {"model": SERVICE_NAME, "results": scored[:top_n] if top_n else scored}


# --------------------------------------------------------------------------
# 网页 UI
# --------------------------------------------------------------------------
def _ui_html():
    """读取外部 ui.html（便于改界面，不用动后端）。"""
    p = os.path.join(HERE, "ui.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# 面板控制钩子（桌面版用）
# --------------------------------------------------------------------------
# 菜单栏图标是个独立小进程（tray_helper.py），它够不到我们的窗口对象，
# 所以「打开面板 / 隐藏面板 / 退出」这三件事走本机 HTTP 转交给 launcher.py：
# 它启动时把三个回调塞进 PANEL_HOOKS（见 launcher.py 的 _install_panel_hooks）。
# 命令行跑 app.py 时没人注册，这里就老老实实回一句「只在桌面版里可用」。
PANEL_HOOKS = {}
# 没注册钩子时回什么话，按功能分开说 —— 「麦克风」和「打开面板」对用户是两件事
_PANEL_HOOK_MISSING = {
    "mic": "应用内录音只在桌面版（FXseek.app）里走原生接口；浏览器里请允许麦克风权限",
}

# --------------------------------------------------------------------------
# 本地服务的实例：菜单栏的「停止服务器 / 启动服务器」要用它。
# 以前 run_server 里是 `ThreadingHTTPServer(...).serve_forever()` 一行，
# 对象谁都没留着引用，也就没法停。现在存下来 —— 停了之后**模型、索引线程、
# 用户数据都不动**，再点「启动服务器」就是原样把监听重新架起来。
class _Server(ThreadingHTTPServer):
    """本地服务用的 HTTP 服务器。

    ★ request_queue_size 默认只有 5（socketserver.TCPServer 的历史默认值）。
      素材瀑布一屏就是几十张卡，浏览器会**同时**发几十个 /v1/thumb，
      排在 5 个之后的连接直接被内核丢掉 —— 客户端看到的就是
      `Connection reset by peer`。实测 34 个并发取图，25 个被掐断，
      表现就是「搜完之后缩略图半天不出来」。
      调到 256：并发取图不再被 backlog 卡死。
    """
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


# 缩略图**生成**（缓存未命中）的并发闸门：一屏几十张卡同时进来会瞬间拉起几十个
# ffmpeg 子进程，把 CPU 抢烂反而全都变慢；排队等一小会儿比一起挤好。
_THUMB_SEM = threading.BoundedSemaphore(6)

# 音频波形缩略图的**公共底图**（渐变 + 光晕）。它与文件无关，每张卡都一模一样，
# 所以只算一次缓存住。原来这两步是纯 Python 逐像素循环（420×140≈5.9 万次）外加
# 一次 GaussianBlur(85)，单张几百毫秒，而且全程占着 GIL —— 并发生成时线程互相
# 排队，几十张卡能拖到十几秒。
_WAVE_BASE = {}


def _wave_base(W, H):
    hit = _WAVE_BASE.get((W, H))
    if hit is not None:
        return hit
    from PIL import Image, ImageDraw, ImageFilter
    im = Image.new("RGB", (W, H))
    px = im.load()
    c0 = (24, 30, 54)      # 左上：深靛
    c1 = (46, 38, 88)      # 右下：深紫
    for y in range(H):
        for x in range(0, W, 4):
            t = (x / W * 0.55 + y / H * 0.45)
            r = int(c0[0] + (c1[0] - c0[0]) * t)
            g = int(c0[1] + (c1[1] - c0[1]) * t)
            b = int(c0[2] + (c1[2] - c0[2]) * t)
            for k in range(4):
                if x + k < W:
                    px[x + k, y] = (r, g, b)
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 300, -190, W + 130, 240], fill=(96, 58, 160))
    glow = glow.filter(ImageFilter.GaussianBlur(85))
    im = Image.blend(im, Image.blend(im, glow, 1.0), 0.42)
    _WAVE_BASE[(W, H)] = im
    return im

_SRV = {"inst": None, "host": "127.0.0.1", "port": 8231, "thread": None}


def server_running() -> bool:
    return _SRV.get("inst") is not None


def server_stop() -> dict:
    """停掉本地服务（只关监听，不碰窗口和索引）。

    为什么要延 0.4 秒再动手：这个函数正是「点菜单」那个 HTTP 请求在跑的，
    当场关掉就把承载这次响应的服务一起掐了 —— 菜单助手会看到一个断开的连接，
    以为失败。先让响应发出去，再关。
    """
    srv = _SRV.get("inst")
    if srv is None:
        return {"ok": True, "running": False, "note": "服务本来就没在跑"}

    def _later():
        time.sleep(0.4)
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
        if _SRV.get("inst") is srv:
            _SRV["inst"] = None
            _SRV["thread"] = None
        print("[服务] 本地服务已停止（端口 %d 留着，随时能再起）" % _SRV["port"])

    threading.Thread(target=_later, daemon=True, name="fxseek-server-stop").start()
    return {"ok": True, "running": False, "port": _SRV["port"]}


def server_start() -> dict:
    """把监听重新架起来。模型还在内存里，所以这条路径很快。"""
    if _SRV.get("inst") is not None:
        return {"ok": True, "running": True, "note": "服务已经在跑"}
    srv = _Server((_SRV["host"], _SRV["port"]), Handler)
    _SRV["inst"] = srv
    th = threading.Thread(target=srv.serve_forever, daemon=True, name="fxseek-server")
    _SRV["thread"] = th
    th.start()
    print("[服务] 本地服务已重新启动：http://%s:%d/" % (_SRV["host"], _SRV["port"]))
    return {"ok": True, "running": True, "port": _SRV["port"]}


def server_state() -> dict:
    return {"running": server_running(), "port": _SRV["port"],
            "url": "http://%s:%d/" % (_SRV["host"], _SRV["port"])}


# 服务开关是 app.py 自己的事（命令行跑也给用），所以做成**内置钩子**：
# 桌面版在 PANEL_HOOKS 里注册同名钩子时会覆盖它（那边还要顺手管窗口）。
_PANEL_BUILTIN = {
    "server_start": server_start,
    "server_stop": server_stop,
    "server_state": server_state,
}


def _panel_hook(name):
    fn = PANEL_HOOKS.get(name)
    if fn is None:
        fn = _PANEL_BUILTIN.get(name)
    if fn is None:
        for k, msg in _PANEL_HOOK_MISSING.items():
            if name.startswith(k):
                return {"error": msg}
        return {"error": "面板控制只在桌面版（FXseek.app）里可用"}
    try:
        # 钩子可以返回 dict（比如录音要回 base64）；返回 None 就按「成了」处理
        res = fn()
        return res if isinstance(res, dict) else {"ok": True}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def _settings_html():
    """读取外部 settings.html。"""
    p = os.path.join(HERE, "settings.html")
    with open(p, encoding="utf-8") as f:
        return f.read()



# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[app] " + (fmt % args) + "\n")

    def handle_one_request(self):
        # 每个请求都打一次时间戳；但「闲置自动打标」只看 last_user_req，
        # 前端自己的状态轮询不算用户活动（否则闲置计时永远清零，见 _QUIET_PATHS）
        now = time.time()
        _STATE["last_req"] = now
        # 必须**在**父类解析完请求行之后再读 self.path：
        # 之前把它放在前面，第一次调用时 self.path 还不存在（AttributeError），
        # 放后面又只能读到上一条请求的路径，所以顺序不能变。
        BaseHTTPRequestHandler.handle_one_request(self)
        try:
            path = (self.path or "").split("?")[0]
        except AttributeError:
            path = ""
        if path not in _QUIET_PATHS:
            _STATE["last_user_req"] = now

    def _json(self, code, obj):
        p = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(p)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(p)

    def _bytes(self, code, data, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():      # 少数自带资源要长缓存
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self._bytes(200, _ui_html().encode(), "text/html; charset=utf-8",
                        extra={"Cache-Control": "no-cache"})
        elif u.path == "/icons.js":
            p_js = os.path.join(HERE, "icons.js")
            with open(p_js, encoding="utf-8") as f:
                # no-cache：这两个 .js 是本机小文件，加了图标却看不到「已经改过」
                # 是最费解的一类问题（详见 README「换图之后…」那节）。
                self._bytes(200, f.read().encode(),
                            "application/javascript; charset=utf-8",
                            extra={"Cache-Control": "no-cache"})
        elif u.path == "/agent_logos.js":
            # 各 Agent 的官方 logo（内联 SVG 路径 + base64 位图），目前只有设置页用。
            # 和 icons.js 一样本地服务：打包成 app 后要完全离线可用。
            p_js = os.path.join(HERE, "agent_logos.js")
            with open(p_js, encoding="utf-8") as f:
                self._bytes(200, f.read().encode(),
                            "application/javascript; charset=utf-8",
                            extra={"Cache-Control": "no-cache"})
        elif u.path == "/i18n.js":
            # 中英文字典 + DOM 翻译器（见 README「界面语言」那节）。
            # 改成 no-cache：加了词条却看不到，是最费解的一类问题。
            p_js = os.path.join(HERE, "i18n.js")
            with open(p_js, encoding="utf-8") as f:
                self._bytes(200, f.read().encode(),
                            "application/javascript; charset=utf-8",
                            extra={"Cache-Control": "no-cache"})
        elif u.path.startswith("/vendor/"):
            # 本地化的第三方前端库（docx-preview / SheetJS / JSZip）。
            # 必须本地服务：打包成 app 后要完全离线可用，不能依赖 CDN。
            self._vendor(u.path)
        elif u.path.startswith("/assets/"):
            # 应用自带的小图（打赏二维码等）。同理必须本地服务——
            # 这些东西不能挂外链，也不该塞成 data: URI 把 HTML 撑肥。
            self._assets(u.path)
        elif u.path == "/health":
            self._json(200, {"status": "ok", "service": SERVICE_NAME,
                             "model": _STATE["model_path"],
                             "busy": bool(_STATE["busy"]),
                             "progress": _STATE.get("progress"),
                             "last_index": _STATE["last_index"],
                             "ffmpeg": ix.ffmpeg_status()})
        elif u.path == "/v1/stats":
            src = qs.get("source", [None])[0]
            self._json(200, scoped_stats(source=src))
        elif u.path == "/v1/about":
            self._json(200, about_info())
        elif u.path == "/v1/update/check":
            force = (qs.get("force", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            self._json(200, update_check(force=force))
        elif u.path == "/v1/settings":
            self._json(200, load_settings())
        elif u.path == "/v1/cache":
            self._json(200, cache_info())
        elif u.path == "/v1/gc":
            orphans = (qs.get("orphans", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            self._json(200, gc_index(orphans=orphans, dry_run=True))
        elif u.path == "/v1/paths":
            # 顺带把搜索记录 / 最近上传的条数带上：设置页那行统计要显示它们，
            # 不然只报个文件大小，用户看不出里面有多少条记录。
            _d = P.describe()
            try:
                _h = ui_history_get()
                _d["hist"] = {"search": len(_h["search"]),
                              "uploads": len(_h["uploads"]),
                              "seeded": bool(_h.get("seeded"))}
            except Exception:
                _d["hist"] = {}
            self._json(200, _d)
        elif u.path == "/v1/ai/status":
            self._json(200, ai_tag_status())
        elif u.path == "/v1/asr/status":
            self._json(200, asr_status())
        elif u.path == "/v1/mcp/status":
            self._json(200, mcp_state())
        elif u.path == "/v1/server/state":
            # 菜单栏那条状态行靠它刷新（服务停了以后托盘只能用命令文件）
            self._json(200, server_state())
        elif u.path == "/v1/trash":
            self._json(200, trash_list())
        elif u.path == "/v1/logs":
            self._json(200, logs_list())
        elif u.path == "/v1/links":
            import fingerprint as fp
            con = fp._init_db()
            rows = con.execute("SELECT a,b,votes,base_frames,score,margin FROM song_links"
                               " ORDER BY score DESC").fetchall()
            con.close()
            self._json(200, {"count": len(rows), "links": [
                {"a": a, "b": b, "a_name": os.path.basename(a),
                 "b_name": os.path.basename(b), "votes": v,
                 "offset_sec": round(bf * fp.LIB_FRAME, 2), "score": sc, "margin": mg}
                for a, b, v, bf, sc, mg in rows]})
        elif u.path == "/v1/sources":
            self._json(200, {"sources": sources_with_stats()})
        elif u.path == "/v1/seed/history":
            # 首次启动的演示记录（搜索历史 / 最近上传）。这两个列表真正的家是
            # 用户数据目录里的 history.json（见 ui_history_get/put），这里只把
            # 种子内容吐出去，由前端在「服务端和本地都一条记录都没有」时写进去。
            self._json(200, SEED.demo_history())
        elif u.path == "/v1/ui/history":
            # 搜索记录 / 最近上传的服务端副本（存用户数据目录，别只靠 localStorage）
            self._json(200, ui_history_get())
        elif u.path == "/v1/seed/thumb":
            # 「最近上传」演示条目的缩略图：前端只拿到文件名，图片本身在示例素材
            # 目录里，这里按名字找出来缩成小 JPEG 的 dataURL 返回。
            self._json(200, seed_thumb(qs.get("name", [""])[0],
                                       qs.get("w", ["160"])[0]))
        elif u.path == "/v1/seed/bytes":
            # 「最近上传」里那两条演示记录点下去要真跑一次检索，而检索要的是原始
            # 字节（图片走视觉编码、音频走指纹），只有文件名不够。这里按名字把
            # 体验素材读出来给前端。只认文件名、不接受路径，所以不会穿越。
            self._json(200, seed_bytes(qs.get("name", [""])[0]))
        elif u.path == "/v1/cache/stats":
            self._json(200, cache_stats())
        elif u.path == "/v1/export":
            # 桌面版走原生 NSSavePanel（launcher 注册的钩子，不会多 Dock 图标）；
            # 浏览器里没人注册钩子，回落 osascript 的 choose file name。
            if "export_file" in PANEL_HOOKS:
                self._json(200, _panel_hook("export_file"))
            else:
                self._json(200, export_config_to_file())
        elif u.path == "/settings":
            self._bytes(200, _settings_html().encode(), "text/html; charset=utf-8",
                        extra={"Cache-Control": "no-cache"})
        elif u.path == "/v1/thumb":
            t_arg = qs.get("t", [None])[0]
            try:
                t_val = float(t_arg) if t_arg not in (None, "") else None
            except ValueError:
                t_val = None
            try:
                w_val = int(qs.get("w", ["560"])[0])
            except ValueError:
                w_val = 560
            self._thumb(qs.get("path", [""])[0], at=t_val, w=w_val)
        elif u.path == "/v1/text":
            try:
                self._json(200, read_text_preview(qs.get("path", [""])[0]))
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif u.path == "/v1/detail":
            try:
                self._json(200, file_detail(qs.get("path", [""])[0]))
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif u.path == "/v1/file":
            self._file(qs.get("path", [""])[0])
        else:
            self._json(404, {"error": "not found"})

    def _vendor(self, url_path):
        """服务 vendor/ 下的本地第三方前端库。

        只允许白名单文件名，且用 realpath 二次校验防目录穿越 ——
        这条路经受 URL 直接访问，不能只靠字符串拼接。
        """
        VENDOR_ALLOW = {
            "docx-preview.min.js": "application/javascript; charset=utf-8",
            "xlsx.full.min.js":    "application/javascript; charset=utf-8",
            "jszip.min.js":        "application/javascript; charset=utf-8",
        }
        name = url_path[len("/vendor/"):].split("?")[0]
        if name not in VENDOR_ALLOW:
            self._json(404, {"error": "vendor asset not found"}); return
        vdir = os.path.realpath(os.path.join(HERE, "vendor"))
        fp = os.path.realpath(os.path.join(vdir, name))
        if not fp.startswith(vdir + os.sep) or not os.path.exists(fp):
            self._json(404, {"error": "vendor asset not found"}); return
        try:
            with open(fp, "rb") as f:
                data = f.read()
            self._bytes(200, data, VENDOR_ALLOW[name])
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _assets(self, url_path):
        """服务 assets/ 下的应用自带小图（目前是打赏二维码）。

        和 _vendor 同一套做法：**白名单 + realpath 二次校验**。
        这条路经受 URL 直接访问，不能只靠字符串拼接来防目录穿越。
        白名单是刻意的——assets/ 以后要是放了别的东西，也不会因为
        多丢一个文件进去就自动暴露出去。
        """
        ASSET_ALLOW = {
            "donate-wechat.jpg": "image/jpeg",
            "donate-alipay.jpg": "image/jpeg",
            "baboon_white.png": "image/png",
            "baboon_menu_template.png": "image/png",
            # 品牌 logo（左上角 / 设置页「关于」都用它）；白名单漏了它 → 404 → 方块空着
            "logo_baboon.png": "image/png",
        }
        name = url_path[len("/assets/"):].split("?")[0]
        if name not in ASSET_ALLOW:
            self._json(404, {"error": "asset not found"}); return
        adir = os.path.realpath(os.path.join(HERE, "assets"))
        fp = os.path.realpath(os.path.join(adir, name))
        if not fp.startswith(adir + os.sep) or not os.path.exists(fp):
            self._json(404, {"error": "asset not found"}); return
        try:
            with open(fp, "rb") as f:
                data = f.read()
            # 这几张图很小（几十 KB）而且是本地读取，不值得长缓存：
            # 一旦长缓存，换了图用户一周内还看旧图，反而更麻烦。
            self._bytes(200, data, ASSET_ALLOW[name],
                        extra={"Cache-Control": "no-cache"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _thumb(self, path, at=None, w=560):
        """统一缩略图：图片/视频(ffmpeg 抽帧)/文档(文本卡)/音频(波形)。带磁盘缓存。

        at: 视频抽帧时间点（秒）；指定时用该时刻画面作为封面（用于以图搜图定位）。
        w:  边长上限（默认 560）。「漫步时光」长廊的卡片只有 ~250px 宽，
            按 560 解码等于白白多占几倍显存，卡多了浏览器会丢图层 → 整列闪一下，
            所以长廊按 w=360 取图；w 进缓存 key，两种尺寸各缓存各的。
        """
        w = max(160, min(1200, int(w or 560)))
        if not path or not _path_allowed(path) or not os.path.exists(path):
            self._json(404, {"error": "bad path"}); return
        ext = os.path.splitext(path)[1].lower()
        try:
            cache_dir = P.THUMB_DIR
            os.makedirs(cache_dir, exist_ok=True)
            import hashlib
            key = hashlib.sha1(f"v2|{path}|{os.path.getmtime(path)}|{at}|{w}".encode()).hexdigest()[:24]
            cache_fp = os.path.join(cache_dir, key + ".jpg")
            if os.path.exists(cache_fp):
                self._bytes(200, open(cache_fp, "rb").read(), "image/jpeg"); return

            _THUMB_SEM.acquire()
            try:
                img = self._make_thumb_image(path, ext, at=at)
            finally:
                _THUMB_SEM.release()
            if img is None:
                self._json(404, {"error": "cannot render thumb"}); return
            img.thumbnail((w, w))
            buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=84)
            data = buf.getvalue()
            try:
                with open(cache_fp, "wb") as f: f.write(data)
                # 写完顺手按 cache_limit_mb 淘汰旧缓存（超限才会动手）
                _evicted = P.enforce_thumb_limit(load_settings().get("cache_limit_mb", 1024))
                if _evicted:
                    op_log("缓存", f"缩略图超上限，淘汰 {_evicted} 个旧文件")
            except OSError:
                pass
            self._bytes(200, data, "image/jpeg")
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _make_thumb_image(self, path, ext, at=None):
        """按类型生成缩略图（返回 PIL.Image）。at 指定视频抽帧时间点。"""
        from PIL import Image

        # 1) 图片：直接读
        if ext in ix.IMAGE_EXT:
            return Image.open(path).convert("RGB")

        # 2) 视频：抽取指定时刻（默认中间帧，避开黑屏首帧）
        if ext in ix.VIDEO_EXT:
            import subprocess, tempfile
            dur = ix._probe_duration(path)
            if at is not None:
                t = max(0.0, min(float(at), max(0.0, dur - 0.05)))
            else:
                t = dur / 2 if dur > 1 else 0
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "f.jpg")
                try:
                    subprocess.run([ix.FFMPEG, "-ss", str(t), "-i", path,
                                    "-frames:v", "1", "-vf", "scale=560:-2",
                                    "-q:v", "3", "-y", fp],
                                   capture_output=True, timeout=60)
                except Exception:
                    pass
                if os.path.exists(fp):
                    return self._overlay_play(Image.open(fp).convert("RGB"))
            frames = ix._video_frames(path)
            if frames:
                return self._overlay_play(frames[0][0])
            return self._placeholder("🎬 视频", "#e0e7ff")

        # 3) 音频：优先用封面（内嵌 APIC / 同目录封面图），没有才回落到波形
        if ext in ix.AUDIO_EXT:
            cov = self._audio_cover(path)
            if cov is not None:
                try:
                    return self._cover_card(cov)
                except Exception as e:
                    print(f"  [封面合成失败] {path}: {e}")
            return self._audio_waveform(path)

        # 4) 文档：文本摘要卡片
        text = ""
        if ext in ix.DOC_EXT:
            try:
                text = ix._read_text(path)[:800]
            except Exception as e:
                print(f"  [文档读取失败] {path}: {e}")
                text = ""
        if text.strip():
            return self._text_card(os.path.basename(path), text)
        # 显示具体扩展名
        ext_label = ext.upper().lstrip('.') or 'DOC'
        return self._placeholder(f"📄 {ext_label}", "#f3f4f6")

    def _overlay_play(self, img):
        """在视频缩略图中央画播放按钮。"""
        from PIL import ImageDraw
        w, h = img.size
        d = ImageDraw.Draw(img, "RGBA")
        cx, cy, r = w // 2, h // 2, max(24, min(w, h) // 7)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 135))
        d.polygon([(cx - r // 3, cy - r // 2), (cx - r // 3, cy + r // 2),
                   (cx + r // 2, cy)], fill=(255, 255, 255, 245))
        return img

    def _placeholder(self, label, bg):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (560, 420), bg)
        d = ImageDraw.Draw(im)
        d.text((40, 196), label, fill="#9ca3af", font=self._font(18))
        return im

    def _font(self, size=15):
        """加载支持中文的系统字体（PIL 默认字体不含中文字形）。"""
        from PIL import ImageFont
        cands = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Songti.ttc",
        ]
        for p in cands:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
        try:
            return ImageFont.load_default(size)
        except Exception:
            return ImageFont.load_default()

    def _text_card(self, name, text):
        """把文档前几行渲染成缩略卡（支持中文）。"""
        from PIL import Image, ImageDraw
        W, H = 560, 420
        im = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(im)
        f_head = self._font(16)
        f_body = self._font(14)
        d.rectangle([0, 0, W, 56], fill="#f3f4f6")
        d.text((18, 19), "📄 " + name[:34], fill="#374151", font=f_head)
        lines, y = [], 82
        for raw in text.splitlines():
            raw = " ".join(raw.split())
            if not raw:
                continue
            while len(raw) > 26:
                lines.append(raw[:26]); raw = raw[26:]
            lines.append(raw)
            if len(lines) >= 13:
                break
        for ln in lines[:13]:
            d.text((18, y), ln, fill="#6b7280", font=f_body)
            y += 25
        d.rectangle([0, H - 5, W, H], fill="#c8f542")
        return im

    def _audio_cover(self, path):
        """取音频封面：优先内嵌（ID3 APIC / MP4 covr / FLAC picture），其次同目录封面图。

        ★ 这些封面一直就在文件里 —— ffmpeg 把附加图当作一路 video 流暴露出来，
        以前音频缩略图一律走程序生成的波形，等于把封面白扔了。
        取不到返回 None，调用方回落到波形缩略图。
        """
        from PIL import Image
        import subprocess, io as _io
        # 1) 内嵌封面：抽第一路视频流的第一帧
        try:
            r = subprocess.run([ix.FFMPEG, "-v", "error", "-i", path,
                                "-map", "0:v:0", "-frames:v", "1",
                                "-f", "image2", "-c:v", "mjpeg", "-"],
                               capture_output=True, timeout=30)
            if r.returncode == 0 and r.stdout:
                im = Image.open(_io.BytesIO(r.stdout))
                im.load()
                return im.convert("RGB")
        except Exception:
            pass
        # 2) 同目录封面图（cover/folder/front/album/artwork、封面/专辑…）
        try:
            d = os.path.dirname(path)
            for n in sorted(os.listdir(d)):
                if os.path.splitext(n)[1].lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                    continue
                low = n.lower()
                if (low.startswith(("cover", "folder", "front", "album", "artwork", "disc"))
                        or n.startswith(("封面", "专辑"))):
                    with Image.open(os.path.join(d, n)) as im:
                        return im.convert("RGB")
        except Exception:
            pass
        return None

    def _cover_card(self, cover):
        """把封面排成一张卡片图：模糊放大做底 + 居中圆角方形封面。

        故意**不**叠播放键 —— 音频卡片中央那个播放键是真实 DOM 按钮
        （`.playbadge.audplay`），烘进图里会和它重叠成两个。
        """
        from PIL import Image, ImageDraw, ImageFilter
        W, H = 560, 420
        bg = cover.copy()
        sc = max(W / max(1, bg.width), H / max(1, bg.height))
        bg = bg.resize((max(W, int(bg.width * sc)), max(H, int(bg.height * sc))), Image.LANCZOS)
        left, top = (bg.width - W) // 2, (bg.height - H) // 2
        bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(30))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (10, 12, 18)), 0.5)

        side = int(H * 0.74)
        fg = cover.copy()
        s = min(fg.width, fg.height)
        fg = fg.crop(((fg.width - s) // 2, (fg.height - s) // 2,
                      (fg.width + s) // 2, (fg.height + s) // 2))
        fg = fg.resize((side, side), Image.LANCZOS)
        mask = Image.new("L", (side, side), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, side - 1, side - 1],
                                               radius=16, fill=255)
        bg.paste(fg, ((W - side) // 2, (H - side) // 2), mask)
        return bg

    def _audio_waveform(self, path):
        """音频缩略图：深色渐变底 + 对称圆角波形 + 中央播放键 + 底部时长胶囊。

        配色与界面品牌渐变（#6366f1 → #a855f7 → #ec4899）及音频色（青蓝）协调；
        波形取自真实采样峰值，静音/极短音频回落到合成波形，保证视觉不塌。
        """
        from PIL import Image, ImageDraw, ImageFilter
        import subprocess, array, tempfile, math, random

        W, H = 560, 420
        # ---- 1+2. 渐变底 + 右上角光晕：每张卡都一样，只算一次（见 _wave_base）----
        im = _wave_base(W, H).copy()
        d = ImageDraw.Draw(im, "RGBA")

        # ---- 3. 取真实波形峰值；失败则用平滑合成波，避免出现空白 ----
        #   -t 60：只解码前 60 秒。原先整首歌解成 8kHz 原始 PCM，
        #   6 分钟的曲子要解 2.8 MB 出来，纯属浪费 —— 缩略图看不出差别。
        peaks = []
        try:
            with tempfile.TemporaryDirectory() as td:
                fp = os.path.join(td, "a.raw")
                subprocess.run([ix.FFMPEG, "-t", "60", "-i", path,
                                "-ac", "1", "-ar", "8000",
                                "-f", "s16le", "-y", fp],
                               capture_output=True, timeout=90)
                if os.path.exists(fp) and os.path.getsize(fp) > 1000:
                    with open(fp, "rb") as f:
                        raw = f.read()
                    samples = array.array("h")
                    samples.frombytes(raw[:len(raw) // 2 * 2])
                    step = max(1, len(samples) // 54)
                    for i2 in range(0, max(1, len(samples) - step), step):
                        chunk = samples[i2:i2 + step]
                        peaks.append(max(abs(v) for v in chunk) / 32768.0)
        except Exception:
            peaks = []
        if not peaks or max(peaks) < 0.02:
            rnd = random.Random(hash(os.path.basename(path)) & 0xffff)
            peaks = [0.22 + 0.55 * abs(math.sin(i * 0.42)) * (0.55 + 0.45 * rnd.random())
                     for i in range(54)]

        # ---- 4. 对称圆角波形条，颜色沿中线做青→紫渐变 ----
        N = 54                                  # 条数克制，避免密成栅栏
        if len(peaks) > N:                      # 均匀重采样到 N 根
            stepf = len(peaks) / N
            peaks = [max(peaks[int(k * stepf):int((k + 1) * stepf)] or [0]) for k in range(N)]
        elif len(peaks) < N:
            peaks = (peaks * (N // max(1, len(peaks)) + 1))[:N]
        mid = int(H * 0.465)                    # 波形中线略高于几何中心，给右下角时长留位
        pad_x = 96                              # 左右留白加大，波形不贴边
        bw = (W - pad_x * 2) / N
        bar_w = max(3.2, bw * 0.42)
        max_h = H * 0.245
        for k, pk in enumerate(peaks):
            t = k / max(1, N - 1)
            # 青蓝 (56,189,248) → 紫 (168,85,247)
            r = int(56 + (168 - 56) * t)
            g = int(189 + (85 - 189) * t)
            b = int(248 + (247 - 248) * t)
            bh = max(4.0, pk ** 0.72 * max_h)
            x = pad_x + k * bw + (bw - bar_w) / 2
            d.rounded_rectangle([x, mid - bh, x + bar_w, mid + bh],
                                radius=bar_w / 2, fill=(r, g, b, 235))

        # ---- 5. 中央磨砂播放键（与视频缩略图同一视觉语言）----
        cx, cy, R = W // 2, mid, 46
        d.ellipse([cx - R - 8, cy - R - 8, cx + R + 8, cy + R + 8], fill=(0, 0, 0, 70))
        d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(255, 255, 255, 235))
        d.polygon([(cx - 12, cy - 17), (cx - 12, cy + 17), (cx + 18, cy)],
                  fill=(38, 34, 62, 255))

        # ---- 6. 底部信息条：文件时长（若有 ffprobe）----
        try:
            pr = subprocess.run([ix.FFPROBE, "-v", "error", "-show_entries",
                                 "format=duration", "-of",
                                 "default=noprint_wrappers=1:nokey=1", path],
                                capture_output=True, timeout=20)
            secs = float((pr.stdout or b"0").decode().strip() or 0)
            if secs > 0:
                mm, ss = divmod(int(secs), 60)
                label = f"{mm:02d}:{ss:02d}"
                f = self._font(17)
                tw = d.textlength(label, font=f)
                bw2, bh2 = tw + 26, 30
                bx, by = W - bw2 - 18, H - bh2 - 16
                d.rounded_rectangle([bx, by, bx + bw2, by + bh2], radius=8,
                                    fill=(0, 0, 0, 165))
                d.text((bx + 13, by + 6), label, fill=(240, 244, 255), font=f)
        except Exception:
            pass
        return im

    def _file(self, path):
        """原文件响应，支持 HTTP Range（视频/音频可拖拽播放、边下边播）。"""
        if not path or not _path_allowed(path) or not os.path.exists(path):
            self._json(404, {"error": "bad path"}); return
        if os.path.isdir(path):
            self._json(400, {"error": "is a directory"}); return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(path)
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                spec = rng.split("=", 1)[1].split(",")[0].strip()
                a, _, b = spec.partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                start = max(0, start)
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            # 文本类也 inline（内置预览），只有真正的二进制/危险类型才强制下载
            inline_ok = (
                ctype.startswith(("image/", "video/", "audio/", "text/"))
                or ctype in ("application/pdf", "application/json")
            )
            self.send_header("Content-Disposition",
                             "inline" if inline_ok else "attachment")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json(400, {"error": str(e)}); return

        t0 = time.time()
        try:
            if u.path == "/v1/find":
                # 按文件名找 —— 与 /v1/search 的语义检索刻意分开，两条路互不污染。
                res = find_by_name(body.get("name", ""), body.get("kind"),
                                   body.get("limit", 30), body.get("source"))
            elif u.path == "/v1/search":
                res = do_search(body.get("query", ""), body.get("top_k", 10),
                                body.get("kind"), body.get("threshold", 0.0),
                                tier=body.get("tier", "auto"),
                                source=body.get("source"),
                                lexical=bool(body.get("lexical", True)))
            elif u.path == "/v1/browse":
                res = browse_all(body.get("kind"), body.get("limit", 300),
                                 body.get("sort", "recent"),
                                 source=body.get("source"))
            elif u.path == "/v1/open":
                res = open_with_system(body.get("path", ""))
            elif u.path.startswith("/v1/mic/"):
                # 桌面版原生录音（WKWebView 从访达启动时拿不到麦克风，详见 recorder.py）。
                # 钩子在 launcher.py 的 _install_panel_hooks 里注册；浏览器里打开时没人注册，
                # 会回一句「只在桌面版里可用」，前端据此改走 getUserMedia。
                res = _panel_hook("mic_" + u.path.rsplit("/", 1)[-1].replace("-", "_"))
            elif u.path in ("/v1/panel/show", "/v1/panel/hide", "/v1/panel/settings",
                            "/v1/panel/settings-mcp", "/v1/app/quit"):
                # 菜单栏图标助手（独立进程）点菜单时调的那几个口子
                # settings-mcp → settings_mcp：直接开在设置页的 MCP 那一节
                res = _panel_hook(u.path.rsplit("/", 1)[-1].replace("-", "_"))
            elif u.path in ("/v1/server/start", "/v1/server/stop"):
                # 「停止服务器 / 启动服务器」。桌面版在 launcher 里注册了同名钩子
                # （除了动服务还要管窗口），命令行跑时走 _PANEL_BUILTIN。
                res = _panel_hook("server_" + u.path.rsplit("/", 1)[-1])
            elif u.path == "/v1/pick-folder":
                # 桌面版走原生 NSOpenPanel（不额外亮 Dock 图标）；浏览器回落 osascript。
                if "pick_folder" in PANEL_HOOKS:
                    res = _panel_hook("pick_folder")
                else:
                    res = pick_folder()
            elif u.path == "/v1/sources/add":
                res = add_source(body.get("path", ""))
            elif u.path == "/v1/sources/scan":
                res = scan_source(body.get("path", ""))
            elif u.path == "/v1/sources/remove":
                res = remove_source(body.get("path", ""))
            elif u.path == "/v1/sources/reorder":
                res = reorder_sources(body.get("paths") or [])
            elif u.path == "/v1/sources/index":
                res = index_sources()
            elif u.path == "/v1/search-image":
                img_b64 = body.get("image") or ""
                if not img_b64:
                    res = {"error": "缺少 image 字段（base64）"}
                else:
                    try:
                        if "," in img_b64 and img_b64.strip().startswith("data:"):
                            img_b64 = img_b64.split(",", 1)[1]
                        raw = base64.b64decode(img_b64)
                        from PIL import Image as _PILImage
                        img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
                        items = search_by_image(img, top_k=int(body.get("top_k", 20)),
                                                kind=body.get("kind"))
                        res = {"count": len(items), "results": items,
                               "source": {"width": img.size[0], "height": img.size[1]}}
                    except Exception as e:
                        res = {"error": f"图片解析失败：{e}"}
            elif u.path == "/v1/refresh":
                res = index_sources(body.get("kinds"))
            elif u.path == "/v1/index":
                res = do_index(body.get("paths", []), body.get("force", False))
            elif u.path == "/v1/embeddings":
                res = do_embeddings(body)
            elif u.path == "/v1/rerank":
                res = do_rerank(body)
            elif u.path == "/v1/settings":
                res = save_settings(body)
            elif u.path == "/v1/ai/test":
                res = ai_test(body)
            elif u.path == "/v1/asr/test":
                res = asr_test(body)
            elif u.path == "/v1/audio-search":
                res = audio_search(body)
            elif u.path == "/v1/links":
                # GET：列出当前「同一首歌」关联；POST {rebuild:true}：重算
                import fingerprint as fp
                if body.get("rebuild"):
                    fp.detect_links()
                con = fp._init_db()
                rows = con.execute(
                    "SELECT a,b,votes,base_frames,score,margin FROM song_links "
                    "ORDER BY score DESC").fetchall()
                con.close()
                res = {"count": len(rows), "links": [
                    {"a": a, "b": b, "a_name": os.path.basename(a),
                     "b_name": os.path.basename(b), "votes": v,
                     "offset_sec": round(bf * fp.LIB_FRAME, 2),
                     "score": sc, "margin": mg}
                    for a, b, v, bf, sc, mg in rows]}
            elif u.path == "/v1/voice-search":
                res = voice_search(body)
            elif u.path == "/v1/logs/clear":
                res = logs_clear()
            elif u.path == "/v1/ai/models":
                res = ai_models(body)
            elif u.path == "/v1/ai/describe":
                res = ai_describe(body)
            elif u.path == "/v1/ai/tag":
                # 立即给一个素材打标（详情页按钮）。视觉模型要跑十几到几十秒，
                # 这里是同步返回 —— ThreadingHTTPServer 每个请求一条线程，不会挡住别的请求。
                res = ai_tag_one(body.get("path", ""))
            elif u.path == "/v1/ai/backfill":
                # 「立即补全」：立刻、不等空闲地把待打标/待转写的补完。
                # action = start | stop | status（默认 status，只读）
                res = ai_backfill(body.get("action", "status"), body.get("what", "tag"))
            elif u.path == "/v1/asr/transcribe":
                # 立即给一个音频/视频转写（详情页按钮）。同步返回，同上。
                res = asr_transcribe_one(body.get("path", ""))
            elif u.path == "/v1/asr/polish":
                # 单个素材的转写文字智能纠偏（播放面板按钮）。同步返回。
                res = asr_polish_one(body.get("path", ""))
            elif u.path == "/v1/asr/unpolish":
                # 丢掉优化版、回到只看原文。音频还要按原文重编码 chunk 0，
                # 否则优化版里的词还能从向量那条路搜到 —— 那就没还原干净。
                res = asr_unpolish_one(body.get("path", ""))
            elif u.path == "/v1/asr/polish_all":
                # 一键优化全部。action = start | stop | status（默认 status）
                res = asr_polish_all(body.get("action", "status"))
            elif u.path == "/v1/mcp/toggle":
                # 一键开启/关闭 MCP 接入。body: {"enabled":true, "ids":["dsh",...]}
                res = mcp_set(bool(body.get("enabled")), body.get("ids"))
            elif u.path == "/v1/mcp/skill":
                # 手动补/重写配套技能。body: {"ids":[...], "force":false}
                # force=false 只补缺失的，不碰已有的。
                res = mcp_skill(body.get("ids"), bool(body.get("force")))
            elif u.path == "/v1/cache/clear":
                res = clear_cache()
            elif u.path == "/v1/gc":
                res = gc_index(orphans=bool(body.get("orphans")),
                               dry_run=bool(body.get("dry_run")))
            elif u.path == "/v1/trash/add":
                res = trash_add(body.get("paths", []))
            elif u.path == "/v1/trash/restore":
                res = trash_restore(body.get("paths", []))
            elif u.path == "/v1/trash/purge":
                res = trash_purge()
            elif u.path == "/v1/trash/delete":
                res = trash_delete_permanent(body.get("paths", []))
            elif u.path == "/v1/reveal":
                res = reveal_in_folder(body.get("path", ""))
            elif u.path == "/v1/ui/history":
                res = ui_history_put(body)
            elif u.path == "/v1/open-url":
                res = open_url(body.get("url", ""))
            elif u.path == "/v1/open-folder":
                res = open_folder(body.get("path", ""))
            elif u.path == "/v1/import":
                res = import_config(body)
            else:
                self._json(404, {"error": "not found"}); return
            res["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            self._json(200, res)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._json(400, {"error": str(e), "type": type(e).__name__})


_DL_LAST = {"t": 0.0}


def _dl_progress(ev):
    """下载进度回调：1.9GB 要下好几分钟，命令行上一行一行刷会很吵，
    所以压成「最多每 2 秒一行」，显示百分比、速度、剩余时间。"""
    now = time.time()
    pct = ev.get("pct") or 0
    if now - _DL_LAST["t"] < 2.0 and pct < 100:
        return
    _DL_LAST["t"] = now
    sp = ev.get("speed") or 0
    eta = ev.get("eta") or 0
    print("  [%3d%%] %-28s %5.1f MB/s  剩余 %d 分 %02d 秒"
          % (pct, ev.get("file", "")[:28], sp / 1048576.0, eta // 60, eta % 60),
          flush=True)


def run_server(model_path, host="127.0.0.1", port=8231, allow=None):
    global ALLOW_ROOTS
    if allow:
        ALLOW_ROOTS = list(ALLOW_ROOTS) + list(allow)
    # 首次启动播种：把随包的示例素材、预置向量库、指纹库铺到用户数据目录，
    # 让用户装完打开就是一个「已经装满的库」，随便点点就能体会语义搜索 / 以图搜图 /
    # 哼唱搜歌。只在这台机器上还没任何用户数据时动手，已有索引源就一律跳过。
    try:
        _seed_result = SEED.seed_if_needed()
        if _seed_result.get("seeded"):
            print("[首次播种] 示例素材已就位：%s" % _seed_result.get("demo_dir"))
    except Exception as _e:
        print("[首次播种] 跳过（%s）" % _e)
    # 演示用的搜索记录 / 最近上传：注进**用户数据目录**的 history.json（后端注，
    # 不走前端 localStorage），并且只注一次 —— 用户清空之后不会再补回来。
    try:
        _h = ui_history_seed_if_needed()
        if _h.get("seeded"):
            print("[首次播种] 演示搜索记录 %d 条、最近上传 %d 条已写入用户数据目录"
                  % (_h.get("search", 0), _h.get("uploads", 0)))
        else:
            print("[界面记录] 跳过演示注入（%s）" % _h.get("reason"))
    except Exception as _e:
        print("[界面记录] 演示注入跳过（%s）" % _e)
    # 应用已保存的图片编码尺寸
    try:
        _st0 = load_settings()
        if _st0.get("image_max_side"):
            we.set_image_max_side(int(_st0["image_max_side"]))
            print(f"图片编码尺寸: {_st0['image_max_side']} px")
    except Exception:
        pass
    # 应用抽帧设置
    try:
        _st1 = load_settings()
        ix.FRAME_OPTS.update({
            "mode": _st1.get("video_density", "auto") or "auto",
            "custom_gap": _st1.get("video_custom_gap"),
            "max_frames": _st1.get("video_max_frames") or None,
        })
    except Exception:
        pass
    # 定位模型：打包分发版不带 1.9GB 权重（dmg 会大到没法下），首次启动现下。
    # 顺序是「环境变量 → 用户数据目录/model → 程序目录/model」：
    # 开发机上程序目录里就有 model/，所以这一步会立刻命中、不联网；
    # 用户装在 /Applications 下时 .app 是只读的，模型落在数据目录里。
    # --model 显式指定过就尊重调用方，不再自作主张。
    if model_path == we.DEFAULT_MODEL:
        try:
            _found = model_dl.find_model()
            if _found:
                model_path = _found
                print(f"模型位置: {model_path}")
            else:
                _data_dir = P.describe()["data_dir"]
                print("[模型下载] 本地没有模型，开始下载（约 1.9 GB，只需一次）")
                print("[模型下载] 下载源: %s" % model_dl.SOURCES[model_dl.DEFAULT_SOURCE]["label"])
                print("[模型下载] 干别的去就行，下完会自动继续启动。")
                model_path = model_dl.ensure_model(
                    _data_dir,
                    source=model_dl.DEFAULT_SOURCE,
                    progress=_dl_progress,
                )
                print(f"[模型下载] 完成: {model_path}")
        except Exception as _e:
            print(f"[模型下载] 失败：{_e}")
            print("           可以手动下载后放到数据目录的 model/ 下，或用 --model 指定路径。")
            raise
    _st0 = load_settings()
    if _st0.get("model_lazy", True):
        # 按需加载：这里只记路径，权重等第一次检索时再拉。
        # 8 GB 的 M1 上开机就吃 3.5 GB 会让整机发涩，而代价只是首次检索慢 1.5~3 秒。
        _STATE["model_path"] = model_path
        print("模型: 按需加载模式（首次检索时载入，约 1.5~3 秒）")
    else:
        print(f"加载模型: {model_path}")
        model, processor = we.load_model(model_path)
        _STATE.update({"model": model, "processor": processor,
                       "model_path": model_path})
    # ffmpeg 自检（确保视频/音频能力可用，且不依赖目标机器是否装了系统 ffmpeg）
    fs = ix.ffmpeg_status()
    tag = "内置 ✅" if fs["using_builtin"] else "系统 ⚠️"
    if fs["ok"]:
        print(f"ffmpeg: [{tag}] {fs['version']}")
    else:
        print(f"ffmpeg: [不可用 ❌] {fs.get('error', '未检测到')} —— 视频/音频功能将受限")
    print(f"✅ 模型就绪  应用地址: http://{host}:{port}")
    print(f"   网页版 UI:   http://{host}:{port}/")
    print(f"   API 文档:    POST /v1/search  /v1/index  /v1/embeddings  /v1/rerank")
    _pd = P.describe()
    print(f"   数据目录:    {_pd['data_dir']}"
          + ("（环境变量指定）" if _pd["custom"] else ""))
    _gc_autostart()                     # 后台自检，不阻塞启动
    _idle_autostart()                   # 闲置时逐个补 AI 标签 / 音频转写
    # 服务实例存进 _SRV：菜单栏的「停止服务器 / 启动服务器」要能把它关掉再架回来
    _SRV.update({"host": host, "port": port})
    _srv = _Server((host, port), Handler)
    _SRV["inst"] = _srv
    _SRV["thread"] = threading.current_thread()
    try:
        _srv.serve_forever()
    finally:
        if _SRV.get("inst") is _srv:
            _SRV["inst"] = None


def main():
    ap = argparse.ArgumentParser(description="WeMM 本地语义检索应用")
    ap.add_argument("--model", default=we.DEFAULT_MODEL)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8231)
    ap.add_argument("--allow", nargs="*", help="额外允许索引的根目录")
    args = ap.parse_args()
    run_server(args.model, args.host, args.port, args.allow)


if __name__ == "__main__":
    main()
