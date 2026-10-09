#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
AI 素材描述/标签生成 —— 对接 OpenAI 兼容 API（oMLX 本地 / OpenAI 官方 / 任意兼容服务）

用途：给图片、视频帧生成文字描述与标签，写入索引。
      这样检索「动物」时能匹配到描述里的「小狗」，解决上位词泛化问题。

依赖：仅标准库（urllib），无第三方 SDK。
"""
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from net_util import urlopen_smart, api_url


class AIError(Exception):
    """AI 调用错误（含可读原因）。"""


# ---------------------------------------------------------------------------
# 官方服务商免 Key 模型目录
# ---------------------------------------------------------------------------
# 部分官方服务商在无 Key（或 Key 无效）时会拒绝 /models 或 401，但模型名本身是
# 公开固定的。这里内置一份「官方目录」：请求失败时回落显示，让用户不填 Key
# 也能看到该服务商有哪些模型可选（再挑一个去申请 Key）。
# 只列「当前仍在提供」的主流型号；kind: vision=能看图 / asr=语音转写 / chat=纯文本。
OFFICIAL_CATALOGS = {
    "api.openai.com": {
        "gpt-4o-mini": "vision", "gpt-4o": "vision", "gpt-4.1": "vision",
        "gpt-4.1-mini": "vision", "gpt-4.1-nano": "vision",
        "chatgpt-4o-latest": "vision", "o3": "vision", "o4-mini": "vision",
        "whisper-1": "asr", "gpt-4o-transcribe": "asr",
        "gpt-4o-mini-transcribe": "asr",
    },
    "generativelanguage.googleapis.com": {
        # Gemini 系全部原生多模态
        "gemini-2.5-pro": "vision", "gemini-2.5-flash": "vision",
        "gemini-2.5-flash-lite": "vision", "gemini-2.0-flash": "vision",
        "gemini-2.0-flash-lite": "vision",
    },
    "dashscope.aliyuncs.com": {
        # 通义千问 VL 系列（能看图）+ ASR 系列
        "qwen-vl-max-latest": "vision", "qwen-vl-plus-latest": "vision",
        "qwen2.5-vl-72b-instruct": "vision", "qwen2.5-vl-32b-instruct": "vision",
        "qwen2.5-vl-7b-instruct": "vision",
        "qvq-max": "vision", "qvq-plus": "vision",
        "qwen3-asr-flash": "asr", "paraformer-v2": "asr",
        "sensevoice-v1": "asr",
    },
    "open.bigmodel.cn": {
        # ★ 智谱的 /models 只列纯文本型号，视觉线全靠这份目录兜住。
        # 实测：glm-4v-flash 喂一张纯色图能正确答出颜色；而 glm-4.5 / glm-5.3
        # 全部拒收图片（HTTP 400 "messages.content.type 参数非法，取值范围 ['text']"）。
        "glm-4v-flash": "vision", "glm-4v-plus": "vision", "glm-4v": "vision",
        "glm-4.5v": "vision", "glm-4.6v": "vision",
        "glm-asr": "asr",
        # glm-4v-air 与 glm-5v 实测已下架（HTTP 400 模型不存在），不要再列。
    },
    "api.moonshot.cn": {
        # Kimi 视觉型号
        "moonshot-v1-8k-vision-preview": "vision",
        "moonshot-v1-32k-vision-preview": "vision",
        "moonshot-v1-128k-vision-preview": "vision",
        "kimi-latest": "vision", "moonshot-v1-8k": "chat",
        "moonshot-v1-32k": "chat", "moonshot-v1-128k": "chat",
    },
    "api.siliconflow.cn": {
        # 硅基流动聚合的开源视觉/语音模型
        "Qwen/Qwen2.5-VL-72B-Instruct": "vision",
        "Qwen/Qwen2.5-VL-32B-Instruct": "vision",
        "Qwen/Qwen2.5-VL-7B-Instruct": "vision",
        "deepseek-ai/deepseek-vl2": "vision",
        "OpenGVLab/InternVL2_5-26B": "vision",
        "FunAudioLLM/SenseVoiceSmall": "asr",
        "FunAudioLLM/SenseVoiceLarge": "asr",
    },
    "api.x.ai": {
        "grok-2-vision-1212": "vision", "grok-2-vision": "vision",
        "grok-vision-beta": "vision",
        "grok-4": "vision", "grok-3": "chat",
    },
    "api.groq.com": {
        "whisper-large-v3": "asr", "whisper-large-v3-turbo": "asr",
        "distil-whisper-large-v3-en": "asr",
        "llama-3.2-11b-vision-preview": "vision",
        "llama-3.2-90b-vision-preview": "vision",
    },
}


def _catalog_for(base_url: str) -> dict:
    """按 base_url 的 host 匹配官方目录；不匹配返回 {}。"""
    try:
        host = urllib.parse.urlparse(base_url if "://" in base_url
                                     else "https://" + base_url).netloc.lower()
    except Exception:
        return {}
    host = host.split("@")[-1].split(":")[0]
    if host in OFFICIAL_CATALOGS:
        return OFFICIAL_CATALOGS[host]
    # 允许一级子域泛匹配（如 xxx.dashscope.aliyuncs.com）
    for h, cat in OFFICIAL_CATALOGS.items():
        if host.endswith("." + h) or host == h:
            return cat
    return {}


def _catalog_entries(cat: dict, kind: str = "") -> list:
    """目录 → [{id, owned_by}]；kind 为空返回全部。"""
    out = []
    for mid, k in cat.items():
        if kind and k != kind:
            continue
        out.append({"id": mid, "owned_by": "official"})
    return out


# 纯泛词：对语义检索零信息量（索引期还会被 tag_center 居中掉），过滤以免占标签槽位。
GENERIC_TAGS = {
    "图片", "照片", "视频", "图像", "画面", "场景", "物体", "内容", "人物",
    "素材", "无", "图", "图里", "图中",
}


def _post_json(url: str, payload: dict, api_key: str = "", timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen_smart(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise AIError(f"HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise AIError(f"无法连接 {url}（{e.reason}）")
    except json.JSONDecodeError:
        raise AIError("返回内容不是合法 JSON")
    except Exception as e:
        raise AIError(str(e))


def _get_json(url: str, api_key: str = "", timeout: int = 15) -> dict:
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlopen_smart(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise AIError(str(e))


# ★ 这两组关键词只是「粗筛」—— 它们永远不完整，绝不能当「拒绝服务」的理由。
# 现实里「名字里看不出能力」的服务商越来越多：智谱的 /models 干脆只列纯文本型号
# （实测 glm-4.5 / glm-5.3 全部拒收图片，报 "messages.content.type 参数非法"），
# 能看图的 glm-4v-flash 根本不在返回里。所以 list_models 必须做并集 + 兜底。
_VISION_NEG = (
    "whisper", "transcribe", "tts", "asr", "speech", "paraformer", "sensevoice",
    "funasr", "embedding", "rerank", "moderation", "audio", "realtime",
    "cogview", "cogvideo", "dall-e", "stable-diffusion", "flux", "kolors",
    "voice", "image-generation",
)
_VISION_HINT = (
    "vision", "vl", "vlm", "multimodal", "omni", "qvq", "internvl", "minicpm-v",
    "llava", "idefics", "pixtral", "cogvlm", "moondream", "paligemma", "mllama",
    "florence", "molmo", "yi-vision", "step-1v", "glm-4v", "glm-4.5v",
)
# 「数字（可带小数点）+ v」：glm-4v / glm-4.5v / glm-4.6v / step-1v / qwen-1v。
# ★ 别写死 "4v" —— 智谱已经出到 glm-4.6v，写死就只能匹配 4 那一代。
_NUMV_RE = re.compile(r"\d+(?:\.\d+)?v(?:\b|[-._])")


def is_vision_model(mid: str) -> bool:
    """粗筛「能看图」的模型名。本地/未知模型宁可多留，由用户自己判断。"""
    s = (mid or "").lower()
    if not s:
        return False
    if any(k in s for k in _VISION_NEG):
        return False                       # 明确的语音/文本/生图类，剔除
    if any(k in s for k in _VISION_HINT) or _NUMV_RE.search(s):
        return True                        # 名字里明示视觉
    if s.startswith("gemini") or s.startswith("gpt-4o") or s.startswith("chatgpt-4o"):
        return True                        # Gemini / GPT-4o 系原生多模态
    if s.startswith(("o1", "o3", "o4")):
        return True                        # OpenAI o 系列带视觉输入
    if s.startswith("grok") and ("vision" in s or s in ("grok-4",)):
        return True
    return False


_ASR_HINT = (
    "whisper", "asr", "transcribe", "transcription", "speech-to-text",
    "sensevoice", "paraformer", "funasr", "voxtral", "parakeet", "canary",
    "audio-preview", "-stt", "stt-",
)
_ASR_NEG = ("tts", "text-to-speech", "speech-synthesis", "voice-clone", "cosyvoice")


def is_asr_model(mid: str) -> bool:
    """粗筛「语音转写」模型名。TTS（合成）不算转写。"""
    s = (mid or "").lower()
    if not s:
        return False
    if any(k in s for k in _ASR_NEG) and "asr" not in s:
        return False                       # 语音合成，不是转写
    if any(k in s for k in _ASR_HINT):
        return True
    return False


def _list_models_impl(base_url: str, api_key: str = "", kind: str = ""):
    """返回 (模型列表, 提示文案)。

    提示文案非空时前端要原样显示 —— 它解释的是「为什么这个列表长这样」。
    """
    base = base_url.rstrip("/")
    cat = _catalog_for(base)
    note = ""
    try:
        d = _get_json(api_url(base, "/models"), api_key)
        items = d.get("data") or d.get("models") or []
        live = []
        for m in items:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name") or ""
                if mid:
                    live.append({"id": mid, "owned_by": m.get("owned_by", "")})
    except AIError:
        # 网络/鉴权失败 → 官方目录兜底（仅官方域名有目录）
        if not cat:
            raise
        live = []
        note = "该地址连不上或 Key 无效，下面是内置官方目录。"

    # ★ 在线列表与官方目录取「并集」，而不是「在线失败才用目录」。
    # 智谱就是活例子：它的 /models 只列纯文本型号（实测 glm-4.5 / glm-5.3 全部拒收
    # 图片），能看图的 glm-4v-flash 压根不在返回里 —— 只信在线列表，视觉型号永远筛不出来。
    have = {m["id"] for m in live}
    out = list(live)
    for mid in (cat or {}):
        if mid not in have:
            out.append({"id": mid, "owned_by": "official-catalog"})

    if kind in ("vision", "asr") and not cat:
        # ★ 自定义 / 本地地址**不参与过滤**：没有内置目录，我们就没有任何依据
        # 判断这家怎么给模型命名 —— 猜错一次，用户要的型号就从下拉里消失了。
        # 全部列出，让他自己挑（右侧「测试连接」能立刻验证选中的那个行不行）。
        note = ("自定义 / 本地地址不参与过滤，已列出全部 %d 个模型 —— "
                "我们无从判断这家怎么命名模型。挑一个后用「测试连接」确认。"
                % len(out))
    elif kind in ("vision", "asr"):
        match = is_vision_model if kind == "vision" else is_asr_model
        zh = "看图" if kind == "vision" else "语音转写"
        keep = [m for m in out
                if match(m["id"]) or (cat or {}).get(m["id"]) == kind]
        if not live:
            # 只有内置目录（地址连不上 / Key 无效）：目录说什么就是什么，不猜。
            out = keep
        elif keep and any(match(m["id"]) for m in live):
            # 在线列表自己标了能力 → 这份过滤可信，按它裁。
            out = keep
        elif keep:
            # 在线列表里一个名字都没标这类能力 → 这家不靠名字标能力。
            # 只留命中的会把当前旗舰（智谱 glm-5.3 之类）藏起来，那正是用户的抱怨。
            # 把确定命中的排最前，其余一并列出并说明，让人自己挑。
            # 「其余」要排掉明确属于另一类的（列 ASR 时不该混进一堆视觉型号）。
            other = is_asr_model if kind == "vision" else is_vision_model
            out = keep + [m for m in out
                          if m not in keep and not other(m["id"])]
            note = ("该服务商的模型名里没有%s标记，只认出 %d 个（已排在最前），"
                    "其余一并列出供你确认。" % (zh, len(keep)))
        elif out:
            # 一个都没认出来 ≠ 该服务商没有这类模型，只说「没有发现」会把人挡在门外。
            note = ("没能从模型名里认出%s型号，已列出全部 %d 个 —— 不少服务商"
                    "（如智谱 GLM-4.5 起）把能力并进了基础型号，名字里不留标记，"
                    "请自行挑选后用「测试连接」确认。" % (zh, len(out)))

    # 去重（在线列表 + 目录可能重叠）
    seen, uniq = set(), []
    for m in out:
        if m["id"] not in seen:
            seen.add(m["id"])
            uniq.append(m)
    return uniq, note


def list_models(base_url: str, api_key: str = "", kind: str = "") -> list:
    """列出服务端可用模型（用于设置页下拉）。

    kind: ""=不过滤 / "vision"=只留能看图的 / "asr"=只留语音转写的。
    在线列表与内置官方目录取并集；目录命中时用户不填 Key 也能看到有哪些模型可选。
    """
    return _list_models_impl(base_url, api_key, kind)[0]


def list_models_ex(base_url: str, api_key: str = "", kind: str = "") -> dict:
    """同 list_models，但多带一个 note（说明这份列表为什么长这样）。"""
    models, note = _list_models_impl(base_url, api_key, kind)
    return {"models": models, "note": note}


def test_connection(base_url: str, api_key: str = "", model: str = "") -> dict:
    """测试连通性：返回可用模型列表和一个简单的补全测试。"""
    result = {"ok": False, "models": [], "message": ""}
    try:
        result["models"] = list_models(base_url, api_key)
        result["ok"] = True
        result["message"] = f"连接成功，发现 {len(result['models'])} 个模型"
    except AIError as e:
        result["message"] = f"连接失败：{e}"
        return result

    if model:
        try:
            txt = chat_completion(base_url, api_key, model,
                                  "请只回复两个字：正常", max_tokens=16, timeout=30)
            result["message"] += f"；模型「{model}」响应：{txt[:40]}"
        except AIError as e:
            result["ok"] = False
            result["message"] += f"；但模型调用失败：{e}"
    return result


def _img_to_data_url(img, fmt: str = "JPEG", quality: int = 85, max_side: int = 896) -> str:
    """PIL Image → data URL（先缩放到合适尺寸，省 token）。"""
    from PIL import Image
    im = img.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    im.save(buf, fmt, quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/{fmt.lower()};base64,{b64}"


def chat_completion(base_url: str, api_key: str, model: str, prompt: str,
                    image=None, max_tokens: int = 220, timeout: int = 120) -> str:
    """调用 chat/completions；image 为 PIL.Image 时走多模态消息。"""
    base = base_url.rstrip("/")
    url = api_url(base, "/chat/completions")

    if image is not None:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _img_to_data_url(image)}},
        ]
    else:
        content = prompt

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    d = _post_json(url, payload, api_key, timeout)
    try:
        msg = d["choices"][0]["message"]
        txt = msg.get("content") or ""
        if isinstance(txt, list):      # 部分服务返回分段内容
            txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
        return (txt or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError(f"响应格式异常：{str(d)[:200]}")


def _clean_tags(text: str, limit: int = 12) -> list:
    """从模型输出里抽取标签。

    策略：优先取「关键词/关键字/标签」字段里逗号分隔的短词；没有该字段时，
          退化为从整段文本按标点切短词。剥掉字段前缀，只保留短词（≤10 字），
          过滤完整句子和纯泛词（这些词对检索零信息量，还占标签槽位）。
    """
    if not text:
        return []
    t = text.strip()
    # 1) 优先取「关键词」那一行：模型按格式输出时，标签都在这一行，能避开描述句里的标点噪声
    m = re.search(r"(?:关键词|关键字|标签)\s*[：:]\s*([^\n\r]*)", t)
    field = m.group(1).strip() if m else t
    # 2) 剥掉其余字段前缀，统一成竖线分隔（方便下一步切分）
    field = re.sub(
        r"(图片主要内容|主要内容|关键物体|关键词|关键字|标签|场景|类别|描述|画面|图中|图片|动作|颜色|风格|主体)\s*[：:]\s*",
        "|", field)
    field = re.sub(r"^[^\u4e00-\u9fa5A-Za-z0-9]+", "", field)
    parts = re.split(r"[，,、；;。\n\r/|\t]+", field)
    tags, seen = [], set()
    for p in parts:
        p = p.strip(" 　.。:：!！?？\"'()（）[]【】-—").strip()
        if not p:
            continue
        if len(p) > 10:          # 过滤完整句子
            continue
        if not re.search(r"[\u4e00-\u9fa5A-Za-z]", p):
            continue
        if p in GENERIC_TAGS:    # 纯泛词：零检索信号，跳过
            continue
        if p not in seen:
            seen.add(p); tags.append(p)
        if len(tags) >= limit:
            break
    return tags


def describe_image(base_url: str, api_key: str, model: str, image,
                   prompt: str = "", max_tags: int = 12) -> dict:
    """给一张图片生成描述 + 标签。"""
    p = prompt or ("用简洁的中文描述这张图片的主要内容，列出关键物体、场景和类别。"
                   "只输出描述，不要解释。")
    txt = chat_completion(base_url, api_key, model, p, image=image,
                          max_tokens=max(120, max_tags * 20), timeout=120)
    tags = _clean_tags(txt, max_tags)
    return {"description": txt, "tags": tags, "model": model}
