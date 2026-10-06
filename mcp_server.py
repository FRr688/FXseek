#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""FXseek MCP server —— 把本地媒体库的检索能力开放给 agent。

传输：stdio（JSON-RPC 2.0，行分隔）。
设计上这是一个**纯翻译层**：本文件不含任何检索逻辑，所有请求都转发给
已经跑着的 FXseek HTTP 服务（默认 127.0.0.1:8231）。这样做的好处是
MCP 和 Web UI 永远看到同一份数据、同一套排序，不会出现两套实现打架。

刻意只暴露**只读**工具。trash/delete、open、import、settings 这些能改变
状态或产生副作用的后端接口一律不接——agent 应该「找到文件并告诉用户」，
而不是替用户操作文件。

用法：
    ./venv/cpython-3.11/bin/python3.11 mcp_server.py            # stdio
    ./venv/cpython-3.11/bin/python3.11 mcp_server.py --selftest # 自检后退出
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_BASE = "http://127.0.0.1:8231"
BASE = os.environ.get("FXSEEK_URL", DEFAULT_BASE).rstrip("/")
TIMEOUT = float(os.environ.get("FXSEEK_TIMEOUT", "30"))
SERVER_NAME = "fxseek"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# HTTP 转发
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """后端不可达或返回了错误。

    kind 分两种，**不能混为一谈**：

      "unreachable" —— 连不上（FXseek 没开、端口不对）。告诉 agent
                       「暂时不可用」是对的，它值得重试或者提示用户启动。
      "rejected"    —— 后端**明确拒绝**了这次请求（HTTP 4xx，比如传了个
                       不存在的路径）。重试一万次也是同样的结果，必须把
                       后端那句原话带给 agent，否则它会一直重试、或者把
                       「文件不存在」说成「服务挂了」。
    """

    def __init__(self, message, kind="unreachable"):
        super().__init__(message)
        self.kind = kind


def _request(method, path, body=None, params=None, timeout=None):
    """调 FXseek 的 HTTP 接口，返回解析后的 JSON。"""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        # 4xx = 后端明确拒绝。把它的原话（形如 {"error": "文件不存在或不可访问"}）
        # 原样带出去，别包成「暂时不可用」——那会骗 agent 一直重试。
        if 400 <= e.code < 500:
            msg = ""
            try:
                msg = (json.loads(detail) or {}).get("error") or ""
            except Exception:
                pass
            raise BackendError(
                msg or ("FXseek 拒绝了这次请求（HTTP %s）：%s" % (e.code, detail or e.reason)),
                kind="rejected",
            )
        raise BackendError("FXseek 返回 HTTP %s：%s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        raise BackendError(
            "连不上 FXseek（%s）。请确认 FXseek 正在运行，"
            "或在设置页把 MCP 服务打开。" % (e.reason,)
        )
    except Exception as e:  # noqa: BLE001
        raise BackendError("请求 FXseek 失败：%s" % (e,))
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise BackendError("FXseek 返回的不是合法 JSON：%s" % (e,))


# ---------------------------------------------------------------------------
# 结果精简
#
# /v1/search 的响应是给前端用的，带了一堆渲染才需要的字段（dim_text、
# size_text、img_format、img_mode、chunk_idx…）。直接塞给 LLM 是白烧 token，
# 而且噪声会干扰它对结果的判断。这里统一裁成 agent 真正需要的那些。
# ---------------------------------------------------------------------------


def _fmt_size(n):
    if not isinstance(n, (int, float)):
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0
    return None


def _slim_meta(meta):
    """把 meta 裁成对 agent 有用的部分，丢掉纯渲染字段。"""
    if not isinstance(meta, dict):
        return {}
    out = {}
    # 文本类：直接给出可用内容
    for k in ("text", "ai_description"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    tags = meta.get("ai_tags")
    if isinstance(tags, list) and tags:
        out["ai_tags"] = [str(t) for t in tags if str(t).strip()]
    # 媒体类：给出人类可读的尺寸/时长，而不是给一堆原始数字
    if meta.get("dim_text"):
        out["dimensions"] = meta["dim_text"]
    dur = meta.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        out["duration_sec"] = round(float(dur), 1)
    if isinstance(meta.get("timestamp"), (int, float)) and meta["timestamp"] > 0:
        out["frame_at_sec"] = round(float(meta["timestamp"]), 1)
    # 文档：字数
    for k in ("chars", "text_len"):
        if isinstance(meta.get(k), (int, float)) and meta[k]:
            out["char_count"] = int(meta[k])
            break
    return out


def _slim_result(r):
    """一条检索结果 → agent 视角的精简表示。"""
    if not isinstance(r, dict):
        return r
    path = r.get("path") or ""
    raw_score = r.get("score")
    # 分数钳到 1.0：语义分是余弦（≤1），而加成项（转写文本命中）是**加**上去的，
    # 所以原始值可能出现 1.07 这种。技能正文里告诉 agent 的是「0.9 以上基本就是要
    # 找的」，报一个大于 1 的相似度会让它摸不着头脑。
    if isinstance(raw_score, (int, float)):
        raw_score = max(0.0, min(1.0, round(float(raw_score), 4)))
    out = {
        "name": r.get("name") or os.path.basename(path),
        "path": path,
        "kind": r.get("kind"),
        "score": raw_score,
    }
    size = r.get("size")
    if isinstance(size, (int, float)):
        out["size"] = _fmt_size(size)
    meta = _slim_meta(r.get("meta"))
    if meta:
        out["info"] = meta
    ts = r.get("matched_timestamp")
    if isinstance(ts, (int, float)):
        # 视频命中的是某一帧，这个信息对「截取画面」很有用，单独提出来
        out["matched_frame_sec"] = round(float(ts), 1)
    if r.get("exists") is False:
        out["exists"] = False
    return out


def _text(payload):
    """构造 MCP 的 text content。"""
    s = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=1
    )
    return {"content": [{"type": "text", "text": s}]}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def tool_search_media(args):
    """自然语言检索媒体库。主力工具。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return _text("请给出 query（要搜什么）。")
    body = {"query": query}
    if args.get("kind"):
        body["kind"] = args["kind"]
    if args.get("top_k"):
        try:
            body["top_k"] = max(1, min(50, int(args["top_k"])))
        except (TypeError, ValueError):
            pass
    # tier 默认钉死 deep，**不是** auto。
    # auto 的行为取决于「这个查询的向量有没有在缓存里」：
    #   缓存未命中 → 落到 L2 深度检索，分数偏高（实测 0.7737 / 0.7948）
    #   缓存命中   → 走 L1 快路径，分数偏低（实测 0.6349 / 0.5315）
    # 于是同一个问题，第一次问和第二次问可能给出不同答案，还会在
    # TOP_FLOOR=0.50 这条线上左右横跳（有时返回、有时「没找到」）。
    # 对 agent 来说「答案稳定」比省 0.3 秒重要得多（deep 0.33~0.42s，
    # auto 0.10~0.30s）。调用方显式传了 tier 就听调用方的。
    body["tier"] = args.get("tier") or "deep"
    # 关掉文件名/路径加成，只按内容相似度排序。
    # 文件名可能是随手起的、甚至写错的（「最终版」「稿2」），拿它当证据会把
    # agent 带到沟里；而描述/标签/转写文本是真实内容。用户明确要求：
    # 「agent 查找我更看重文件相似度，而不是文件名」。
    body["lexical"] = False
    if args.get("source"):
        body["source"] = args["source"]

    d = _request("POST", "/v1/search", body=body)
    results = [_slim_result(r) for r in (d.get("results") or [])]

    # ---- 相关度截断 ----
    # /v1/search 把 top_k 个结果全给你，不管多不相关。Web UI 那边有一层
    # smartFilter 按「匹配门槛」过滤，MCP 这边也得有——否则 agent 拿到
    # 一堆 0.38 分的噪声，会当成「真的找到了」然后拿去回答用户。
    # 规则：低于绝对下限的直接丢；剩下的按最高分的比例截。
    # 至少保留 1 条，避免把唯一的结果也误杀。
    kept, dropped = _filter_relevant(results)
    for r in kept:
        r.pop("_score_num", None)

    if not kept:
        return _text(
            {
                "query": query,
                "count": 0,
                "note": "库里没有找到相关素材（%d 条候选的相关度都太低，已丢弃）。"
                        "换一种说法可能有用；也可以用 list_sources 确认"
                        "要搜的目录是否已经被索引。" % dropped,
            }
        )
    payload = {
        "query": query,
        "count": len(kept),
        "tier": d.get("tier"),
        "elapsed_ms": d.get("elapsed_ms"),
        "results": kept,
    }
    if dropped:
        # 告诉 agent「我替你藏了几条」，它就不会以为库里只有这些
        payload["filtered_out"] = (
            "%d 条相关度过低的结果已隐藏；如果确实需要可以缩小范围或换关键词"
            % dropped
        )
    return _text(payload)


def tool_find_by_name(args):
    """按文件名找素材 —— 和 search_media 刻意分开的一条路。

    search_media 只按内容相似度排（文件名完全不参与打分，见那边的注释）；
    这个工具反过来，**只按名字**。两者分开的原因：把文件名混进内容排序会污染
    排序结果，但用户明确报出一个文件名时，「按名字找」就是他要的。
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return _text("请给出 name（要查找的文件名，可以是其中一段）。")
    body = {"name": name}
    if args.get("kind"):
        body["kind"] = args["kind"]
    if args.get("limit"):
        try:
            body["limit"] = max(1, min(100, int(args["limit"])))
        except (TypeError, ValueError):
            pass
    if args.get("source"):
        body["source"] = args["source"]

    d = _request("POST", "/v1/find", body=body)
    out = []
    for r in d.get("results") or []:
        item = {
            "name": r.get("name"),
            "path": r.get("path"),
            "kind": r.get("kind"),
        }
        if r.get("unindexed"):
            item["unindexed"] = True
        sz = r.get("size")
        if isinstance(sz, (int, float)) and sz:
            item["size"] = _fmt_size(sz)
        # 分词兜底命中的要标出来：文件名里并没有用户要的那一串，
        # 让 agent 报给用户时把完整名字念出来确认，别当成精确命中。
        if r.get("approx"):
            item["approx"] = True
            item["match_ratio"] = r.get("ratio")
        out.append(item)

    if not out:
        return _text({
            "name": name,
            "count": 0,
            "note": "没有任何文件名包含「%s」。注意这里**只比文件名**，不比内容——"
                    "如果用户描述的是内容（「有气球的画面」），应该改用 search_media。" % name,
        })
    payload = {
        "name": name,
        "count": len(out),
        "total": d.get("total"),
        "results": out,
        "note": "这些是按**文件名**匹配的，不代表内容相关；要按内容找请用 search_media。",
    }
    if d.get("total", 0) > len(out):
        payload["truncated"] = "共 %d 个匹配，只显示了前 %d 个。" % (d["total"], len(out))
    if d.get("approx_note"):
        payload["approx_note"] = d["approx_note"]
    return _text(payload)


# MCP 的匹配门槛。
#
# 比 Web UI 的 balanced 档（0.40/0.60）略松一点，但**多一道「最高分够不够格」的
# 检查**。原因是这里服务的是 agent 而不是人眼：人看到一屏 0.43 分的灰色结果会
# 自己判断「这不对」，而 agent 会照着念给用户听，说成「找到了」。
#
# 「最高分不够格」这一条是必须的。实测「太空」这种库里根本没有的主题，
# 向量检索会返回一**簇** 0.42~0.43 的结果——它们彼此分数很接近，光靠
# 「相对最高分比例」是切不掉的（比例全是 1.0）。必须用绝对分数线拦。
REL_FLOOR = 0.46      # 单条结果的绝对下限
REL_RATIO = 0.62      # 相对最高分的比例
TOP_FLOOR = 0.50      # 最高分低于这个数 → 判定「没找到」，整批不返回
#
# 0.46 这个数是实测调出来的。原先写 0.42，结果「日落的风景」会带上
# 0.4518 的 个人简历.doc 和 0.4429 的 比较.txt —— 明显是噪声。提到 0.46 后
# 这两条被滤掉，而 0.5211 的女孩.mp4、0.5315 的小狗在操场奔跑.png 这些
# 真结果一条不少。


def _filter_relevant(results):
    """按相关度截断，返回 (保留的, 丢掉的数量)。"""
    if not results:
        return [], 0
    scored = []
    for r in results:
        try:
            s = float(r.get("score") or 0.0)
        except (TypeError, ValueError):
            s = 0.0
        scored.append((s, r))
    top = max(s for s, _ in scored)
    if top < TOP_FLOOR:
        # 整批都不可信 —— 与其给 agent 一堆噪声让它编答案，不如老实说没有。
        return [], len(scored)
    thr = max(REL_FLOOR, top * REL_RATIO)
    kept = [r for s, r in scored if s >= thr]
    if not kept:                      # 兜底：至少给最高分那条
        kept = [max(scored, key=lambda x: x[0])[1]]
    return kept, len(scored) - len(kept)


def tool_search_by_image(args):
    """以图搜图：给一张本地图片，找库里长得像的。"""
    path = str(args.get("image_path") or "").strip()
    if not path:
        return _text("请给出 image_path（本地图片的绝对路径）。")
    if not os.path.isfile(path):
        return _text("找不到这个文件：%s" % path)
    d = _request(
        "POST",
        "/v1/search-image",
        body={"path": path, "top_k": int(args.get("top_k") or 12)},
    )
    raw = d.get("results") or d.get("items") or []
    results = [_slim_result(r) for r in raw]
    return _text(
        {"query_image": path, "count": len(results), "results": results}
    )


def tool_search_by_audio(args):
    """以音搜素材：给一段音频，用指纹找库里同一首歌/同一段声音。"""
    path = str(args.get("audio_path") or "").strip()
    if not path:
        return _text("请给出 audio_path（本地音频或视频的绝对路径）。")
    if not os.path.isfile(path):
        return _text("找不到这个文件：%s" % path)
    d = _request(
        "POST",
        "/v1/audio-search",
        body={"path": path, "top_k": int(args.get("top_k") or 10)},
    )
    raw = d.get("results") or d.get("items") or []
    results = [_slim_result(r) for r in raw]
    if not results:
        return _text(
            "没有匹配到同一段音频。指纹匹配只认**同一段录音**——"
            "清唱、哼唱、翻唱因为音高和时值都变了，原理上匹配不到。"
        )
    return _text({"query_audio": path, "count": len(results), "results": results})


def tool_get_media_info(args):
    """按路径取单个素材的详情（含 AI 描述/标签/转写文本）。"""
    path = str(args.get("path") or "").strip()
    if not path:
        return _text("请给出 path（素材的绝对路径）。")
    d = _request("GET", "/v1/detail", params={"path": path})
    meta = d.get("meta") or {}
    out = {
        "name": d.get("name"),
        "path": d.get("path"),
        "dir": d.get("dir"),
        "kind": d.get("kind_name") or d.get("kind"),
        "indexed": d.get("indexed"),
        "size": _fmt_size(meta.get("size")),
        "info": _slim_meta(meta),
    }
    times = d.get("times")
    if isinstance(times, list) and times:
        # 视频的关键帧时间点——想看具体某一秒时有用
        out["frame_times_sec"] = [round(float(t), 1) for t in times[:40]]
    return _text(out)


def tool_list_sources(args):
    """列出已索引的目录，让 agent 知道库覆盖了哪些范围。"""
    d = _request("GET", "/v1/sources")
    srcs = []
    for s in d.get("sources") or []:
        srcs.append(
            {
                "path": s.get("path"),
                "name": s.get("name"),
                "files": s.get("files"),
                "vectors": s.get("vectors"),
                "exists": s.get("exists"),
            }
        )
    return _text(
        {
            "count": len(srcs),
            "sources": srcs,
            "note": "只能搜到这些目录下的素材。用户问的文件如果不在这些目录里，"
                    "说明还没被索引进库。",
        }
    )


def tool_list_recent(args):
    """列出最近的素材（相当于「全部素材」浏览）。"""
    limit = int(args.get("limit") or 20)
    kind = args.get("kind")
    body = {"limit": max(1, min(100, limit))}
    if kind:
        body["kind"] = kind
    d = _request("POST", "/v1/browse", body=body)
    raw = d.get("items") or d.get("results") or []
    results = [_slim_result(r) for r in raw]
    return _text({"count": len(results), "results": results})


def tool_library_stats(args):
    """库的整体统计——agent 判断「这个库值不值得查」用。"""
    stats = _request("GET", "/v1/stats")
    return _text(
        {
            "files": stats.get("files"),
            "vectors": stats.get("vectors"),
            "by_kind": stats.get("by_kind"),
            "ai_tagged": stats.get("ai_tagged"),
        }
    )


# ---------------------------------------------------------------------------
# 工具目录
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_media",
        "description": (
            "用自然语言在当前媒体库里找文件，支持**画面内容**而不只是文件名。"
            "比如「有气球的画面」「一只狗在跑」「夕阳风景」「手心里的汗珠那首歌」。"
            "这是最常用的工具——用户说「找一下…」「有没有…」时优先用它。"
            "返回按相关度排序的文件列表，含绝对路径、类型、匹配度。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜的内容，自然语言即可。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["image", "video", "audio", "document"],
                    "description": "只看某一类素材。不填则搜全部。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "最多返回几条，默认 10，上限 50。",
                },
                "tier": {
                    "type": "string",
                    "enum": ["auto", "fast", "vector", "deep"],
                    "description": (
                        "检索深度。auto（默认）会先试文件名匹配再决定要不要跑模型；"
                        "fast 只匹配文件名（最快，几十毫秒）；"
                        "deep 强制跑深度语义（最准，约 1 秒）。"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_by_image",
        "description": (
            "以图搜图：给一张本地图片，找出库里画面相似的素材。"
            "当用户说「找和这张图差不多的」并提供了一个图片路径时用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "本地图片的绝对路径。"},
                "top_k": {"type": "integer", "description": "最多返回几条，默认 12。"},
            },
            "required": ["image_path"],
        },
    },
    {
        "name": "search_by_audio",
        "description": (
            "以音搜素材：给一段音频/视频文件，用音频指纹在库里找同一段声音"
            "（比如同一首歌的不同文件）。注意：只认同一段录音，"
            "清唱、哼唱、翻唱匹配不到。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audio_path": {"type": "string", "description": "本地音频或视频的绝对路径。"},
                "top_k": {"type": "integer", "description": "最多返回几条，默认 10。"},
            },
            "required": ["audio_path"],
        },
    },
    {
        "name": "get_media_info",
        "description": (
            "拿到某个素材的详细信息：尺寸/时长、AI 描述、AI 标签、"
            "音频转写文本、视频关键帧时间点。"
            "在 search_media 拿到路径之后，想进一步了解某个文件时用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "素材的绝对路径。"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "find_by_name",
        "description": (
            "按**文件名**查找素材（只比名字，不比内容）。"
            "★ 只在用户明确说出一个文件名/文件名片段时用它，比如「帮我找那个叫季度报告的文件」"
            "「文件名里有 2024 的」。「找一下有气球的画面」这种描述内容的请求**不要用这个**，"
            "要用 search_media。"
            "未建索引的目录也会被扫到，结果里带 unindexed 标记。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "文件名或其中一段（不区分大小写）。例如「季度报告」「.wps」。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["image", "video", "audio", "document"],
                    "description": "只看某一类素材。不填则全部。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几条，默认 30，上限 100。",
                },
                "source": {
                    "type": "string",
                    "description": "限定在某个索引源目录下查找。",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_sources",
        "description": (
            "列出媒体库已经索引了哪些目录。"
            "用它来判断：用户要找的文件是不是根本没在库里。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_recent",
        "description": (
            "列出媒体库里最近的素材。当用户说「我刚上传的」「最近的那些」"
            "这种没有具体语义关键词的请求时用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回几条，默认 20。"},
                "kind": {
                    "type": "string",
                    "enum": ["image", "video", "audio", "document"],
                    "description": "只看某一类。",
                },
            },
        },
    },
    {
        "name": "library_stats",
        "description": "媒体库的整体规模统计（多少文件、多少向量、各类多少）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "search_media": tool_search_media,
    "find_by_name": tool_find_by_name,
    "search_by_image": tool_search_by_image,
    "search_by_audio": tool_search_by_audio,
    "get_media_info": tool_get_media_info,
    "list_sources": tool_list_sources,
    "list_recent": tool_list_recent,
    "library_stats": tool_library_stats,
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP
# ---------------------------------------------------------------------------


def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


def handle(msg):
    """处理一条 JSON-RPC 消息。返回响应 dict，或 None（通知不需要响应）。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request")
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}

    # --- 生命周期 ---
    if method == "initialize":
        return _ok(
            id_,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "FXseek 本地媒体库。它能用自然语言按**画面内容**（不只是文件名）"
                    "找到本机的图片、视频、音频、文档，还支持以图搜图和以音搜素材。\n"
                    "使用建议：\n"
                    "1. 用户问「有没有/找一下/帮我找」某类素材时，先调 search_media。\n"
                    "2. 只有搜到具体文件之后才调 get_media_info 看细节，不要一上来就查。\n"
                    "3. 搜不到时先用 list_sources 确认目标目录在不在库里，再换说法重试；"
                    "不要反复用同一种措辞硬试。\n"
                    "4. 视频结果里的 matched_frame_sec 是命中的秒数，"
                    "如果需要画面可以用 ffmpeg 按这个时间点截帧。\n"
                    "5. 这些工具都是只读的——你负责找到文件并把绝对路径告诉用户，"
                    "不要擅自删除或移动文件。"
                ),
            },
        )
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(id_, {})

    # --- 工具 ---
    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return _ok(
                id_,
                {
                    "content": [{"type": "text", "text": "没有这个工具：%s" % name}],
                    "isError": True,
                },
            )
        try:
            return _ok(id_, fn(args))
        except BackendError as e:
            # 「连不上」是**可预期**的情况（FXseek 没开），回一句人话提示去启动；
            # 「被拒绝」则要原样转达后端的话——重试没用，别让 agent 白等。
            txt = str(e) if e.kind == "rejected" else "媒体库暂时不可用：%s" % e
            return _ok(
                id_,
                {
                    "content": [{"type": "text", "text": txt}],
                    "isError": True,
                },
            )
        except Exception as e:  # noqa: BLE001
            return _ok(
                id_,
                {
                    "content": [
                        {"type": "text", "text": "执行 %s 出错：%s" % (name, e)}
                    ],
                    "isError": True,
                },
            )

    # --- 其他（resources/prompts 等）—— 明确回「不支持」，别装作成功 ---
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return _ok(id_, {key: []})
    if method in ("resources/templates/list",):
        return _ok(id_, {"resourceTemplates": []})

    if id_ is None:
        return None  # 未知通知，忽略
    return _err(id_, -32601, "Method not found: %s" % method)


def serve_stdio():
    """stdio 主循环：一行一条 JSON-RPC。"""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, -32700, "Parse error: %s" % e)
        else:
            try:
                resp = handle(msg)
            except Exception as e:  # noqa: BLE001
                resp = _err(msg.get("id") if isinstance(msg, dict) else None,
                            -32603, "Internal error: %s" % e)
        if resp is None:
            continue
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()


def selftest():
    """自检：不依赖 FXseek 是否在跑，逐项报告。"""
    print("FXseek MCP server 自检")
    print("  后端地址   : %s" % BASE)
    print("  工具数量   : %d" % len(TOOLS))
    for t in TOOLS:
        print("    - %s" % t["name"])
    ok = True
    try:
        stats = _request("GET", "/v1/stats", timeout=5)
        print("  后端连通   : ✓  files=%s vectors=%s"
              % (stats.get("files"), stats.get("vectors")))
    except BackendError as e:
        ok = False
        print("  后端连通   : ✗  %s" % e)
    # 协议自检
    r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r["result"]["serverInfo"]["name"] == SERVER_NAME
    r = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(r["result"]["tools"]) == len(TOOLS)
    r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "nope", "arguments": {}}})
    assert r["result"]["isError"] is True
    print("  协议自检   : ✓")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    serve_stdio()


if __name__ == "__main__":
    main()
