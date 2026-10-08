#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
转写文字智能纠偏 —— 用已配置的 LLM 把 ASR 生文字修成人能读的文字。

**为什么原文一个字都不能动**（这是本模块最重要的一条约束）：
`asr_text` 同时喂两条检索路 —— 关键词路（`meta LIKE '%asr_text%'`）和音频
chunk 0 的向量重编码。而用户搜的时候，往往**正是照着他听到的那个错字打的**。
如果让 LLM 改写后覆盖原文，那些错字就永久搜不到了。所以：
    asr_text  = ASR 原始结果，永不改动
    asr_clean = LLM 优化版，另存一份
检索两路都吃这两个字段，UI 上可「原始 / 优化」切换、可一键还原。

场景分三类，因为它们该做的事完全不同：
    song       歌曲 —— 修错别字、剔掉伴奏/和声误识别，**绝不能加「XX说：」**
    dialogue   对话 —— 按轮次分段 + 加说话人前缀
    monologue  独白 —— 校对标点、分段

说话人前缀是**有把握才标**：LLM 只看到文字、听不到声音，标「女声说：」全靠
用词推测，会错。所以判不出来就老老实实标「说话人 A：」。想要真准，得上
说话人分离模型（pyannote），那是另一件事。

护栏（防 LLM 幻觉）：长度比、前后缀话术、代码围栏三道，任何一道不过就整份丢弃，
宁可让用户看到原文，也不要把加了戏的文字写进库。
"""
import re
import statistics

import ai_desc

# 场景
SONG = "song"
DIALOGUE = "dialogue"
MONOLOGUE = "monologue"
KIND_NAMES = {SONG: "歌曲", DIALOGUE: "对话", MONOLOGUE: "独白"}


class PolishError(Exception):
    """纠偏过程中的可读错误。"""


# ---------------------------------------------------------------------------
# 场景判定
# ---------------------------------------------------------------------------
# 先看文件名/路径里的线索 —— 免费、准、还能说清理由。判不出来才问 LLM。
_SONG_WORDS = (
    "音乐", "歌曲", "歌", "专辑", "唱片", "单曲", "金曲", "情歌", "民谣", "摇滚",
    "天籁", "无损", "发烧", "hifi", "dsd", "flac", "ape", "wav", "ktv", "伴奏",
    "演唱会", "live", "mv", "华语", "粤语", "国语", "欧美", "日语", "韩语", "女声",
    "男声", "合唱", "翻唱", "remix", "ost", "原声",
)
_DIALOGUE_WORDS = (
    "访谈", "对话", "采访", "播客", "podcast", "讲座", "演讲", "会议", "论坛",
    "课程", "相声", "脱口秀", "综艺", "影视", "电视剧", "电影", "台词", "字幕",
    "剧本", "广播剧", "连麦", "直播回放", "圆桌",
)
_MONOLOGUE_WORDS = (
    "有声书", "朗读", "独白", "讲解", "教程", "新闻", "播报", "口播", "评书",
    "听书", "课件", "复盘", "总结",
)


def _hint_kind(path: str):
    """从路径/文件名猜场景。返回 (kind, 理由) 或 (None, "")。"""
    if not path:
        return None, ""
    low = path.lower()
    for w in _DIALOGUE_WORDS:
        if w in low:
            return DIALOGUE, f"路径含「{w}」"
    for w in _MONOLOGUE_WORDS:
        if w in low:
            return MONOLOGUE, f"路径含「{w}」"
    for w in _SONG_WORDS:
        if w in low:
            return SONG, f"路径含「{w}」"
    return None, ""


# 中文歌词的指纹。**为什么需要它**：路径线索会被目录名带偏 ——
# `陈奕迅 - 谁来剪月光.mkv` 放在「影视」目录里，于是被判成「对话」，
# 而它其实是首歌词。对话提示词一旦照着做，就会给歌词加上「说话人 A：」。
#
# 三段切分后量三个数：平均句长、长度变异系数、相邻句「长度接近」的比例。
# 中文歌词句短（均长 5~10）、长短齐整（CV<0.55）、成对工整（邻齐>0.5）；
# 中文访谈/对话的长短参差得多（均长普遍 >12），所以 `mean <= 12` 这道闸
# 也顺带挡住了「把真对话误判成歌词」。
#
# 只对中文有效：英文歌的 ASR 断句很长、长短悬殊（均长 25~90、CV 0.7~1.3），
# 用不上这套指纹 —— 但英文歌也不会被「影视」这种中文目录名带偏，所以不吃亏。
_SEG_SPLIT = re.compile(r"[，,。！？；;、\n]")


def _looks_like_cjk_lyrics(text: str) -> bool:
    segs = [s.strip() for s in _SEG_SPLIT.split(text or "") if len(s.strip()) >= 3]
    if len(segs) < 12:
        return False
    L = [len(s) for s in segs]
    mean = sum(L) / len(L)
    if mean > 12:
        return False
    cv = statistics.pstdev(L) / mean
    near = sum(1 for a, b in zip(L, L[1:]) if abs(a - b) <= 3) / (len(L) - 1)
    return cv < 0.55 and near > 0.5


_CLS_PROMPT = """判断下面这段语音识别文字属于哪一类，只回一个词，不要解释：
歌曲 / 对话 / 独白

歌曲 = 歌词、唱词，句子短、押韵、反复。
对话 = 两个或多个人在交谈、访谈、采访、吵架、聊天。
独白 = 一个人在连续讲述、讲解、播报、朗读。

文字：
---
%s
---
只回「歌曲」或「对话」或「独白」这三个词之一。"""


def classify(text: str, path: str = "", base_url: str = "", api_key: str = "",
             model: str = "", timeout: int = 60):
    """判定场景。返回 (kind, 理由)。

    先走路径线索；线索判不出来（或只说了「歌」这种含糊词）再问一次 LLM。
    LLM 也判不出来就退到「独白」—— 它是最安全的一类：只改标点和错别字，
    不会去加说话人，也不会当成歌词删句子。
    """
    kind, why = _hint_kind(path)
    # ★ 路径说是「对话」，但文字一看就是中文歌词（影视目录里的歌 MV 就是这种），
    #   以文字为准 —— 给歌词加「说话人 A：」是这套功能最难看的失败模式。
    if kind == DIALOGUE and _looks_like_cjk_lyrics(text):
        return SONG, f"{why}，但文字是中文歌词的形态，改判歌曲"
    if kind:
        return kind, why
    if not (text and base_url and model):
        return MONOLOGUE, "无 LLM，退到最保守的独白"
    try:
        ans = ai_desc.chat_completion(base_url, api_key, model,
                                      _CLS_PROMPT % text[:1500],
                                      max_tokens=8, timeout=timeout)
    except Exception:
        return MONOLOGUE, "分类调用失败，退到最保守的独白"
    a = (ans or "").strip()
    if "歌曲" in a or "歌词" in a:
        return SONG, "LLM 判定"
    if "对话" in a or "访谈" in a:
        return DIALOGUE, "LLM 判定"
    if "独白" in a or "讲述" in a:
        return MONOLOGUE, "LLM 判定"
    return MONOLOGUE, "LLM 回答无法识别，退到最保守的独白"


# ---------------------------------------------------------------------------
# 纠偏提示词
# ---------------------------------------------------------------------------
_NO_META = ("\n直接输出正文本身，不要任何前言、后记、解释、标题或代码块围栏"
            "（不要出现「以下是」「校对后」「好的」这类话）。")

_SONG_PROMPT = """你是歌词校对助手。下面是歌曲的语音识别（ASR）原始结果，常有同音字错误，
以及把伴奏、和声、叹息误识别成的杂乱碎片。

只做这三件事：
1. 修正明显的同音字/错别字，让歌词读起来通顺、符合歌词的表达习惯。
2. 删掉明显不是人声的碎片（单独成句的「啊」「嗯」「哦」、乱码般的重复段），
   以及明显是识别故障的整段复读。
3. 保持原有的断句分行，一行一句。

严格禁止：
- 不许增加原文没有的歌词，不许凭想象补全。
- 不许改写原意，不许调整顺序。
- 听不出意思的句子就原样保留，不要编。
""" + _NO_META

_DIALOGUE_PROMPT = """你是对话记录整理助手。下面是包含人物对话的语音识别（ASR）原始结果，
可能有错别字，而且没有区分谁在说话。

只做这三件事：
1. 修正明显的错别字与同音字错误。
2. 按说话轮次分段，每段独立成行，行首加说话人前缀：
   - 上下文能**明确**看出身份时才写具体称呼，例如文中出现了自我介绍、被人点名、
     或明确写着「我是某某」 —— 这时写「张三说：」。
   - 能明确看出性别时才写「男声说：」「女声说：」（例如文中自称「哥」「姐」、
     或对话里明确点出）。
   - **其余一律**按出现顺序写「说话人 A：」「说话人 B：」，交替编号。
   - 拿不准就用「说话人 X：」，**绝对不要臆造**性别、姓名或身份。
3. 删掉明显的语气词碎片与识别故障的重复段落。

严格禁止：
- 不许编造原文没有的对话内容。
- 不许臆造说话人的性别、姓名或身份。

另外：如果通读下来发现这其实**不是对话** —— 比如它是一首歌的歌词，或者只有一个人
在连续唱/念、没有任何你来我往的轮次 —— 那就**一句说话人前缀都不要加**，
只做错别字校对，原样保留其余文字。
""" + _NO_META

_MONOLOGUE_PROMPT = """你是文字校对助手。下面是语音识别（ASR）原始结果，可能有错别字、
同音字错误和断句问题。

只做这三件事：
1. 修正明显的错别字与同音字错误。
2. 补好标点，让句子读得通。
3. 按语义分成通顺的段落。

严格禁止：
- 不许增加原文没有的内容，不许改写原意。
""" + _NO_META

_PROMPTS = {SONG: _SONG_PROMPT, DIALOGUE: _DIALOGUE_PROMPT, MONOLOGUE: _MONOLOGUE_PROMPT}


# ---------------------------------------------------------------------------
# 护栏
# ---------------------------------------------------------------------------
# LLM 加戏的典型开头。命中就整份丢弃 —— 原文还在，用户顶多看不到优化版，
# 但绝不会让「以下是校对后的歌词」这种话进库污染检索。
_META_HEAD = re.compile(
    r"^\s*(以下是|这是|好的|当然|明白|已为您|校对后|整理后|优化后|输出[:：]|如下[:：])")
_FENCE = re.compile(r"```")


def _plain_len(t: str) -> int:
    """只数非空白字符 —— LLM 爱加换行，按总长度比会误判。"""
    return len(re.sub(r"\s+", "", t or ""))


def check_polished(orig: str, out: str):
    """三道护栏。返回 (ok, 原因)。"""
    if not out or not out.strip():
        return False, "模型没有返回内容"
    n_o, n_n = _plain_len(orig), _plain_len(out)
    if n_o:
        ratio = n_n / float(n_o)
        if ratio < 0.55:
            return False, f"优化后只剩原文的 {ratio:.0%}，像被截断了"
        if ratio > 1.6:
            return False, f"优化后是原文的 {ratio:.0%}，像加戏了"
    if _META_HEAD.match(out):
        return False, "输出带着「以下是…」这类前言"
    if _FENCE.search(out):
        return False, "输出带了代码块围栏"
    return True, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def polish(text: str, kind: str, base_url: str, api_key: str, model: str,
           timeout: int = 180) -> dict:
    """按场景优化一段转写文字。返回 {ok, text, kind, reason} 或 {ok: False, message}。"""
    t = (text or "").strip()
    if not t:
        return {"ok": False, "message": "这段没有转写文字，先转写再优化"}
    if not (base_url and model):
        return {"ok": False, "message": "请先在设置里配置智能服务的地址与模型"}
    kind = kind if kind in _PROMPTS else MONOLOGUE
    prompt = _PROMPTS[kind] + "\n\n原始识别结果：\n---\n" + t + "\n---"
    # 别让模型被 max_tokens 掐掉：中文 1 字 ≈ 1 token 起步，留 2 倍余量
    cap = min(8192, max(512, int(_plain_len(t) * 2.2)))
    try:
        out = ai_desc.chat_completion(base_url, api_key, model, prompt,
                                      max_tokens=cap, timeout=timeout)
    except Exception as e:
        return {"ok": False, "message": f"调用智能服务失败：{e}"}
    out = (out or "").strip()
    ok, why = check_polished(t, out)
    if not ok:
        return {"ok": False, "message": f"优化结果被丢弃（{why}），原文未受影响"}
    return {"ok": True, "text": out, "kind": kind, "reason": why}


def polish_full(text: str, path: str, base_url: str, api_key: str, model: str,
                timeout: int = 180) -> dict:
    """判场景 + 优化，一步到位。"""
    kind, why = classify(text, path, base_url, api_key, model)
    r = polish(text, kind, base_url, api_key, model, timeout=timeout)
    if r.get("ok"):
        r["kind_reason"] = why
    return r
