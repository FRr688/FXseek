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
import shutil
import uuid
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
import aligner as aln
import eg2
import indexer as ix
import license as lic
import mobile as MOB
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
          "align_running": None,  # 正在后台对齐的文件路径
          "model_unloaded": 0}    # 累计「按需卸载」次数（诊断用）

# 模型按需加载的锁：两个请求同时发现模型没加载时，只让一个真去 load()，
# 否则会同时加载两份 2GB 权重（8G 机器上直接换页卡死）。
_MODEL_LOCK = threading.Lock()

# 纯轮询端点：前端（设置页 / 进度条）会按秒级定时打这些接口，
# 它们只代表「界面开着」不代表「用户在用」。如果拿它们刷新闲置计时，
# 设置页每 8 秒轮询一次 /v1/ai/status 就会把计时不断清零 ——
# 用户看到「空闲 2s / 300s」永远涨不上去，闲置自动打标/转写再也不会触发。
_QUIET_PATHS = ("/health", "/v1/ai/status", "/v1/asr/status",
                # ★ 2026-10-18：`/v1/stats` 是计数轮询（侧栏/筛选标签/顶栏都在刷它，
                #   实测每 6~7 秒一次）。把它算成用户活动的话，闲置计永远到不了
                #   1 分钟 —— 用户实测就是「后台补全一直不启动」，日志里
                #   能看到「还差 41 秒 / 18 秒」反复回退。
                "/v1/stats",
                "/v1/align/status", "/v1/idle/status",
                # 手机版：设置页的「连接」卡片会轮询这两个；手机自己的
                # 状态请求也不该被当成「用户在电脑前操作」，否则一开局域网
                # 就再也等不到空闲，自动打标/转写/对齐全部停摆。
                "/v1/mobile/state", "/v1/mobile/caps", "/v1/mobile/vectors",
                # 手机会定期自报家门（announce）—— 同样是机器在问，别清零闲置计时。
                # bye 是断开时打的那一声招呼，同理。
                "/v1/phone/announce", "/v1/phone/sources", "/v1/phone/bye",
                # 主界面的「已连接手机」徽标会轮询它 —— 同样是机器在问，
                # 不能算成「用户坐在电脑前」，否则顶栏一挂手机就再也闲不下来。
                "/v1/pair/devices",
                # 工作模式两扇窗的状态轮询：绳窗 700ms 问一次 "挂着什么"、55ms 问
                # 一次光标位置。这两个要是算成"用户坐在电脑前"，工作模式那套
                # 「闲置 N 分钟自动收起搜索层」就永远等不到 —— 页面自己一直在说话。
                "/v1/work/hung", "/v1/work/pointer", "/v1/work/stage",
                # ★ 2026-10-18：页面自己往上送的「几何/命中区」也不算用户在活动。
                #   注意区分：真正的「用户在活动」是 workwin 的光标探针**直接写**
                #   last_user_req（只在搜索区里移动时），不是这些上报接口。
                #   ropehit / ropeedit 是暂存区（绳窗）的悬停上报 —— 按口径暂存区
                #   不参与判定，若把它们算成活动，光在绳子上划一圈就把闲置计时清零。
                "/v1/work/hitrects", "/v1/work/searchzone",
                "/v1/work/ropehit", "/v1/work/ropeedit",
                "/v1/work/dismiss",
                # ★ 2026-10-18：缩略图是**渲染副作用**，不是人在操作。实测
                #   主窗一开就瀑布式拉（25 秒 179 次：网格重绘、导入后刷新、
                #   手机同步落地都会触发），闲置计被它按住不放 —— 用户看到的
                #   就是「补全永远不开始」。真要点开某个素材，后面跟着的
                #   /v1/media、详情请求照样会把计时清零，不缺这一下。
                "/v1/thumb")
# 闲置自动处理的运行状态（不落盘，重启清零）
_IDLE = {"run_tagged": 0, "run_asr": 0, "run_align": 0,
         "last_path": None, "last_ts": None,
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
    # 连过的手机（「手机来源」）—— 手机每次连上都会自报家门，见 phone_announce()。
    # 存的是缓存用的元信息（名字/地址/端口/件数/最后在线），素材本身在手机上。
    "phone_sources": {},
    "theme": "auto",          # auto | light | dark
    "font_size": "medium",    # small | medium | large
    "cache_limit_mb": 1024,   # 预览缓存上限(MB)
    # ★ 版本号唯一来源：改这里就够了 —— build_app.sh / release.sh 都从这一行 grep，
    # tray_helper 的「关于」兜底也从这里读。别在别处再写死版本号。
    # 1.1.3（2026-10-15 凌晨，范老板六条）：
    #  ① 唤醒即聚焦 —— ★ 1.1.2 那样只恢复页面的 window focus 监听是**没用的**：
    #     pywebview 的覆盖窗被 makeKeyAndOrderFront_ 叫出来时页面收不到 DOM
    #     focus 事件（workwin 自己早就记着这个坑）。改成 show() 里主动往
    #     WKWebView 派 `window.__fxseekFocus()`（异步派发，绝不走会死锁的
    #     evaluate_js）。
    #  ② 菜单栏去掉「打开收工文件夹」
    #  ③ 明信片改成**绕夹子**左右轻摆（±2.5°）：★ 原来写在 .post 的
    #     style.rotate 上，而 .post 的定位是 transform:translate ——
    #     CSS 合成顺序 translate→rotate 让每张卡绕屏幕左上角那个固定点转，
    #     力臂越长甩得越凶，所以「都快飞起来了」「怎么上下移动」。改写到内层
    #     .sway 上，支点才回到夹子咬绳的地方。
    #  ④ 搜索层和暂存区是两个独立窗口 —— 删掉「鼠标停 3 秒叫出搜索层」，
    #     唯一的联动是快捷键唤醒搜索框时暂存区进入可编辑态
    #  ⑤ 「收工」按钮从搜索窗搬进绳窗，**常驻屏幕左边缘**、点得到
    #     （绳窗整窗是点击穿透的，靠 workwin 的 _rope_hit_tick 单独放行它那一块）
    #  ⑥ 按下「收工」**立即**起飞：★ 原来 work_collect_hung 是「先复制完、
    #     再放 fly 旗子」，复制大文件要几秒 —— 现在旗子先放，复制在后台跑；
    #     动画也整体提速
    # 1.1.2（2026-10-14 深夜，范老板五条）：
    #  ① 唤醒即聚焦（上一版改保守了，撤销）+ 收工取消搜索后红✕要消失
    #  ② 已挂到绳上的素材在搜索结果里带动效隐去，从绳上摘下来又能回来
    #  ③ 收工动画真会飞了 —— ★ 根因是 /v1/work/collect 那条**带 paths 的岔路
    #     不发 fly 旗子**（界面走的偏偏就是它），不是动画本身；外加桌面 FXseek
    #     文件夹打系统红标签（macOS 26/27 上文件夹会整个变红）
    #  ④ 鼠标在暂存区停 3 秒 → 自动叫出搜索层（进入可拖删状态）
    #  ⑤ 明信片风铃式摆动；编辑态鼠标拂过像拨铃，荡几下慢慢收住
    "version": "1.1.1",
    # ---- 购买专业版 ----
    # 收款页地址。默认指向爱发电商品页（FXseek 电脑专业版 · 一次买断 ¥29.9），
    # 2026-10-18 起改为爱发电；换平台时把下面这行换成新地址即可，
    # 就跳到它。**不写死第三方平台域名** —— 平台会换、会跑路，写死在代码里
    # 等于把用户往一个可能已经死掉的地址送。
    "buy_url": "https://ifdian.net/item/f9d058d0c70111f199f15254001e7c00",
    # 价格只用来显示。改价钱不必重打包。
    "buy_price": "¥29.9",
    # ★ 付款之后码怎么到手，决定了点「购买」时走哪条路（见 settings.html 的 licBuy）：
    #   "afdian" —— 爱发电这类平台：**没有「用我自己的订单号下单」的接口**
    #               （它的 API 是只读的：查订单 + webhook），所以自动激活那条路走不通。
    #               只能跳过去、他在那边付完、平台页面直接显示兑换码、他抄回来粘进激活框。
    #               好处是**零开户费**（爱发电抽 6%），也不用部署任何东西。
    #   "auto"   —— 自家发货页（发货页/worker.js 那套，走聚合支付）：
    #               后端先开单拿 order+nonce，轮到浏览器打开带参地址，
    #               再每 3 秒轮询发货页要码，自动激活 —— 用户一次都不用粘。
    #               代价是要先交聚合支付的开户费（XorPay ¥100 等）。
    # 换通道只改这一个词，前端两条路的代码都留着。
    "buy_mode": "afdian",
    # 点窗口关闭按钮时怎么办：ask = 每次问；quit = 直接退出；tray = 直接最小化到菜单栏。
    # 由 launcher.py 的关闭确认框写入（勾了「记住我的选择」才会变成 quit/tray）。
    "close_action": "ask",
    # ======================================================================
    # 界面模式（1.11 起）
    # ----------------------------------------------------------------------
    # FXseek 现在有两副面孔，**互斥**，同一时刻只会有一副在桌面上：
    #   "leisure" —— 休闲模式：就是原来那个大窗口（素材墙 / 索引源 / 看板 / 设置）。
    #   "work"    —— 工作模式：主窗口收起来，只剩菜单栏图标 + 一个全局唤醒键。
    #                按唤醒键，桌面浮出一个极简搜索框；搜到的缩略图可以拖出来
    #                挂在一条「曲线绳」上暂存，最后点绳子左端的「收工」，统一
    #                复制一份进桌面 FXseek 文件夹（按类别分目录）。
    #
    # 为什么做成互斥而不是「再来一个窗口」：两副面孔同时出现在屏幕上时，
    # 用户分不清「我现在在哪个模式里、关掉这个会发生什么」。互斥之后，
    # 任何时刻屏幕上最多只有一套 FXseek 的东西，心智负担为零。
    "ui_mode": "leisure",
    # 工作模式的唤醒键。写法见 work_key.py 的 parse_hotkey()：
    #   "cmd+alt+space" / "ctrl+shift+f" / "f6" …
    # 默认给了 ⌥⌘Space —— 和系统 Spotlight（⌘Space）差一个 Option，
    # 手不用挪位置，又不会撞车。
    "work_hotkey": "alt+cmd+space",
    # 搜索框「多久没人动就自动收」。0 = 不自动收。放这里而不是写死在页面里，
    # 是因为「一分钟」对有的人太短（正翻结果呢就没了）、对有的人太长。
    "work_idle_sec": 60,
    # ★ 工作模式**没有**「收工放哪儿」这个设置项了（范老板 2026-10-14：
    #   「设置里工作模式只有设置快捷键这一个功能」）。落点固定是桌面的
    #   FXseek 文件夹，见 work_out_dir()。
    #   代价要说清楚：桌面要是被搬进了 iCloud，expanduser("~/Desktop") 拿到的
    #   是本地那个空目录，收进去的东西在桌面上看不见 —— 真碰上了再单独处理。
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
    # ---- 增强对齐时间轴（把转写文字逐字钉到音频时间轴上）----
    # 默认关闭：要额外下 1.2 G 的强制对齐模型；只有「歌词/字幕跟着唱走」这类
    # 场景才有肉眼收益，图片库用户白下一个 G 是纯浪费。
    # 开启后由后台队列自动跟随转写（asr 一完成就对齐同一素材），不用手动点。
    "align_enabled": False,
    # 对齐模型的下载源，取 model_dl.SOURCES 的键（mirror = hf-mirror.com，
    # hugging = huggingface.co）。国外用户切到官方源快得多，和「首次下载模型」
    # 的准备窗口共用同一套源定义。
    "align_source": "mirror",
    # ---- 歌词/字幕高亮外观 ----
    # 颜色取 LYRIC_COLORS 里的 id（blue / violet / teal / amber / rose），
    # 样式取 word（逐字，要该素材有对齐数据才生效）或 line（整行）。
    # ★ 播放面板本身只吃 localStorage 缓存就能立刻上色，这两个键是给
    # 「换台机器/清了缓存」和设置页读取用的 —— 但必须在这里登记，
    # 否则 save_settings() 会把前端 POST 上来的值直接丢掉。
    "lyric_color": "blue",
    "lyric_style": "word",
    # ---- 手机版 / 局域网（见 docs/手机版规划.md）----
    # 默认**关**：开了之后局域网里任何设备都能敲到这个服务，所以必须由用户
    # 主动在「设置 → 连接」里打开（安全第一）。关着的时候下面的键全都不起作用，
    # 回环请求（桌面端自己的 UI）也永远放行，完全感知不到手机版的存在。
    "mobile_lan": False,
    # 已配对的手机：[{id, name, token, created, last_seen, from}]。
    # ★ token 只存本机；设置页只显示打了码的前缀，`/v1/pair/devices` 也不回完整值。
    "mobile_devices": [],
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


def license_status() -> dict:
    """许可证现状 + 用掉了多少配额 —— 一眼看清「还差多少撞墙」。

    ★ 素材件数用索引库里 `COUNT(DISTINCT path)`（`ix.stats()["files"]`），
      不是向量条数。一个视频会拆成几十个单元，拿向量条数去跟「总素材不超过 N」
      比，用户会看到「我才放了 30 个视频怎么就 3000/2000 了」—— 那是在骗人。

    ★ 2026-10-13（范老板）：口径改成 **本机索引 + 所有联机终端的素材，合计**。
      原来是只算本机，手机相册几千张完全不进数 —— 那等于给了个后门，
      「免费版 2000 件」直接形同虚设。
      只算**在线**的：手机拔了线就把它的数摘出去，否则用户会觉得
      「我手机都关了怎么还占着我的额度」。
    """
    used_items = 0
    try:
        used_items = int(ix.stats().get("files") or 0)
    except Exception:
        pass
    phone_items, phone_n = 0, 0
    try:
        now = time.time()
        for rec in _phone_sources().values():
            if not isinstance(rec, dict):
                continue
            if (now - float(rec.get("last_seen") or 0)) >= PHONE_SOURCE_TTL:
                continue                      # 不在线的不算
            phone_items += int(rec.get("items") or 0)
            phone_n += 1
    except Exception:
        pass
    used_sources = len(load_sources())
    st = lic.status(used_sources=used_sources, used_items=used_items + phone_items,
                    used_terminals=len(lic.terminals()))
    # 拆开给界面看，好让用户明白「2000 是怎么来的」
    st["used"]["desktop_items"] = used_items
    st["used"]["phone_items"] = phone_items
    st["used"]["phone_online"] = phone_n
    # 购买入口（发货页地址 + 价格）—— 界面照这个渲染「购买专业版」那张卡。
    # 放在 license_status 里而不是另开接口：它跟 tier 是同一次渲染要用的东西。
    _cfg = load_settings()
    _mode = str(_cfg.get("buy_mode") or "afdian").strip().lower()
    if _mode not in ("afdian", "auto"):
        _mode = "afdian"
    st["buy"] = {"url": str(_cfg.get("buy_url") or "").strip(),
                 "price": str(_cfg.get("buy_price") or "¥29.9").strip(),
                 "mode": _mode,
                 "free_items": lic.FREE_MAX_ITEMS,
                 "free_terminals": lic.FREE_MAX_TERMINALS}
    return st


def license_activate(code: str) -> dict:
    """粘贴一个码。验签通过才落盘（见 license.activate）。"""
    r = lic.activate(code)
    if r.get("ok"):
        op_log("许可证", f"已解锁专业版（序列号 {r['license'].get('serial')}）")
        return {**r, **license_status()}
    msg = {"empty": "还没填码", "invalid": "这个码验不过 —— 是不是少粘了一段、或者改过？",
           "expired": "这个码已经过期了", "io": "写不进去（磁盘权限？）"}
    op_log("许可证", f"激活失败：{r.get('error')}", level="warn")
    return {**r, "message": msg.get(r.get("error"), "激活失败"), **license_status()}


def license_deactivate() -> dict:
    """把本机许可证摘掉（换机器 / 退货用）。不联网，也不会自动退款。"""
    lic.deactivate()
    op_log("许可证", "已移除本机许可证，回到免费版")
    return license_status()


# --------------------------------------------------------------------------
# 分享奖励（2026-10-13 范老板定的方案）
# --------------------------------------------------------------------------
def _phone_tokens() -> dict:
    """{配对设备 id: 配对令牌} —— 给凭证码验签用。

    ★ 只认**我们这边存着的**令牌。用户在设置里手编一个 id 出来是没用的：
      验签要拿令牌当 HMAC 的密钥，他猜不到 32 字节的 token_urlsafe。
    """
    out = {}
    try:
        for d in MOB._devices_raw():
            if d.get("id") and d.get("token"):
                out[str(d["id"])] = str(d["token"])
    except Exception:
        pass
    return out


def license_ticket(code: str) -> dict:
    """兑现手机上那段凭证码（分享奖励的**副路径**）。

    主路径是手机连着电脑时随 announce 心跳自动上报（见 phone_announce）。
    副路径给的是「手机没连电脑」的场景：用户在手机上看一段码，抄到电脑端贴这儿。

    安全性靠 HMAC：密钥是**配对时交换的那串令牌**，只有真正配过对的手机算得出。
    次数写进签名，所以同一段码重放也不会重复加分（`redeem_ticket` 那边
    记的是「这台手机已计入的累计值」，同一个数报第二次是 0）。
    """
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "请把手机上那段凭证码粘贴进来。", **license_status()}
    r = lic.redeem_ticket(code, _phone_tokens())
    if not r.get("ok"):
        # ★ 原因码在 `error` 字段里（见 license.redeem_ticket 的说明），不是 `reason`。
        #   踩过一次：这里读 `r.get("reason")`，结果不管什么错都落到兜底那句
        #   「这段码用不了」—— 用户抄错一个字符也看不出到底错在哪。
        reason = r.get("error") or "invalid"
        tips = {
            "format": "这段码形状不对。应该是 FXT1- 开头的四段"
                      "（FXT1-朋友次数-朋友圈次数-签名）—— "
                      "在手机上长按「拷贝」再粘过来，别手打（手打极易错）。",
            "range": "这段码里的分享次数不对劲，回手机上重新复制一次。",
            "nokey": "这台电脑还没配对过任何手机，没法验证这段码 —— "
                     "先用「手机连接」把手机连上一次，之后再贴这段码就认了。",
            "invalid": "这段码对不上任何一台已配对的手机。"
                       "确认是在「分享」里复制的那段吗？别的手机配对的码不通用。",
            "cooling": "今天这次已经计过了，明天再贴同一段码还能继续加。",
            "capped": "分享奖励已经攒满 3 个月，用不完啦。",
        }
        return {"ok": False, "error": tips.get(reason, "这段码用不了。"),
                "reason": reason, **license_status()}
    if r.get("credited"):
        op_log("分享奖励", "凭证码 +%d 天电脑专业版（累计 %d 天）"
               % (r.get("days_granted") or 0, r.get("share_days_total") or 0))
    return {**r, **license_status()}


def license_unbind(dev_id: str) -> dict:
    """解绑一台移动终端（换手机用）。

    ★ 只动「绑定名单」，**不碰**它的分享计数 —— 用户误点解绑再绑回来，
      攒过的天数还得在。删计数等于惩罚手滑。
    """
    dev_id = (dev_id or "").strip()
    if not dev_id:
        return {"ok": False, "error": "没指定要解绑哪台。", **license_status()}
    name = ""
    for t in lic.terminals():
        if str(t.get("id")) == dev_id:
            name = t.get("name") or ""
            break
    r = lic.unbind_terminal(dev_id)
    if r.get("ok"):
        op_log("许可证", "已解绑移动终端「%s」，可以换手机了" % (name or dev_id))
    return {**r, **license_status()}


def license_share(source: str = "desktop", share_kind: str = "") -> dict:
    """记一次分享。

    ★ 范老板 2026-10-13（下午·十一）把奖励分成两档之后，这里要**分开两个参数**：
      `source`      —— 谁记的账（desktop / phone），只进返回值和日志；
      `share_kind`  —— 哪一档（friend = 分享给朋友 15 天 / moments = 发朋友圈 30 天）。
      别把这两个混成一个：早先签名是 `license_share(kind)`，那个 kind 其实是 source，
      再接一档进去会变成「传了 friend 却被当成来源」，界面上天数就永远不对。
    """
    sk = share_kind or lic.SHARE_KIND_MOMENTS
    r = lic.credit_share(source or "desktop", 1, sk)
    if r.get("credited"):
        op_log("分享奖励", "分享 +%d 天电脑专业版（累计 %d 天）"
               % (r.get("days_granted") or lic.share_days(sk),
                  r.get("share_days_total") or 0))
    return {**r, **license_status()}


def license_share_poster(path: str = "") -> dict:
    """现画一张分享海报存盘（电脑端「保存分享海报」）。

    ★ 用 App 里那份 `share_card.py`（和根目录命令行入口同源）。
      失败不致命（用户可能只是想复制文案），所以异常一律兜成 error 字段，
      不让它把整个设置面板干崩。
    """
    try:
        import share_card
        out = path or os.path.join(os.path.expanduser("~"), "Desktop", "FXseek-分享图.png")
        r = share_card.render(out)
        op_log("分享奖励", "已保存分享海报：%s" % r["path"])
        return {"ok": True, **r}
    except Exception as e:
        op_log("分享奖励", "生成分享海报失败：%s" % e, level="warn")
        return {"ok": False, "error": "生成海报失败：%s" % e,
                "text": _share_text_fallback()}


# --------------------------------------------------------------------------
# 购买专业版（2026-10-13 范老板：应用里要能直接买）
# --------------------------------------------------------------------------
#
# 设计：**App 不碰钱、也不碰码池**。它只干两件事 ——
#   ① 拿一个订单号去开发货页；
#   ② 付款后自己去问「那笔单发货了没」，发了就自动激活。
#
# 为什么不「让发货页反过来调本机接口把 App 激活了」：
#   发货页是**公网**上的东西。本机 8231 这个接口对回环请求是完全放行的
#   （桌面端自己的 UI 就靠它），一旦让网页能调它，那么用户随便打开一个
#   恶意页面就能白拿专业版。**这里的请求方向只有一个：App 往外问。**
#
# 用户手抄码那条路（激活框）**始终保留** —— 发货页挂了、码没取到、
# 或者他就是在手机上买的，都能自己粘回来。

_BUY_ORDERS = {}           # {order: {"nonce","at","tries"}}
_BUY_TTL = 3600            # 一笔下单管 1 小时，够他慢慢付款


def buy_start(buy_url: str, price: str = "") -> dict:
    """开一笔购买：生成本机订单号 + 一个只有本机知道的 nonce，拼出发货页地址。

    ★ order 是**本机生成**的、不是发货页生成的 —— 只有这样 App 才知道该问谁要码。
    ★ nonce 是给发货页 `?n=` 用的：订单号本身是 `FX…` 这种能猜的形态，
      没有 nonce 的话别人拿订单号去 `/api/order` 就能把你的码读走。
    """
    import secrets
    # 界面一般会把当前地址一起传上来；没传就回落设置里的 buy_url ——
    # 这样 `curl -d '{}'` 也能下单（自测和脚本化都方便）。
    url = str(buy_url or "").strip() or str(load_settings().get("buy_url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "还没配置发货页地址（设置里的 buy_url）。"}
    now = time.time()
    for k in [k for k, v in _BUY_ORDERS.items() if now - v["at"] > _BUY_TTL]:
        _BUY_ORDERS.pop(k, None)
    order = "FX" + secrets.token_hex(5).upper()
    nonce = secrets.token_urlsafe(12)
    _BUY_ORDERS[order] = {"nonce": nonce, "at": now, "tries": 0}
    sep = "&" if "?" in url else "?"
    full = "%s%soid=%s&n=%s&v=%s" % (
        url, sep, order, nonce, str(load_settings().get("version") or ""))
    # nonce 也给界面一份：它轮询 /buy/poll 时要带上（见 settings.html 的 licBuy）
    return {"ok": True, "order": order, "nonce": nonce, "url": full, "price": price or ""}


def buy_poll(order: str, nonce: str) -> dict:
    """问发货页：我那笔单发货了没？发了就直接激活，用户一次都不用粘。

    失败一律「还没好」而不是报错 —— 用户刚付完款那几秒本来就是空的，
    弹个红字只会吓人。
    """
    import json as _json
    import urllib.request
    order = str(order or "").strip()
    rec = _BUY_ORDERS.get(order)
    if not rec or rec["nonce"] != str(nonce or ""):
        return {"ok": False, "pending": False, "error": "这笔下单对不上（可能重启过设置页）。重新点一次「购买专业版」。"}
    rec["tries"] += 1
    base = str(load_settings().get("buy_url") or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "pending": True}
    try:
        api = "%s/api/order?order=%s&n=%s" % (base, order, rec["nonce"])
        with urllib.request.urlopen(api, timeout=6) as r:
            d = _json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception as e:
        return {"ok": False, "pending": True, "note": "问发货页失败：%s" % e}
    code = str(d.get("code") or "").strip()
    if not code:
        return {"ok": False, "pending": True}
    if d.get("nonce_ok") is False:
        return {"ok": False, "pending": False, "error": "发货页说这串校验不过，把订单号发给作者：%s" % order}
    r2 = lic.activate(code)
    if not r2.get("ok"):
        return {"ok": False, "pending": False,
                "error": "发货页送来的码验不过（%s）。把订单号发给作者：%s" % (r2.get("error"), order)}
    op_log("许可证", "购买成功并已自动激活（订单 %s，序列号 %s）"
           % (order, (r2.get("license") or {}).get("serial")))
    _BUY_ORDERS.pop(order, None)
    return {"ok": True, "serial": (r2.get("license") or {}).get("serial"), **license_status()}


def _share_text_fallback() -> str:
    """海报画不出来时至少把文案给出来（兜底，不让按钮白点）。"""
    try:
        import share_card
        return share_card.share_text()
    except Exception:
        try:
            v = str(load_settings().get("version") or "")
        except Exception:
            v = ""
        return ("分享一个我自己在用的本地素材库：FXseek\n\n"
                "图片、视频、音频、文档都能用大白话搜，全程本地跑，"
                "不上传、不注册，非商业用途免费。\n\n"
                "👉 下载：https://github.com/FRr688/FXseek/releases/latest  （v%s）" % v)


def add_source(path: str) -> dict:
    """记录一个索引源；返回该源的实时统计。

    ★ 2026-10-13（范老板）：**索引源个数不再限制**（`FREE_MAX_SOURCES = None`）。
      原来免费版卡 3 个源，实际用下来「几个源」跟「值不值 19.9」几乎无关，
      反而最先撞墙、最容易让人觉得「这软件是坏的」。免费版现在只卡两件事：
      素材件数（2000，含联机终端）和移动终端台数（1）。
      这里保留 `cap` 那道判断是为了**万一以后又要加源数限制**时不用改结构，
      而不是忘了删。
    """
    path = os.path.realpath(os.path.expanduser(path))
    items = load_sources()
    if not any(s["path"] == path for s in items):
        cap = lic.limits().get("max_sources")
        if cap is not None and len(items) >= cap:
            return {"sources": sources_with_stats(), "upgrade": True,
                    "error": f"免费版最多 {cap} 个索引源（你现在有 {len(items)} 个）。"
                             f"把不常用的目录先移除，或者在「设置与备份 → 许可证」里解锁专业版。"}
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
    _eg2_forget(con, victims)
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

def _eg2_forget(con, paths):
    """删素材时把它的 EG2 向量一起删掉，并留一条墓碑。

    ★ 墓碑不能省。手机是按 `rev` 增量同步的，只删不留的话它看不出「这条没了」，
      本地会永远留着一条点开是空的记录。
    ★ 整段包在 try 里：这张表是手机版后加的，老库第一次删素材时它可能还不存在，
      绝不能因为清理一张附属表而让「删素材」本身失败。
    """
    try:
        eg2.ensure_schema(con)
        eg2.drop(con, paths)
    except Exception as e:
        print("[eg2] 向量清理跳过：%s" % e)


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
_FAIL_COOL = 900         # 冷藏时长（秒），第一次冷藏用这个
# ★ 2026-10-18（范老板问「打标一直失败怎么办」）：冷藏到期再试还失败，就**逐级
#   加长**——否则一个彻底坏掉的文件会每 15 分钟回来抢一次队头，把正在进行的
#   转写/对齐阶段打断一次（按阶段推进之后，这种打断比轮换时更显眼）。
#   最后停在 6 小时：文件真修好了，最多等 6 小时自己回来，不需要重启。
_FAIL_COOL_STEPS = ((_FAIL_MAX * 2, _FAIL_COOL), (_FAIL_MAX * 4, 3600))
_FAIL_LOOKAHEAD = 200    # 选任务时最多往后看多少个候选


def _fail_key(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path or ""


def _cool_seconds(n: int) -> float:
    """连败 n 次要冷藏多久：15 分钟 → 1 小时 → 6 小时，逐级加长。"""
    n = int(n or 0)
    for limit, secs in _FAIL_COOL_STEPS:
        if n < limit:
            return float(secs)
    return 21600.0


def _is_cooled(path: str) -> bool:
    """这个文件刚连败太多次？是的话本轮先跳过它。"""
    f = _IDLE.get("fail", {}).get(_fail_key(path))
    if not f or int(f.get("n", 0)) < _FAIL_MAX:
        return False
    return (time.time() - float(f.get("ts", 0))) < _cool_seconds(int(f.get("n", 0)))


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


_IDLE_WHY = {"msg": None, "ts": 0.0}


def _idle_why(msg: str):
    """闲置循环「为什么没开跑」的说明。60 秒最多一行，理由变了立刻打。

    ★ 以前这些分支全是静默 `continue`：设置页写着「空闲 1 分钟后开始」，
      用户等半天什么都不动，日志里也一个字都没有 —— 只能靠猜是重任务占着、
      还差几十秒、名额满了、还是压根没有待办。现在直接说清楚。
    """
    now = time.time()
    if msg == _IDLE_WHY["msg"] and now - _IDLE_WHY["ts"] < 60:
        return
    _IDLE_WHY["msg"] = msg
    _IDLE_WHY["ts"] = now
    print("[闲置处理] " + msg, flush=True)


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
    三类任务**严格分阶段**：打标先清空，再整体转写，最后整体对齐（不轮换）。
    这样同一个本地模型在一整段里是热的，只在换阶段时付一次加载代价 ——
    轮换会让模型「卸了装、装了卸」，每条素材都白等一次加载（2026-10-18 改）。
    """
    while True:
        try:
            # ★ 2026-10-18（范老板「外派特别慢」）：外派任务先探一眼再睡 ——
            #   以前是「睡 20 秒 → 接一条 → 干完 → 再睡 20 秒」，4 个素材排队就是
            #   两分钟，而真干活只要 2–5 秒；那 20 秒的等待比活本身还长。
            #   空转时也别一睡 20 秒：按 1 秒一探，手机那边点了按钮 1 秒内就该动，
            #   空转 5 秒（不是 20 秒）就回来巡一圈：从「刚满足空闲门槛」到
            #   「真的开跑第一个」最多只等 5 秒（范老板 2026-10-18 定的）。
            # 刚干完一件就只探 1 秒：闲置补全连着跑的时候，别让「20 秒巡一圈」
            # 挡在两条素材中间（一条素材本身已经要十几到几十秒，再等 20 秒很冤）。
            _recent = (time.time() - float(_IDLE.get("ran") or 0)) < 6.0
            _ran = False
            for _i in range(1 if _recent else 5):
                _pj0 = phone_job_take()
                if _pj0:
                    phone_job_run(_pj0)
                    _ran = True
                    break
                time.sleep(1)
            if _ran:
                time.sleep(0.2)
                continue
            # ★★ 工作模式**不再冻结**这一圈（范老板 2026-10-18）：
            #    打标 / 转写 / 对齐 / 手机外派任务在工作模式下照跑，区别只在
            #    「算不算用户在用电脑」——那个判据挪到了 last_user_req 的
            #    来源上（只有搜索区里的光标移动才刷新，见 workwin._activity_probe），
            #    所以这里不用再挡。
            st = load_settings()
            # ★★ 外派任务优先：手机派来的活排在最前面，不看空闲时长、不看本轮上限，
            #    也不受「闲置处理开关关着」影响 —— 这是用户明确要的插队优先级。
            _pj = phone_job_take()
            if _pj:
                phone_job_run(_pj)
                continue
            idle_tag = bool(st.get("idle_tag")) and bool(st.get("ai_enabled"))
            idle_asr = bool(st.get("idle_asr")) and bool(st.get("asr_enabled"))
            # 对齐**不是**独立的开关：它跟着「增强对齐时间轴」走，且只在转写
            # 开着时才有意义（没转写文本就没有可对齐的东西）。
            idle_align = (bool(st.get("align_enabled")) and idle_asr
                          and aln.available()[0])
            if not (idle_tag or idle_asr or idle_align):
                continue
            # 打标/转写/纠偏/建索引互斥 —— 顺带把正在跑的「智能纠偏」也算进来，
            # 否则用户点了「优化全部」之后，闲置补全还会照旧开跑，两边同时打 oMLX。
            heavy = _heavy_running(exclude="idle")
            if heavy:
                _IDLE["window"] = None          # 用户回来了/正在忙 → 本轮计数作废
                _idle_why("等「%s」让路（重任务互斥）" % heavy)
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
                idle_align = idle_align and want_kind in ("align", "both")
                if not (idle_tag or idle_asr or idle_align):
                    _IDLE["manual"] = None      # 开关在跑的过程中被关掉了
                    continue
                need, limit, total_run = 0.0, 0, 0
            else:
                idle_sec = time.time() - _STATE.get("last_user_req",
                                                   _STATE.get("last_req", 0))
                need = float(st.get("idle_minutes", 5) or 5) * 60
                if idle_sec < need:
                    _idle_why(f"还差 {int(need - idle_sec)} 秒（已闲置 {int(idle_sec)}s / 需要 {int(need)}s）")
                    continue
                # 一段连续空闲算「一轮」；窗口编号变了就把本轮计数清零
                win = int(time.time() // max(need, 60))
                if _IDLE.get("window") != win:
                    _IDLE["window"] = win
                    _IDLE["run_tagged"] = 0
                    _IDLE["run_asr"] = 0
                    _IDLE["run_align"] = 0
                    _IDLE["run_fail"] = 0
                limit = int(st.get("idle_batch", 10) or 10)
                # 失败也占名额：不然同一批失败文件会被无限重试、
                # 计数永远停在 0/10（用户看到的就是「卡住不动」）
                total_run = (_IDLE.get("run_tagged", 0) + _IDLE.get("run_asr", 0)
                             + _IDLE.get("run_align", 0)
                             + _IDLE.get("run_fail", 0))
                if limit > 0 and total_run >= limit:
                    _idle_why(f"本轮名额已用完（{total_run}/{limit}），下一轮再来")
                    continue

            # **严格分阶段：打标 → 转写 → 对齐，不轮换。**
            # ★ 2026-10-18（范老板）：原来三态是轮流的（turn 每件 +1）。可本地模型
            #   一旦换成自己跑，轮换就很贵 —— 一件打标刚做完，1 秒后就去转写，
            #   ASR 开始加载、打标模型刚热就被换掉；下一件又轮到打标，再加载一遍。
            #   实测观感就是「模型一直在反复装卸」，每条素材都白付一次加载时间。
            #   现在按阶段推进：有打标就一口气把待打标的打完（同一模型连续热着），
            #   打标清零之后才整体转写，转写清零之后才整体对齐。只有**换阶段**时才
            #   付一次加载代价，一批素材省掉 N 次。
            #   某一类暂时没待办就自然落到下一类，不会因为对齐全做完了就空转。
            order = ["tag", "asr", "align"]
            job = None
            all_cooled = False
            for want in order:
                if want == "tag" and idle_tag:
                    pend = ix.ai_tag_status()["pending"]
                elif want == "asr" and idle_asr:
                    pend = _asr_pending_list()
                elif want == "align" and idle_align:
                    pend = aln.pending_list()
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
                if not all_cooled:
                    _idle_why("没有待办（打标 %s 个 / 转写 %s 个 / 对齐 %s 个）" % (
                        (ix.ai_tag_status()["pending"] if idle_tag else "关"),
                        (len(_asr_pending_list()) if idle_asr else "关"),
                        (len(aln.pending_list()) if idle_align else "关")))
                if all_cooled:
                    n = _cooled_count()
                    _IDLE["last_error"] = (f"{n} 个素材连续失败，已冷藏（15 分钟起、"
                                           f"逐级加长到 6 小时），这期间先做别的")
                    # ★ 自动模式下不标「完成」：闲置队列本来就是等冷藏过期自己接，
                    #   标完成会让设置页显示「结束」，而 187 个待办一个没动。
                    #   但**手动「立即补全」**是另一回事 —— 用户点了按钮在等结果，
                    #   不能为一个坏文件挂几小时（冷藏最高 6 小时）。
                    if manual_on:
                        man["finished"] = time.time()
                        print(f"[立即补全] 剩下的 {n} 个都在失败冷藏里，这一轮先结束"
                              f"（成功 {man.get('done', 0)} 个、失败 {man.get('failed', 0)} 个）")
                    else:
                        print(f"[闲置处理] 待办全部处于失败冷藏中（{n} 个），等冷藏过期再试")
                    continue
                if manual_on:
                    man["finished"] = time.time()
                    print(f"[立即补全] 完成（成功 {man.get('done', 0)} 个"
                          f"、失败 {man.get('failed', 0)} 个）")
                continue

            kind, path, left = job
            _IDLE["ran"] = time.time()       # 记一笔「刚干过活」，下一圈别睡满 20 秒
            _IDLE_WHY["msg"] = None          # 开跑了，理由作废（下次等的时候重打）
            label = {"tag": "打标", "asr": "转写", "align": "对齐"}.get(kind, kind)
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
                     else asr_transcribe_one(path, st) if kind == "asr"
                     else align_one(path, st))
            except Exception as e:
                # 单个文件炸了不许带走整批：包装成普通失败，交给下面的账本
                r = {"ok": False, "message": f"{type(e).__name__}: {e}"}
            _IDLE["turn"] = _IDLE.get("turn", 0) + 1
            if r.get("ok"):
                _clear_fail(path)
                _IDLE["consec_fail"] = 0
                key = {"tag": "run_tagged", "asr": "run_asr",
                       "align": "run_align"}[kind]
                _IDLE[key] = _IDLE.get(key, 0) + 1
                _IDLE["last_error"] = None
                if manual_on:
                    man["done"] = man.get("done", 0) + 1
                if kind == "tag":
                    print(f"[{tag}] 完成 {os.path.basename(path)}"
                          f" · {len(r.get('tags') or [])} 个标签 · {r.get('seconds')}s")
                elif kind == "asr":
                    print(f"[{tag}] 完成 {os.path.basename(path)}"
                          f" · {r.get('chars')} 字 · {r.get('seconds')}s")
                else:
                    print(f"[{tag}] 完成 {os.path.basename(path)}"
                          f" · {len(r.get('items') or [])} 条时间轴"
                          f" · {r.get('chunks')} 块 · {r.get('elapsed')}s")
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
                        _cool_min = int(_cool_seconds(n) // 60)
                        _cool_txt = (f"{_cool_min} 分钟" if _cool_min < 120
                                     else f"{_cool_min // 60} 小时")
                        print(f"[{tag}] 失败 {os.path.basename(path)}"
                              f"（已连败 {n} 次，冷藏 {_cool_txt}再试，其它素材照顺序继续）: {msg}")
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
    # 工作模式不再冻后台（见 _work_frozen 的说明）：这里只把「搜索区活动探针」
    # 接上，并记下当前模式。上次退出时停在工作模式也没关系 —— 打标转写照跑，
    # 只是活动判定改为「光标在搜索区里动才算人坐在电脑前」。
    _work_init_activity()
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


PHONE_CACHE_DIR = os.path.join(P.DATA_DIR, "phone-cache")


def _phone_cache_dir(device: str) -> str:
    safe = "".join(c for c in str(device) if c.isalnum() or c in "-_")[:80] or "phone"
    d = os.path.join(PHONE_CACHE_DIR, safe)
    os.makedirs(os.path.join(d, "thumbs"), exist_ok=True)
    return d


def _phone_req(rec: dict, path: str, method: str = "GET", body: dict | None = None,
               timeout: float = 20.0, raw: bool = False):
    """替电脑去问手机要东西。手机那边要令牌（回环才免）。"""
    import urllib.request
    url = "http://%s:%s%s" % (rec.get("host"), rec.get("port"), path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    tok = _phone_token(rec.get("id") or "")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
        return payload if raw else json.loads(payload.decode("utf-8", "replace"))


def _phone_token(device: str = "") -> str:
    """电脑手里那枚「发给手机」的令牌 —— 手机端拿它校验来客。

    ★ 必须挑对**是哪一台**。反复重装 / 重配对会在 `mobile_devices` 里堆出好几条
      同名记录（iPhone 的 identifierForVendor 一重装就变），老写法「取第一条有令牌的」
      会抓到几天前那条死记录的令牌 → 手机回 401 被 except 吞掉 → 日志只剩一句
      「拉原件失败：手机可能不在线了」。看着像网络问题，其实是认错了设备。
    """
    try:
        raw = [d for d in MOB._devices_raw() if (d.get("token") or "")]
    except Exception:
        raw = []
    if not raw:
        return ""
    rec = _phone_sources().get(device) if device else None
    if isinstance(rec, dict):
        # ① 自报家门那一步把配对记录 id 记下来了 —— 精确匹配。
        did = str(rec.get("device_id") or "")
        if did:
            for d in raw:
                if str(d.get("id") or "") == did:
                    return d.get("token") or ""
        # ② 老记录没有 device_id，退回按令牌摘要对。
        hint = str(rec.get("token_hint") or "")
        if hint:
            for d in raw:
                try:
                    if str(MOB.mask_token(d.get("token")) or "") == hint:
                        return d.get("token") or ""
                except Exception:
                    continue
    # ③ 兜底：先看「此刻在线」那台（内存 _LIVE 每请求都刷），再看最近来过的一台。
    try:
        live = MOB._LIVE
    except Exception:
        live = {}
    pool = [d for d in raw if MOB.is_online(d)] or raw
    pool.sort(key=lambda d: float(live.get(d.get("id"), d.get("last_seen") or 0) or 0),
              reverse=True)
    return pool[0].get("token") or ""


def phone_items(device: str, refresh: bool = True, kind: str = None) -> dict:
    """手机素材清单：在线就顺手刷新一份缓存，离线只吐缓存（这就是「离线保留预览」）。

    kind: 只看某一类（image/video/audio/document）。
      ★ 顶栏那几个「全部 / 视频 / 图片 / 音频 / 文档」原来对**手机来源压根不生效** ——
        `/v1/browse` 走到手机这一支时把 `body["kind"]` 丢了，直接吐整份清单，
        于是切「视频」还是 9 项、跟「全部」一模一样（角标却是对的 1 个，看着更困惑）。
        这里补上过滤，和 `browse_all()` 那边的 SQL `WHERE kind=?` 对齐。
    """
    rec = _phone_sources().get(device)
    if not rec:
        return {"error": "没有这台手机", "items": []}
    d = _phone_cache_dir(device)
    cache = os.path.join(d, "manifest.json")
    online = (time.time() - float(rec.get("last_seen") or 0)) < PHONE_SOURCE_TTL
    data = None
    err = None
    if online and refresh:
        try:
            data = _phone_req(rec, "/v1/phone/manifest")
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            data = None
            err = str(e)
    else:
        err = "离线"
    if data is None:
        try:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
            err = err if online else "离线（显示的是缓存）"
        except Exception:
            return {"error": "这台手机还没缓存过清单", "items": []}
    items = data.get("items") or []
    out = []
    for it in items:
        # ★ 「视频 / 图片 / 音频 / 文档」这类筛选在手机来源上也要生效（见上面 kind 的说明）
        if kind and (it.get("kind") or "") != kind:
            continue
        w, h = it.get("width") or 0, it.get("height") or 0
        meta = {"width": w, "height": h}
        if it.get("duration"):
            meta["duration"] = it["duration"]
        out.append({
            # ★ 路径统一成「虚拟路径」：phone:<设备id>|<手机内路径>。
            #   前端拿它去 /v1/thumb、/v1/file、/v1/detail，电脑就知道该转手问谁要。
            "path": "phone:%s|%s" % (device, it.get("path")),
            "phone_path": it.get("path"),
            "name": it.get("name"), "kind": it.get("kind"),
            # 手机那边的智能结果（打标描述 / 转写 / 时间轴）也带过来，电脑侧能显示
            "note": it.get("note") or "",
            "size": it.get("size") or 0, "mtime": it.get("mtime") or 0,
            "score": 0, "chunks": 0, "exists": True,
            "why": it.get("note") or "",
            "thumb": "/v1/phone/thumb?device=%s&path=%s" % (urllib.parse.quote(device),
                                                            urllib.parse.quote(it.get("path") or "")),
            "meta": meta,
            "source": {"kind": "phone", "device": device, "name": rec.get("name") or "手机"},
        })
    return {"count": len(out), "items": out, "error": err}


# ---------- 「外派任务」：手机把打标 / 转写 / 对齐派给电脑 ----------
# 为什么要外派：手机本机素材的 AI 能力得靠外呼 API（用户自己的 key），而电脑上
# 已经有现成的视觉模型、语音识别、强制对齐三套管线。用户的要求是「派给电脑」，
# 而且**外派任务优先** —— 电脑把手上正在跑的那一个做完，立刻转去干外派的活。
#
# 实现：一张任务表（落盘），闲置线程每轮**在最前面**先看它一眼（不看空闲时长、
# 不看批量上限），取到就干。原件按需从手机拉过来缓存，算完把结果写回手机。

PHONE_JOB_FILE = "phone_jobs.json"
PHONE_JOB_MAX = 200            # 任务表最多留这么多条（老的自动丢，免得越攒越大）


def _phone_jobs_path() -> str:
    return os.path.join(P.DATA_DIR, PHONE_JOB_FILE)


def phone_jobs_load() -> dict:
    try:
        with open(_phone_jobs_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def phone_jobs_save(t: dict):
    try:
        os.makedirs(P.DATA_DIR, exist_ok=True)
        if len(t) > PHONE_JOB_MAX:
            # 先丢最老的「已结束」的，还没干的留着
            old = sorted([k for k, v in t.items() if v.get("status") in ("done", "failed")],
                         key=lambda k: t[k].get("created") or 0)
            for k in old[:max(0, len(t) - PHONE_JOB_MAX)]:
                t.pop(k, None)
        with open(_phone_jobs_path(), "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False)
    except Exception as e:
        print(f"[外派] 任务表写不进去：{e}")


def phone_job_add(device: str, asset_path: str, what: str, name: str = "",
                  carry: str = "") -> dict:
    """手机派一件活给电脑。what = tag | asr | align。

    `carry` 是手机顺手带过来的**已有转写原文**（对齐要用）：手机素材不进电脑的库，
    不带这一段，外派对齐永远只能回「先转写再对齐」。
    """
    what = what if what in ("tag", "asr", "align") else "tag"
    if not device or not asset_path:
        return {"error": "要 device 和 path"}
    t = phone_jobs_load()
    # 同一件素材同一种活只留一条：手机那边点两下不会派两遍
    for jid, j in t.items():
        if (j.get("device") == device and j.get("path") == asset_path
                and j.get("what") == what and j.get("status") in ("pending", "running")):
            return {"id": jid, "status": j.get("status"), "already": True}
    jid = uuid.uuid4().hex[:12]
    t[jid] = {"id": jid, "device": device, "path": asset_path, "name": name or os.path.basename(asset_path),
              "what": what, "status": "pending", "created": time.time(),
              "started": None, "finished": None, "text": "", "message": ""}
    if carry and str(carry).strip():
        t[jid]["carry"] = str(carry).strip()[:200000]
    phone_jobs_save(t)
    op_log("外派任务", f"收到：{t[jid]['name']} · {'打标' if what=='tag' else '转写' if what=='asr' else '对齐'}")
    return {"id": jid, "status": "pending"}


def phone_jobs_view(device: str = "") -> dict:
    t = phone_jobs_load()
    rows = [v for v in t.values() if not device or v.get("device") == device]
    rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return {"jobs": rows[:100], "pending": len([r for r in rows if r.get("status") == "pending"]),
            "running": [r for r in rows if r.get("status") == "running"][:1]}


def phone_job_take() -> dict | None:
    """取一件待办，标成 running（外派优先靠调用方把它放在最前面）。"""
    t = phone_jobs_load()
    pend = [v for v in t.values() if v.get("status") == "pending"]
    if not pend:
        return None
    pend.sort(key=lambda r: r.get("created") or 0)
    j = pend[0]
    j["status"] = "running"
    j["started"] = time.time()
    phone_jobs_save(t)
    return j


def _sniff_ext(blob: bytes) -> str:
    """从文件头猜后缀：手机那边给的路径往往没有扩展名（相册 localIdentifier）。"""
    if not blob:
        return ".bin"
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    if len(blob) > 12 and blob[4:8] == b"ftyp":
        brand = blob[8:12]
        return ".mp4" if brand[:3] in (b"iso", b"mp4", b"M4V", b"MSV", b"avc") else ".mov"
    if blob[:4] == b"fLaC":
        return ".flac"
    if blob[:3] == b"ID3" or blob[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if blob[:4] in (b"\x1aE\xdf\xa3",):
        return ".mkv"
    if blob[4:8] == b"ftyp" or blob[:4] == b"OggS":
        return ".ogg"
    return ".bin"


def _phone_orig_dir(device: str) -> str:
    d = os.path.join(_phone_cache_dir(device), "orig")
    os.makedirs(d, exist_ok=True)
    return d


def _phone_orig_key(device: str, asset_path: str) -> str:
    import hashlib
    return hashlib.sha1(("%s|%s" % (device, asset_path)).encode()).hexdigest()[:16]


def _phone_orig_ext(device: str, asset_path: str, name: str = "") -> str:
    """后缀要从「文件名」里取，不能从 asset_path 里取 —— 相册的 localIdentifier
    形如 xxx/L0/001，splitext 什么都取不到，落地成 .bin 后转写/打标全都不认（踩过）。"""
    ext = os.path.splitext(name or "")[1].lower()[:12]
    if not ext:
        row = phone_one(device, asset_path) or {}
        ext = os.path.splitext(row.get("name") or "")[1].lower()[:12]
    return ext or ".bin"


def phone_original_path(device: str, asset_path: str, name: str = "") -> str | None:
    """这台电脑上已经缓存好的那份原件（没有就返回 None，**不会**去回手机）。

    ★ 不能只认「按名字推出来的那个后缀」：相册素材的文件名常常没有扩展名
      （`05_成长_549`），推出来是 `.bin`，下完被 `_fix_orig_ext()` 嗅探改名成 `.jpg`
      —— 于是按 `.bin` 找就永远找不到那份明明在盘上的原件，用户会发现「同步过了
      却还要连手机」。所以最后按 key 前缀 glob 兜一圈，`.part` 不算。
    """
    import glob as _glob
    ext = _phone_orig_ext(device, asset_path, name)
    dst = os.path.join(_phone_orig_dir(device), _phone_orig_key(device, asset_path) + ext)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    for cand in sorted(_glob.glob(os.path.join(
            _phone_orig_dir(device), _phone_orig_key(device, asset_path) + ".*"))):
        if cand.endswith(".part"):
            continue
        try:
            if os.path.getsize(cand) > 0:
                return cand
        except OSError:
            continue
    return None


class _PhoneSyncStopped(Exception):
    """用户点了「停下」：立刻中止当前这一件（.part 留着，下次接着下）。"""


def _phone_download(rec: dict, asset_path: str, dst: str,
                    progress=None, timeout: float = 600.0, should_stop=None) -> int:
    """把手机上的原件**流式**下载到 dst，返回字节数。

    三个讲究（都是踩出来的）：
      1. 先写 `dst + ".part"`，下完才改名 —— 半截文件绝不能以正式名存在，
         否则下次「已经缓存过了」的判断会拿它当成品（详情面板/播放器会读坏文件）。
      2. 支持 Range 续传：`.part` 已经有 N 字节就从 N 接着要，手机那边
         `PhoneServer` 的 `/v1/phone/file` 认 Range（206 + Content-Range）。
         大视频断一次就得从头来，那是拿用户的流量开玩笑。
      3. 边收边回调 progress(已收, 总长) —— 一条 1.2 GB 的 4K 视频要下几分钟，
         没有进度用户只会以为卡死了。总长优先取 Content-Range，其次 Content-Length。
      4. `should_stop()` 在**每个 256KB 块之间**问一次 —— 确认框里明明写着
         「可以在进度条上随时停下」，那就不能等一条 1.2 GB 的视频自己下完才理人。
         每次调用都问、而不是每隔几 MB 问：这里是本地局域网，一次调用的开销
         远小于 256KB 的传输，没必要为了省它让「停下」迟一拍。
    """
    import urllib.request
    part = dst + ".part"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    url = "http://%s:%s/v1/phone/file?path=%s" % (
        rec.get("host"), rec.get("port"), urllib.parse.quote(asset_path))
    req = urllib.request.Request(url)
    tok = _phone_token(rec.get("id") or "")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if have and resp.status != 206:
            have = 0                      # 手机不支持续传（或 Range 被忽略）→ 从头来
        cr = resp.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                total = 0
        if not total:
            try:
                total = have + int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
        mode = "ab" if have else "wb"
        with open(part, mode) as f:
            while True:
                if should_stop and should_stop():
                    raise _PhoneSyncStopped()
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                if progress:
                    try:
                        progress(have, total)
                    except Exception:
                        pass
    if total and have != total:
        raise IOError("没下完（%d / %d 字节）" % (have, total))
    os.replace(part, dst)
    return have


# 最近一次拉原件为什么失败 —— 只给日志/回执用，方便把「手机可能不在线了」
# 这句万能话拆成「到底是哪一步断了」（令牌不对 / 404 / 连不上）。
_phone_last_err: str = ""


def phone_fetch_original(device: str, asset_path: str, name: str = "") -> str | None:
    """把手机上的原件抓到电脑缓存里，返回本地路径（已经抓过就直接用）。"""
    global _phone_last_err
    _phone_last_err = ""
    ext = _phone_orig_ext(device, asset_path, name)
    dst = os.path.join(_phone_orig_dir(device), _phone_orig_key(device, asset_path) + ext)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    # ★ 换掉 `_phone_sources().get(device)`（范老板 2026-10-13）：
    #   表的键从「手机自报的 identifierForVendor」变成了**来源 IP**，
    #   而前端传来的 `device` 是 `phone_sources_list()` 给出的 `r["id"]`
    #   —— 也就是那个 IP 键，所以这里其实仍然查得到。
    #   但手机 `/v1/phone/bye` 报的是机器号，`_phone_find` 能两边都认，
    #   统一走它，省得以后又冒出「按机器号查表查不到」的同类 bug。
    # ★ 2026-10-18：这段原先被缩进到上面那个 `return dst` 后面，成了**死代码** ——
    #   `rec` 永远是 UnboundLocalError，外派任务 100% 失败（手机上看是
    #   「拉原件失败：手机可能不在线了」）。挪回函数级。
    key, rec0 = _phone_find(_phone_sources(), str(device or ""))
    rec = rec0 if isinstance(rec0, dict) else None
    if not rec:
        _phone_last_err = ("电脑还没登记这台手机 —— 手机得先连上它自报一次家门"
                           "（device=%s）" % (device or "?"))
        print(f"[外派] 拉原件失败 {asset_path}：{_phone_last_err}")
        return None
    # ★ 2026-10-18：手机偶尔会回一个 500（看着像瞬间抽风，重试就好），
    #   而且以前只记「HTTP Error 500」，把手机那边写的原因丢掉了 —— 把响应体带上，
    #   下次再犯能直接看出是「读原件失败」还是别的事。
    import urllib.error
    for attempt in (0, 1):
        try:
            _phone_download(rec, asset_path, dst)
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:200].decode("utf-8", "replace").strip()
            except Exception:
                pass
            _phone_last_err = "%s%s" % (e, (" · " + body) if body else "")
            if e.code >= 500 and attempt == 0:
                time.sleep(0.8)
                continue
            print(f"[外派] 拉原件失败 {asset_path}：{_phone_last_err}")
            return None
        except Exception as e:
            _phone_last_err = "%s: %s" % (type(e).__name__, e)
            print(f"[外派] 拉原件失败 {asset_path}：{_phone_last_err}")
            return None
    return _fix_orig_ext(device, asset_path, dst, ext)


def _fix_orig_ext(device: str, asset_path: str, dst: str, ext: str) -> str:
    """下完的 `.bin` 闻一下文件头，改成对的后缀（返回最终路径）。

    ★ 为什么要这一步：相册素材的 `name` 往往没有扩展名（`05_成长_549`），
      `_phone_orig_ext()` 只能给出 `.bin`。而电脑端后面的打标/转写/播放全按扩展名
      分派（`_sniff_ext` 的注释里就写着「落地成 .bin 后转写/打标全都不认（踩过）」）。
      原件同步是**整批**下几百件，漏了这一步就是几百件打不开。
    """
    if ext != ".bin":
        return dst
    try:
        with open(dst, "rb") as f:
            sniffed = _sniff_ext(f.read(64))
    except Exception:
        return dst
    if sniffed == ".bin":
        return dst
    new_dst = os.path.join(_phone_orig_dir(device),
                           _phone_orig_key(device, asset_path) + sniffed)
    try:
        os.replace(dst, new_dst)
        return new_dst
    except Exception:
        return dst


# ---------- 「原件同步」：把手机相册的原件整批拉到电脑 ----------
# 用户 2026-10-11 定的第二阶段：先「清单 + 缩略图」，之后再说原文件。
# 原件拉到电脑有两个好处：① 离线也能打开/播放（不必手机在线、不必同一 Wi-Fi）；
# ② 电脑可以直接对着本地文件跑预览、抽帧，不再每次回手机要。
# 代价是占磁盘，所以**必须是用户主动点的**（不自动全量下），而且进度要看得见、
# 中途能停、能单独清。

PHONE_SYNC = {}                 # device -> 进度字典
_PHONE_SYNC_LOCK = threading.Lock()


def phone_sync_state(device: str = "") -> dict:
    with _PHONE_SYNC_LOCK:
        if device:
            return dict(PHONE_SYNC.get(device) or {})
        return {k: dict(v) for k, v in PHONE_SYNC.items()}


def phone_cache_stats(device: str) -> dict:
    """这台手机在电脑上占了多少地方（缩略图 / 原件分开算）。"""
    d = _phone_cache_dir(device)
    out = {"thumbs": 0, "thumb_bytes": 0, "orig": 0, "orig_bytes": 0, "part_bytes": 0}
    for sub, nk, bk in (("thumbs", "thumbs", "thumb_bytes"), ("orig", "orig", "orig_bytes")):
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        for fn in os.listdir(p):
            fp = os.path.join(p, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if fn.endswith(".part"):
                out["part_bytes"] += sz
                continue
            out[nk] += 1
            out[bk] += sz
    for k in ("thumb_bytes", "orig_bytes", "part_bytes"):
        out[k + "_mb"] = round(out[k] / 1048576, 2)
    return out


def phone_sync_start(device: str) -> dict:
    """开始（或接着）把整台手机的本机素材原件拉到电脑。已经在跑了就直接返回现有进度。"""
    rec = _phone_sources().get(device)
    if not rec:
        return {"error": "没有这台手机"}
    if (time.time() - float(rec.get("last_seen") or 0)) >= PHONE_SOURCE_TTL:
        return {"error": "手机不在线 —— 原件同步要手机连着（同一 Wi-Fi）"}
    with _PHONE_SYNC_LOCK:
        cur = PHONE_SYNC.get(device)
        if cur and cur.get("running"):
            return {"ok": True, "already": True, **cur}

    def _run():
        items = (phone_items(device) or {}).get("items") or []
        # 只需要知道「哪几条」和「叫什么」；字节数从手机现拉，清单里没有总长
        todo = [(it.get("phone_path") or "", it.get("name") or "", it.get("size") or 0)
                for it in items]
        with _PHONE_SYNC_LOCK:
            PHONE_SYNC[device] = {"running": True, "total": len(todo), "done": 0,
                                  "skipped": 0, "failed": 0, "bytes": 0,
                                  "total_bytes": sum(t[2] for t in todo),
                                  "current": "", "note": "", "started": time.time(),
                                  "finished": 0, "errors": []}
        st = PHONE_SYNC[device]
        for ap, nm, _sz in todo:
            if not st.get("running"):
                st["note"] = "已停"
                break
            if not ap:
                continue
            if phone_original_path(device, ap, nm):
                st["skipped"] += 1
                st["done"] += 1
                continue
            st["current"] = nm
            try:
                r = _phone_sources().get(device)
                if not r:
                    raise IOError("这台手机被移除了")
                dst = os.path.join(_phone_orig_dir(device),
                                   _phone_orig_key(device, ap) + _phone_orig_ext(device, ap, nm))

                def _prog(got, tot, _ap=ap):
                    with _PHONE_SYNC_LOCK:
                        st["bytes"] = st.get("_base_bytes", 0) + got
                        if tot:
                            st["current_total"] = tot

                st["_base_bytes"] = st["bytes"]
                n = _phone_download(r, ap, dst, progress=_prog,
                                    should_stop=lambda: not st.get("running"))
                st["bytes"] = st.get("_base_bytes", 0) + n
                st["_base_bytes"] = st["bytes"]
                # 名字里没有扩展名的（相册素材绝大多数如此）下完要闻一下再改名，
                # 否则整批都是 .bin —— 后面打标/转写/播放全都不认
                _fix_orig_ext(device, ap, dst, os.path.splitext(dst)[1].lower())
                st["done"] += 1
            except _PhoneSyncStopped:
                # 用户自己按的「停下」，这一件是**被打断**不是**失败**：
                # 别计 failed、别弹红字 —— 那会让人以为自己点错了。
                # .part 留在盘上，下次「接着下」正好从断点续。
                # 已经收到的字节照算（进度到哪儿就是哪儿），不回退。
                st["note"] = "已停"
                break
            except Exception as e:
                st["failed"] += 1
                if len(st["errors"]) < 20:
                    st["errors"].append("%s：%s" % (nm, e))
                print(f"[原件同步] {nm} 失败：{e}")
        with _PHONE_SYNC_LOCK:
            st["running"] = False
            st["current"] = ""
            st["finished"] = time.time()
            if not st.get("note"):
                st["note"] = "完成" if not st["failed"] else "完成（%d 个失败）" % st["failed"]
        op_log("原件同步", f"{rec.get('name') or device} · {st['done']}/{st['total']}"
                            f" · {st['bytes'] / 1048576:.1f} MB"
                            + (f" · {st['failed']} 个失败" if st["failed"] else ""))

    threading.Thread(target=_run, daemon=True, name="phone-sync").start()
    time.sleep(0.1)
    return {"ok": True, **phone_sync_state(device)}


def phone_sync_stop(device: str) -> dict:
    with _PHONE_SYNC_LOCK:
        st = PHONE_SYNC.get(device)
        if st:
            st["running"] = False
    return {"ok": True, **phone_sync_state(device)}


def phone_sync_reset(device: str) -> dict:
    """清掉这台的同步记录（进度归零，不动已经下好的文件）。"""
    with _PHONE_SYNC_LOCK:
        PHONE_SYNC.pop(device, None)
    return {"ok": True}


def phone_orig_clear(device: str) -> dict:
    """删掉这台手机在电脑上的**原件**缓存（缩略图/清单不动，手机上的东西更不动）。"""
    with _PHONE_SYNC_LOCK:
        st = PHONE_SYNC.get(device)
        if st and st.get("running"):
            st["running"] = False
    d = os.path.join(_phone_cache_dir(device), "orig")
    n, total = 0, 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            try:
                total += os.path.getsize(fp)
                os.remove(fp)
                n += 1
            except OSError:
                pass
    return {"ok": True, "removed": n, "mb": round(total / 1048576, 2)}


def _stub_seed_asr(path: str, text: str) -> bool:
    """把手机上已经有的转写原文临时塞进「借来的」索引行。

    ★ 2026-10-18（范老板第 ① 条「外派对齐全部失败：这个素材还没有转写文字」）：
      手机那边可能早就用**自己的 API** 转写过了（自身转写），但电脑这边的库里没有
      这条素材（手机素材本来就不进电脑库），于是对齐一开就撞「先转写再对齐」。
      手机的转写原文随任务一起送过来，这里先落到临时行上，对齐就能直接吃。
    """
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            row = con.execute("SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                              " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
            if not row:
                return False
            meta = json.loads(row[0]) if row[0] else {}
            if (meta.get("asr_text") or "").strip():
                return False
            meta["asr_text"] = str(text or "")
            con.execute("UPDATE items SET meta=? WHERE path=? AND chunk_idx<?",
                        (json.dumps(meta, ensure_ascii=False), path, ix.AI_TAG_BASE))
            con.commit()
            return True
        finally:
            con.close()
    except Exception as e:
        print("[外派] 种入转写原文失败：%s" % e)
        return False


def _phone_stub_add(path: str) -> bool:
    """手机素材在电脑库里没有行，打标/转写都要求「先有索引行」。

    这里临时借一行给它干活（干完就删）—— 不然外派永远只能回一句「先刷新索引」，
    而手机素材本来就不该进电脑的库。
    """
    ext = os.path.splitext(path)[1].lower()
    kind = ("image" if ext in ix.IMAGE_EXT else
            "video" if ext in ix.VIDEO_EXT else
            "audio" if ext in ix.AUDIO_EXT else "")
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            n = con.execute("SELECT COUNT(*) FROM items WHERE path=?", (path,)).fetchone()[0]
            if n:
                return False
            con.execute("INSERT INTO items(path, kind, chunk_idx, meta, mtime, indexed_at)"
                        " VALUES(?,?,0,?,?,?)",
                        (path, kind, json.dumps({"phone": True}),
                         os.path.getmtime(path), time.time()))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"[外派] 借索引行失败：{e}")
        return False
    return True


def _phone_stub_del(path: str, borrowed: bool):
    """把借来的行还回去（顺手把刚写进去的标签列也清掉）。"""
    if not borrowed:
        return
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            con.execute("DELETE FROM items WHERE path=?", (path,))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def phone_job_run(job: dict) -> dict:
    """干一件外派活：拉原件 → 用电脑自己的管线算 → 结果写回手机。"""
    did, ap, what = job.get("device"), job.get("path"), job.get("what")
    st = load_settings()
    local = phone_fetch_original(did, ap, job.get("name") or "")
    if not local:
        return phone_job_finish(job, ok=False, text="",
                                message="拉原件失败：手机可能不在线了（%s）"
                                        % (_phone_last_err or "没登记这台手机"))
    borrowed = _phone_stub_add(local)      # 借一行索引给它（干完就还）
    try:
        if what == "tag":
            r = ai_tag_one(local, st)
            text = (r.get("description") or "").strip()
            tags = r.get("tags") or []
            if tags:
                text = (text + "\n" + "、".join(str(x) for x in tags)).strip()
        elif what == "asr":
            r = asr_transcribe_one(local, st)
            text = (r.get("text") or "").strip()
        else:
            # 手机把自己的转写原文一起送过来时，先落到临时行上 —— 否则电脑这边
            # 一条没有转写的手机素材，对齐永远只会回「先转写再对齐」。
            carried = str(job.get("carry") or "").strip()
            if carried and not _asr_text_of(local):
                _stub_seed_asr(local, carried)
            r = align_one(local, st)
            text = "%d 条时间点" % len(r.get("items") or []) if r.get("ok") and not r.get("skipped") else ""
            if r.get("skipped"):
                text = ""
        ok = bool(r.get("ok"))
        msg = r.get("message") or r.get("error") or ""
        # 转写/对齐的结果落在这台电脑的库里（按缓存路径），手机只需要拿到文本
        if what == "asr" and ok and not text:
            text = _asr_text_of(local) or ""
        if what == "align" and ok:
            # 对齐是「什么时候说」：手机那边要的是**结构化时间轴**（能逐字），
            # 不是 mm:ss 的人读文本。
            text = _align_items_json(local) or _align_text_of(local) or text
        _phone_stub_del(local, borrowed)
        return phone_job_finish(job, ok=ok, text=text, message=msg)
    except Exception as e:
        _phone_stub_del(local, borrowed)
        return phone_job_finish(job, ok=False, text="", message="电脑这边出错：%s" % e)
    finally:
        pass


def _asr_text_of(path: str) -> str:
    """从索引里把这台电脑刚转写出来的文本捞回来。"""
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        return (json.loads(row[0]).get("asr_text") or "").strip() if row and row[0] else ""
    except Exception:
        return ""


def _align_items_json(path: str) -> str:
    """对齐时间轴，按电脑端 `align_items` 的原始结构发给手机：
    `[[词, 起, 止, 在转写原文里的字符偏移], …]`。

    ★ 2026-10-18（范老板「外派对齐成功了，手机播放面板还是不逐字」）：以前这里
      发的是 `"mm:ss 文字"` 的**人读文本**，手机收到只能当一段字符串存着，
      `Lyrics` 解析不出词/时间 → 逐字永远灰。手机要的是结构化数组。
    """
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        if not (row and row[0]):
            return ""
        meta = json.loads(row[0])
        items = meta.get("align_items") or (meta.get("align") or {}).get("items") or []
        # 只留 [词, 起, 止, 偏移] 四列（老数据可能是 dict，顺手规整）
        out = []
        for it in items:
            if isinstance(it, dict):
                out.append([it.get("word") or it.get("text") or "",
                            round(float(it.get("start") or 0), 3),
                            round(float(it.get("end") or 0), 3),
                            int(it.get("offset") or 0)])
            elif isinstance(it, (list, tuple)) and len(it) >= 4:
                out.append([str(it[0]),
                            round(float(it[1] or 0), 3),
                            round(float(it[2] or 0), 3),
                            int(it[3] or 0)])
        return json.dumps(out, ensure_ascii=False) if out else ""
    except Exception:
        return ""


def _align_text_of(path: str) -> str:
    """逐字时间轴：拼成「mm:ss 文字」的多行文本，手机那边可直接显示。"""
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        if not (row and row[0]):
            return ""
        meta = json.loads(row[0])
        items = (meta.get("align") or {}).get("items") or meta.get("align_items") or []
        out = []
        for it in items[:400]:
            t = float(it.get("start") or 0)
            out.append("%02d:%02d %s" % (int(t // 60), int(t % 60), it.get("text") or ""))
        return "\n".join(out)
    except Exception:
        return ""


def phone_job_finish(job: dict, ok: bool, text: str, message: str = "") -> dict:
    """收尾：写回手机 + 更新任务表。"""
    did, ap, what = job.get("device"), job.get("path"), job.get("what")
    note_kind = {"tag": "ai", "asr": "asr", "align": "align"}.get(what, what)
    wrote = False
    try:
        # ★ 2026-10-18（范老板第 ② 条「外派成功后素材不显示标签」）：这里原来直接
        #   `_phone_sources().get(did)` —— 但那张表的**键是来源 IP**，`did` 是手机自报的
        #   identifierForVendor，两边不是一个编号体系，查不到就**静默不写回**：
        #   电脑上任务显示 done、手机上什么标签都没有。统一走 `_phone_find`。
        _key, rec = _phone_find(_phone_sources(), did)
        if rec:
            _phone_req(rec, "/v1/phone/note", method="POST",
                       body={"path": ap, "what": note_kind, "text": text,
                             "ok": bool(ok), "message": message}, timeout=30.0)
            wrote = True
        else:
            message = (message + " / 没找到这台手机的连接记录，结果没能写回手机（%s）" % did).strip(" /")
    except Exception as e:
        message = (message + " / 写回手机失败：%s" % e).strip(" /")
    t = phone_jobs_load()
    j = t.get(job.get("id"))
    if j:
        j["status"] = "done" if ok else "failed"
        j["finished"] = time.time()
        j["text"] = text[:20000]
        j["message"] = message
        j["wrote_back"] = wrote
        phone_jobs_save(t)
    op_log("外派任务", f"{job.get('name')} · {'完成' if ok else '失败'}"
                        + (f" · {message}" if message else ""),
           level="info" if ok else "warn")
    return {"ok": ok, "text": text, "message": message, "wrote_back": wrote}


def phone_one(device: str, asset_path: str) -> dict | None:
    """取手机清单里的一条（/v1/detail 用；不回手机，读缓存即可）。"""
    data = phone_items(device, refresh=False)
    for it in data.get("items") or []:
        if (it.get("phone_path") or it.get("path")) == asset_path:
            it = dict(it)
            it["ai_description"] = it.get("why") or ""
            return it
    return None


def phone_thumb(device: str, path: str) -> bytes | None:
    """缩略图：电脑本地缓存优先；没有且手机在线就抓一张存下来。"""
    rec = _phone_sources().get(device)
    if not rec:
        return None
    import hashlib
    key = hashlib.sha1(path.encode("utf-8")).hexdigest()[:20] + ".jpg"
    dest = os.path.join(_phone_cache_dir(device), "thumbs", key)
    if os.path.exists(dest):
        try:
            with open(dest, "rb") as f:
                return f.read()
        except Exception:
            pass
    online = (time.time() - float(rec.get("last_seen") or 0)) < PHONE_SOURCE_TTL
    if not online:
        return None
    try:
        blob = _phone_req(rec, "/v1/phone/thumb?path=" + urllib.parse.quote(path), raw=True)
        with open(dest, "wb") as f:
            f.write(blob)
        return blob
    except Exception:
        return None


def phone_search(device: str, body: dict, timeout: float = 60.0) -> dict:
    """把电脑这边的查询原样转给手机，让它用自己的 EG2 算（路线 A 的核心）。"""
    rec = _phone_sources().get(device)
    if not rec:
        return {"error": "没有这台手机", "items": []}
    if (time.time() - float(rec.get("last_seen") or 0)) >= PHONE_SOURCE_TTL:
        return {"error": "手机不在线", "items": []}
    try:
        res = _phone_req(rec, "/v1/phone/search", method="POST", body=body, timeout=timeout)
    except Exception as e:
        return {"error": "问手机失败：%s" % e, "items": []}
    items = []
    for it in res.get("items") or []:
        it = dict(it)
        it["thumb"] = "/v1/phone/thumb?device=%s&path=%s" % (urllib.parse.quote(device),
                                                            urllib.parse.quote(it.get("path") or ""))
        it["source"] = {"kind": "phone", "device": device, "name": rec.get("name") or "手机"}
        it["why"] = it.get("why") or "手机上 EG2 命中"
        items.append(it)
    return {"used": res.get("used"), "tier": res.get("tier"), "total": len(items),
            "took_ms": res.get("took_ms"), "items": items}
PHONE_SOURCE_TTL = 60           # 手机「在线」的判定窗口（秒）
# ★ 为什么从 180 改成 60（范老板 2026-10-13）：
#   他定的状态灯规则是「断开 1 分钟后熄灭」。TTL 就是这个「1 分钟」。
#   配套改动有两处，缺一不可：
#     ① 手机侧心跳从 60 秒改成 20 秒（`AppModel.startAnnouncing`）——
#        心跳必须明显快于 TTL，否则网络稍抖一下，60 秒里没送到，灯就灭了又亮；
#     ② 界面侧加了 5 秒一次的轮询（`ui.html` 的 `pollPhoneSources`）——
#        `online` 是**按当前时间算**的，没人来问就不会重画，灯永远不会自己灭。
#
#: 手机来源在 `phone_sources` 里的键：**用这条 TCP 连接的来源 IP**。
def _phone_host_key(host: str) -> str:
    """把来源地址收拾成可以当键用的形状。

    - `::ffff:127.0.0.1`（IPv6 套 IPv4）→ `127.0.0.1`，否则同一台机器
      换个监听方式就会凭空多出一个来源。
    - 去掉 IPv6 的方括号。
    """
    h = str(host or "").strip()
    if h.lower().startswith("::ffff:"):
        h = h[7:]
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h or "phone"


def _phone_sources() -> dict:
    return dict(load_settings().get("phone_sources") or {})


def _phone_find(table: dict, want: str, host: str = "") -> tuple:
    """按「键 / 自报的机器号 / 来源 IP」任一认出是哪条记录，返回 (key, rec)。

    ★ 为什么不能只 `table.get(want)`（范老板 2026-10-13 的 ①）：
      表的键现在是 IP，而手机 `/v1/phone/bye` 里报的是自己的
      identifierForVendor。两边不是一个编号体系，直接查必然查不到，
      断开时电脑这边就不知道该把谁标成离线。
    """
    want = str(want or "").strip()
    if want and want in table:
        return want, table[want]
    for k, r in table.items():
        if not isinstance(r, dict):
            continue
        if want and str(r.get("device_id") or "") == want:
            return k, r
        if want and want in (r.get("aliases") or []):
            return k, r
    if host:
        for k, r in table.items():
            if isinstance(r, dict) and str(r.get("host") or "") == host:
                return k, r
    return "", None


def phone_announce(body: dict, host: str, dev: dict | None = None) -> dict:
    """手机连上后自报家门：登记成电脑侧的一个「手机来源」。

    ★ 表的键 = **这条 TCP 连接的来源 IP**（范老板 2026-10-13 定的）。
      原因：以前用手机自报的 identifierForVendor 当键，那东西一重装 App 就变，
      「改个手机名字再连一次」也会在「全部来源」里多长出一个文件夹来。
      IP 是电脑自己从连接里读的（不信 body 里自报的 host，免得被伪造），
      改名字不会换 IP，所以同名同机只会有一条记录。
      端口和件数是手机自己的事，照收。
    ★ 但光靠 IP 不够：DHCP 续租会把 IP 换掉，那就又变成两个文件夹了。
      所以每次自报都顺手按 `device_id` 找一遍**旧键**（换过 IP 的老记录），
      找到就整体搬过来，缓存目录一起搬 —— 素材还认得出来。
    """
    if not isinstance(body, dict):
        return {"error": "请求体不是 JSON"}
    host_key = _phone_host_key(host)
    devid = str(body.get("id") or "").strip()
    s = load_settings()
    table = dict(s.get("phone_sources") or {})

    # ① 先看这个 IP 上是不是已经有记录。
    key = host_key
    old = table.get(key)
    if isinstance(old, dict):
        # 同一个 IP 上换了一台**别的**手机（DHCP 把地址转手了），
        # 而且老那台还活着 —— 不能覆盖，给它加个后缀另立一条。
        old_id = str(old.get("device_id") or "")
        still_on = (time.time() - float(old.get("last_seen") or 0)) < PHONE_SOURCE_TTL
        # ★ 只有在「能证明是另一台」时才另立一条（范老板 2026-10-13）：
        #   `device_id` 是手机自报的 identifierForVendor（36 位带连字符的 UUID）才可信。
        #   早期版本这里存的是**配对号**（16 位 hex）或者干脆是空的 —— 拿它跟这台
        #   手机自报的 UUID 一比当然不相等，可是人根本没换，于是同一个地址上就多出
        #   一条 `IP#xxxx` 的重复文件夹。（测试 ⑪ 就是这个坑。）
        #   另外，配对令牌摘要对得上，也说明就是同一台，不必另立。
        hint_now0 = str(MOB.mask_token((dev or {}).get("token")) or "")
        old_looks_uuid = len(old_id) == 36 and old_id.count("-") == 4
        same_by_hint = bool(hint_now0 and str(old.get("token_hint") or "") == hint_now0)
        if (devid and old_id and old_id != devid and still_on
                and old_looks_uuid and not same_by_hint):
            key = "%s#%s" % (host_key, devid[:12])
    rec_old = table.get(key) if isinstance(table.get(key), dict) else {}

    # ★ 这一条记录到底是不是**同一台机器**？（范老板 2026-10-13）
    #   不能只看键：DHCP 会把一个 IP 转手给另一台手机。老那台已经离线时，
    #   上面不会加后缀，键就撞上了 —— 这时候若还继承 token_hint / first_seen，
    #   新手机就会拿到**老那台**的配对令牌摘要，`_phone_token` 认错设备、
    #   拉原件一路 401（现象是「手机可能不在线了」，其实是认错人了）。
    #   老记录没有 device_id 的年代（早期版本）没法判断，按同机处理。
    _old_id = str(rec_old.get("device_id") or "")
    same_machine = bool(rec_old) and (not devid or not _old_id or _old_id == devid)

    # ② 认人：自报的机器号对上哪条记录就继承它的历史（含换过 IP 的那条）。
    aliases = list(rec_old.get("aliases") or []) if same_machine else []
    for k, r in list(table.items()):
        if not isinstance(r, dict) or k == key:
            continue
        if devid and str(r.get("device_id") or "") == devid:
            # 换 IP 了：旧记录整个搬过来，缓存目录也跟着搬。
            if k not in aliases:
                aliases.append(k)
            if not same_machine or not rec_old:
                rec_old = dict(r)
                same_machine = True
            _phone_cache_adopt(k, key)
            del table[k]

    # ②b ★ 收拾旧版本留下的「按机器号（identifierForVendor）当键」的记录。
    #    范老板 2026-10-13 升级前的表里就是这种：键是 UUID、`device_id` 空着、
    #    名字还是同一台手机。键换成 IP 之后，这些老行**永远不会再被更新** ——
    #    它们会一直挂在侧栏里，看着就是「改了个名字又多出一个文件夹」，
    #    正是他要修掉的那个现象。
    #    判据（任一命中即同一台）：
    #      ⅰ) 旧键**就是**这台手机自报的机器号 —— 旧方案正是拿它当键，铁证；
    #      ⅱ) 配对令牌摘要对得上（`token_hint`）。
    #    都对不上就不敢动 —— 宁可留着，也不能误删用户的设备。
    if devid:
        hint_now = str(MOB.mask_token((dev or {}).get("token")) or "")
        for k, r in list(table.items()):
            if not isinstance(r, dict) or k == key:
                continue
            # 只认「旧式键」：36 位、4 个连字符的 UUID。
            if not (len(k) == 36 and str(k).count("-") == 4):
                continue
            if str(r.get("host") or "") and str(r.get("host")) != host:
                continue                       # 别的地址上那台，不动
            hit = (k == devid) or bool(hint_now and str(r.get("token_hint") or "") == hint_now)
            if not hit:
                continue
            if k not in aliases:
                aliases.append(k)
            if not same_machine or not rec_old:
                rec_old = dict(r)
                same_machine = True
            _phone_cache_adopt(k, key)
            del table[k]

    for a in (devid, host_key):
        if a and a != key and a not in aliases:
            aliases.append(a)

    rec = {
        "id": key,
        "name": str(body.get("name") or "手机")[:60],
        "host": host,
        "port": int(body.get("port") or 8235),
        "items": int(body.get("items") or 0),
        "indexed": int(body.get("indexed") or 0),
        "version": str(body.get("version") or "")[:24],
        "last_seen": time.time(),
        # ★ 这一台对应的配对记录（见 `_phone_token` 的注释）：
        #   手机自报的 id 是 identifierForVendor，和 mobile_devices 的 id
        #   不是一个编号体系，只能在这条请求里顺手记下来。
        "device_id": devid or str(rec_old.get("device_id") or ""),
        # ★ 只有确认是同一台机器才继承令牌摘要（见上面 same_machine 的注释）。
        "token_hint": str(MOB.mask_token((dev or {}).get("token")) or "")
                      or (str(rec_old.get("token_hint") or "") if same_machine else ""),
        "aliases": aliases[-8:],
        "first_seen": float(rec_old.get("first_seen") or 0) if same_machine else time.time(),
    }
    if not rec["first_seen"]:
        rec["first_seen"] = time.time()
    table[key] = rec
    # 顺手把同一个 IP 上早就凉透的重复记录清掉（换手机 / 重装留下的）。
    _phone_prune(table, host_key, time.time())
    save_settings({"phone_sources": table})

    # ★ 顺手把它的清单拉一份存缓存（后台线程，不拖慢 announce）：
    #   侧栏/顶栏角标要按类型数手机那批素材，没缓存就只能显示 0。
    #   ★★ 这段原来写在 `return` **后面**，是死代码（永远跑不到）——
    #      所以手机那批素材的类型数一直显示 0，点「只看视频」还会命中过期的缓存清单。
    #      挪到 return 之前才算真的接上。
    try:
        threading.Thread(target=lambda: phone_items(key, refresh=True),
                         daemon=True, name="phone-manifest").start()
    except Exception:
        pass

    # ★ 2026-10-13（范老板）：手机端「分享给朋友 / 转发朋友圈」攒的专业版额度，
    #   靠这条自报带上来 —— 用户什么都不用做，连上电脑就自动到账。
    #   这是**主路径**；手机没连电脑时走「凭证码」那条副路径（见 redeem_ticket）。
    share_r = None
    try:
        if dev:
            # ① 认人：配对过的手机再连一次，确保它在「已绑终端」名单里。
            #    （正常情况配对那一步已经绑了，这里兜的是配对记录被清过、
            #      或者从老版本升上来时名单还是空的那种。）
            #    ★ explicit=False：**用户主动解绑过的那台不补**。不然「解绑旧手机
            #      → 配新手机」永远走不通 —— 旧手机还在跑，20 秒一次的心跳就把
            #      它自己绑回来，新手机的配对立刻被挡。
            lic.bind_terminal(dev.get("id"), dev.get("name"), explicit=False)
            # ② 分享计数：用配对记录的 id 当键（不是手机自报的 UUID）——
            #    绑定名单和凭证码验签用的都是这个 id，三处必须是同一个。
            #    ★ 2026-10-13（下午·十一）起手机报的是**两档分开的次数**；
            #      老包只会带 `share_count` 那一个数 —— 当成 moments（30 天档）收下，
            #      反正未发布的包升上来时那次分享本来就是按 30 天给的。
            reported = None
            if body.get("share_friend") is not None or body.get("share_moments") is not None:
                reported = {"friend": int(body.get("share_friend") or 0),
                            "moments": int(body.get("share_moments") or 0)}
            elif int(body.get("share_count") or 0) > 0:
                reported = int(body.get("share_count") or 0)
            if reported is not None:
                share_r = lic.phone_share_sync(str(dev.get("id") or ""), reported)
                if share_r.get("credited"):
                    op_log("分享奖励", "手机分享 +%d 天电脑专业版（累计 %d 天）"
                           % (share_r.get("days_granted") or 0,
                              share_r.get("share_days_total") or 0))
            # （素材件数不用在这里报 —— 上面刚写进 `rec["items"]`，
            #   `license_status()` 直接读 phone_sources 那张表。）
    except Exception as e:
        op_log("分享奖励", "手机分享同步失败：%s" % e, level="warn")

    return {"ok": True, "ttl": PHONE_SOURCE_TTL, "name": rec["name"],
            "port": rec["port"], "id": key,
            **({"share": share_r} if share_r else {})}


def _phone_cache_adopt(old_key: str, new_key: str) -> None:
    """换键时把缓存目录一起搬过去，别让缩略图/清单白下。"""
    if not old_key or not new_key or old_key == new_key:
        return
    try:
        src = os.path.join(PHONE_CACHE_DIR,
                           "".join(c for c in str(old_key) if c.isalnum() or c in "-_")[:80])
        dst = os.path.join(PHONE_CACHE_DIR,
                           "".join(c for c in str(new_key) if c.isalnum() or c in "-_")[:80])
        if os.path.isdir(src) and not os.path.isdir(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
    except Exception:
        pass


def phone_bye(body: dict, host: str = "", dev: dict | None = None) -> dict:
    """手机**主动断开**时打个招呼：把 last_seen 归零，电脑侧立刻变「离线」。

    为什么要有这个（范老板 2026-10-13）：
      「在线」是 `last_seen` + 60 秒窗口算出来的（PHONE_SOURCE_TTL），而手机每
      20 秒自报一次。手机上点了「断开连接」，电脑这边什么都收不到，绿点会
      **继续亮满 60 秒**，看着像没断干净。有了这个端点，正常断开是即时的；
      那个 TTL 只留给「手机被系统杀掉 / 掉了 Wi-Fi」这类非正常掉线。
    """
    if not isinstance(body, dict):
        return {"error": "请求体不是 JSON"}
    did = str(body.get("id") or "").strip()
    if not did and not host:
        return {"error": "缺少 id"}
    table = dict(load_settings().get("phone_sources") or {})
    # ★ 表键是 IP，手机报的是 identifierForVendor —— 得按「键/机器号/IP」挨个认。
    key, rec = _phone_find(table, did, _phone_host_key(host))
    if not isinstance(rec, dict):
        return {"ok": True, "known": False}
    rec = dict(rec)
    # ★ 不能写 0：`ago = now - last_seen` 会算成 17 亿秒（1970 年至今），
    #   界面上要是显示「离线 xxx 前」就成了一串天文数字。
    #   往后推一个 TTL 再多一秒 —— 判定必然离线，读出来也就是「一分钟前」。
    rec["last_seen"] = time.time() - PHONE_SOURCE_TTL - 1
    table[key] = rec
    save_settings({"phone_sources": table})
    # ★ 顶栏那几枚手机角标是**另一套**判定：`mobile_devices` 的内存 `_LIVE`
    #   窗口（`MOB.is_online`，60 秒）。只把侧栏那条 `last_seen` 推到过期，
    #   顶栏角标还会自己亮满一分钟 —— 实测就是「侧栏已经灰了，顶栏那枚手机
    #   图标还挂着」。断开是用户明确表达的意图，两处都该立刻熄。
    #   `dev` 是这一趟请求的令牌认证出来的配对记录，指哪打哪，不会误伤别的手机。
    try:
        MOB.mark_offline(dev)
    except Exception:
        pass
    return {"ok": True, "known": True, "offline": key}


def phone_sources_list() -> dict:
    """侧边栏「全部来源」要的手机来源清单（带在线判定）。

    ★ 这里会**按机器号去重**（范老板 2026-10-13）。历史遗留：以前表的键是手机
      自报的 identifierForVendor，重装 App 就换一个，于是同一台手机能在
      「全部来源」里堆出好几个文件夹。新的自报会按 device_id 把老记录搬走，
      但用户没再连过的那几台就一直躺着 —— 所以显示这一层再收一次口，
      同一台机器只画最新那条（在线的优先）。
    """
    now = time.time()
    raw = _phone_sources()
    best = {}
    for k, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        # 认机器的身份：优先 device_id（手机自报的 identifierForVendor），
        # 没有就退回 host —— 同一个 IP 上的记录必然是同一台（换了设备会在
        # phone_announce 里另立一条带后缀的键，不会撞）。
        ident = str(rec.get("device_id") or "") or ("host:" + str(rec.get("host") or ""))
        r = dict(rec)
        r["id"] = k
        r["online"] = (now - float(r.get("last_seen") or 0)) < PHONE_SOURCE_TTL
        r["ago"] = round(now - float(r.get("last_seen") or 0), 1)
        r["base"] = "http://%s:%s" % (r.get("host"), r.get("port"))
        cur = best.get(ident)
        if cur is None:
            best[ident] = r
        else:
            # 在线的压过离线的；都在线（或都离线）就留新的那条。
            if (r["online"], float(r.get("last_seen") or 0)) > (cur["online"],
                                                               float(cur.get("last_seen") or 0)):
                best[ident] = r
    out = list(best.values())
    out.sort(key=lambda r: r.get("last_seen") or 0, reverse=True)
    return {"sources": out, "ttl": PHONE_SOURCE_TTL}


def _phone_prune(table: dict, host_key: str, now: float) -> int:
    """清掉同一 IP 上早就凉透的重复记录（换手机 / 重装留下的）。

    只删「同一个 host 且离线超过 5 个 TTL」的 —— 别的 IP 上的记录不动，
    那是用户别的设备，或者只是这次没连过来。
    """
    gone = 0
    for k, r in list(table.items()):
        if not isinstance(r, dict) or k == host_key:
            continue
        if str(r.get("host") or "") != host_key:
            continue
        if (now - float(r.get("last_seen") or 0)) > PHONE_SOURCE_TTL * 5:
            del table[k]
            gone += 1
    return gone


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
    # ★ 连过的手机也算一个「来源」：素材在手机上，电脑这边只存清单+缩略图缓存。
    #   `path` 用 `phone:<设备id>` 这种虚拟路径，前端据此走 /v1/phone/* 那几个口子。
    for rec in phone_sources_list()["sources"]:
        # 原件缓存也一并报上去：侧栏那行要显示「已同步 12/300 · 1.2 GB」，
        # 用户才知道点过的那次同步到底落了什么、占了多少地方。
        # 这个统计要 walk 一遍目录，但每台手机也就几百个文件，且只在这个接口里做
        # （不是每帧调用），不值得再存一份索引去维护一致性。
        cached = phone_cache_stats(rec["id"])
        out.append({
            "path": "phone:" + rec["id"],
            "name": rec.get("name") or "手机",
            "kind": "phone",
            "device": rec["id"],
            "files": int(rec.get("items") or 0),
            "vectors": 0,
            "indexed": int(rec.get("indexed") or 0),
            "online": bool(rec.get("online")),
            "ago": rec.get("ago"),
            "version": rec.get("version"),
            "exists": bool(rec.get("online")),
            "synced": cached.get("orig", 0),
            "synced_mb": cached.get("orig_bytes_mb", 0),
            "cache_mb": round((cached.get("thumb_bytes", 0)
                               + cached.get("orig_bytes", 0)) / 1048576, 2),
        })
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



#: Python 的 `mimetypes` 在 macOS 上把 `.m4a` 认成 `audio/mp4a-latm` ——
#: 那是「裸 LATM AAC 流」的类型，而 `.m4a` 实际上是 MP4 容器。AVFoundation
#: 照这个类型去解会直接判 `Cannot Open / This media format is not supported`
#: （-11828 / -12847），于是**手机上每一首 m4a 都播不了**，界面停在播放态、
#: 进度条一动不动（范老板 2026-10-11：「打开音频素材好像播放不了」）。
#: 这里把几个会踩坑的扩展名钉死成正确的类型。
_MIME_FIX = {
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
}


# ── 手机播放不了 mkv → 电脑按需「无损转封装」成 mp4 ────────────────────────────
# 范老板 2026-10-12：「联机状态下，陈奕迅那个 mkv 视频在手机上打开不播放」。
# 原因很硬：iOS 的 AVPlayer 只认 mp4 / m4v / mov（H.264 / HEVC），
# Matroska（.mkv）**根本没进 AVFoundation**，不是文件坏了、也不是传输出问题。
# 多数 mkv 里的视频是 H.264、音频是 AAC —— 这种**不用重编码**，`-c copy` 换个壳就行，
# 一分钟的片子一两秒完事。转好的 mp4 缓存下来，同一个文件下次直接命中。
#
# 三级回退：① `-c copy`；② 视频流 copy、音频转 aac（DTS/AC3/EAC3 塞不进 mp4）；
#          ③ 全量重编码（VC-1 / MPEG-2 这类，慢，但至少能看）。
_REMUX_CACHE = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                            "FXseek", "remux-cache")
# AVPlayer 原生就吃的容器，不动它。
_NATIVE_PLAY = {".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav",
                ".aif", ".aiff", ".caf", ".flac", ".m4b"}
# 需要过一道 ffmpeg 的容器。
_REMUX_EXT = {".mkv", ".avi", ".webm", ".flv", ".wmv", ".ts", ".mpg", ".mpeg",
              ".rmvb", ".vob", ".ogv", ".3gp", ".m2ts", ".mts", ".asf", ".divx"}


def _needs_remux(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in _NATIVE_PLAY:
        return False
    return ext in _REMUX_EXT


def _remux_target(path: str):
    import hashlib
    try:
        st = os.stat(path)
    except OSError:
        return None
    # ★ 前缀 `v2` 是**缓存代次**，不是装饰。v1 存下来的 mp4 是 `hev1` 标记的 HEVC，
    #   AVPlayer 一律拒播 —— 那批缓存必须作废，否则修了代码也还是播不了。
    key = "v2|%s|%d|%d" % (os.path.realpath(path), st.st_size, int(st.st_mtime))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return os.path.join(_REMUX_CACHE, h + ".mp4")


# HEVC 在 mp4 里必须打成 `hvc1`，AVFoundation 才认；`hev1` 是 ffmpeg 的默认写法，
# 但 Apple 系一律拒绝（QuickTime / AVPlayer / 相册都打不开）。
# 麻烦的是 `-tag:v hvc1` 对 H.264 是**硬报错**（Tag hvc1 incompatible with output
# codec id '27'），所以只能先探到编码再决定加不加，不能无脑加。
def _remux_ffmpeg():
    for c in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bin", "ffmpeg"),
              shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg"):
        if c and os.path.exists(c):
            return c
    return None


def _remux_ffprobe(ff: str):
    cand = os.path.join(os.path.dirname(ff), "ffprobe")
    if os.path.exists(cand):
        return cand
    return shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def _is_hevc(path: str, ff: str) -> bool:
    """探一下源文件的视频编码是不是 HEVC（h265）。探不到就按 False 走。"""
    import subprocess
    fp = _remux_ffprobe(ff)
    if not fp or not os.path.exists(fp):
        return False
    try:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, timeout=60)
        return b"hevc" in (r.stdout or b"").lower() or b"h265" in (r.stdout or b"").lower()
    except Exception:
        return False


def _audio_codec(path: str, ff: str) -> str:
    """源文件第一路音频的编码名（小写）。没有音频 / 探不到返回空串。"""
    import subprocess
    fp = _remux_ffprobe(ff)
    if not fp or not os.path.exists(fp):
        return ""
    try:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=codec_name",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, timeout=60)
        return (r.stdout or b"").decode("utf-8", "ignore").strip().lower()
    except Exception:
        return ""


# mp4 容器里 AVPlayer 能吃下的音频编码。别的（vorbis / opus / pcm / dts…）
# 就算 `-c copy` 塞进去了，画面能动也可能**一点声都没有**，所以直接转 aac。
_MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "ac-3"}


def _remux_to_mp4(path: str):
    """mkv/avi/... → mp4。成功返回 mp4 路径，失败返回 None（调用方回退成原文件）。"""
    import subprocess
    dst = _remux_target(path)
    if not dst:
        return None
    if os.path.exists(dst) and os.path.getsize(dst) > 4096:
        return dst
    ff = _remux_ffmpeg()
    if not ff:
        return None
    os.makedirs(_REMUX_CACHE, exist_ok=True)
    tmp = dst + ".part.mp4"
    # ★ HEVC 必须打 `hvc1`，否则 iPhone 打不开（见 `_is_hevc` 的注释）。
    #   H.264 不能带这个 tag —— 会直接 rc=183 报 incompatible。
    hevc = _is_hevc(path, ff)
    tag = ["-tag:v", "hvc1"] if hevc else []
    # 音频编码不在 mp4 安全名单里，第一轮就别 copy 音频了，省一次白跑。
    ac = _audio_codec(path, ff)
    audio_ok = (not ac) or (ac in _MP4_SAFE_AUDIO)
    base = [ff, "-y", "-loglevel", "error", "-i", path, "-sn", "-dn",
            "-movflags", "+faststart"] + tag
    # 注意：输出路径 `tmp` 必须**在每个 try 的最后**补齐 —— 少写它 ffmpeg 会直接报
    # 「At least one output file must be specified」并 rc=1（这个坑踩过一次）。
    tries = []
    if audio_ok:
        tries.append(base + ["-c", "copy", tmp])
    tries += [
        base + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", tmp],
        # 最后兜底：重编码。MKV 里嵌了字轨/多音轨时也走这条。
        base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", tmp],
    ]
    for cmd in tries:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            r = subprocess.run(cmd, capture_output=True, timeout=1800)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 4096:
                os.replace(tmp, dst)
                return dst
        except Exception:
            continue
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


def _mime_for(path: str) -> str:
    """给 /v1/file 用的 Content-Type。先查修正表，再退回标准库。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME_FIX:
        return _MIME_FIX[ext]
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


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


def _persist_settings(s: dict) -> None:
    """把设置写回 settings.json（原子写：先写 .tmp 再 rename）。

    ★★ 这个函数曾经**只有调用、没有定义** —— `42b42b9`（1.0.6 的 ASR 时长修复）
    引入 `_persist_settings(s)` 时漏了本体。后果是**从 1.0.6 起所有设置都存不下来**：

      * `load_settings` 里那处调用包在 `try/except: pass` 里，静默吞掉；
      * `save_settings` 里那处**没有保护**，直接抛 `NameError` → POST /v1/settings
        返回 500，而前端只 `await ... .json()` 不查状态码，照样弹「设置已保存」。

    于是界面上一片「已保存」，重进就回到原样 —— 用户 1.0.7 报的就是这个。
    教训：**新增一个「持久化」旁边的东西时，先把写入这条路整个走通再谈别的**；
    而且 `except: pass` 包在最要命的那一步上，等于把故障调成静音。

    原子写的理由：settings.json 里冻着 API Key、提示词、语言等全部用户配置，
    直接 `open(w)` 写一半时被强杀会留下截断的 JSON，`load_settings` 读它时
    会被 `except` 吃掉、**静默退回全默认值** —— 用户的 Key 和提示词看着就没了。
    """
    tmp = SETTINGS_PATH + ".tmp"
    d = os.path.dirname(SETTINGS_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_PATH)
    try:
        os.chmod(SETTINGS_PATH, 0o600)   # 里面有 API Key，别让同机别的账号顺手读到
    except OSError:
        pass


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
    # ★ 「手机来源」的缩略图缓存也归它管 —— 用户点了「清除缓存」，就该连手机素材的
    #   封面一起清掉（手机上的素材和索引一件不动，下次看再按需重新拉）。
    ph = 0
    if os.path.isdir(PHONE_CACHE_DIR):
        for dev in os.listdir(PHONE_CACHE_DIR):
            th = os.path.join(PHONE_CACHE_DIR, dev, "thumbs")
            if os.path.isdir(th):
                ph += len(os.listdir(th))
                shutil.rmtree(th, ignore_errors=True)
    # 「清除失效记录」：把那台手机那份已经过期的清单也丢掉（TTL 之外就算失效）
    now = time.time()
    stale = 0
    for dev, rec in list(_phone_sources().items()):
        if (now - float(rec.get("last_seen") or 0)) > PHONE_SOURCE_TTL * 3:
            try:
                shutil.rmtree(_phone_cache_dir(dev), ignore_errors=True)
                stale += 1
            except Exception:
                pass
    return {"removed": n, "phone_thumbs": ph, "phone_stale": stale, "cache": cache_info()}


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
    _eg2_forget(con, paths)
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
# 允许甩给系统浏览器的域名。收款页（爱发电 / 自家发货页）也要在这里，
# 另外「设置里的 buy_url 域名」永远放行 —— 换平台不必改代码（2026-10-18 补）。
_URL_ALLOW = ("github.com", "www.github.com",
              "afdian.com", "www.afdian.com",
              "afdian.net", "www.afdian.net",
              "ifdian.net", "www.ifdian.net")


def _url_allowed(host: str) -> bool:
    """白名单 + 设置里那个收款页自己的域名。"""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in _URL_ALLOW:
        return True
    try:
        from urllib.parse import urlparse as _up
        b = (_up(str(load_settings().get("buy_url") or "").strip()).hostname or "").lower()
    except Exception:
        b = ""
    return bool(b) and h == b


def open_url(url: str) -> dict:
    """用系统默认浏览器打开外链（走 /usr/bin/open，交给用户的默认浏览器）。"""
    import subprocess
    from urllib.parse import urlparse
    u = (url or "").strip()
    try:
        p = urlparse(u)
    except Exception:
        return {"error": "链接不合法"}
    if p.scheme not in ("http", "https") or not _url_allowed(p.hostname or ""):
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
        r = ai_desc.list_models_ex(base, key or "", kind=kind)
        return {"ok": True, "models": r["models"], "note": r["note"]}
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


# ------------------------------------------------------ 增强对齐时间轴
# 转写只告诉我们「这段音频说了什么」，不告诉我们「哪个字在第几秒」。
# 强制对齐（Qwen3-ForcedAligner）把「文字 ↔ 时间」钉起来，播放面板的高亮才能
# 从「按行数均分」变成「跟着唱走」。
#
# ★ 三条设计红线：
#   1. 独立子进程跑完就退（`aligner.run_worker`）—— 0.6B 模型加激活值峰值好几 G，
#      跑完必须把内存还给系统；主进程里跑会一直攥着不放。
#   2. 只跟随后台转写，**不进「重新转写」「智能优化」那两条手动链路**：手动场景
#      用户是盯着看的，多等十几秒对齐没有收益；后台是白捡的。
#   3. `align_chars` 锚定 asr_text 的字数 —— 重新转写后字数对不上，旧时间轴自动
#      失效、重新排队（判据在 `aligner.align_status`）。
def align_cfg_from_settings(st: dict = None) -> dict:
    """把设置里的对齐相关键组装成 aligner 要的配置。"""
    st = st or load_settings()
    return {
        "enabled": bool(st.get("align_enabled")),
        "language": (st.get("asr_language") or "").strip(),
    }


def align_one(path: str, st: dict = None) -> dict:
    """给单个音频/视频做强制对齐（后台队列调用）。

    与 `asr_transcribe_one` 的区别：那边管「说什么」，这边管「什么时候说」。
    对齐必须先有转写文本，所以这里是从索引里读 `meta.asr_text`，读不到就直接说
    「还没转写」—— 免得用户以为对齐坏了。
    """
    if _STATE["busy"]:
        return {"ok": False, "message": "正在建索引，等它跑完再对齐"}
    if not _path_allowed(path) or not os.path.exists(path):
        return {"ok": False, "message": "文件不存在或不可访问"}
    st = st or load_settings()
    if not st.get("align_enabled"):
        return {"ok": False, "message": "增强对齐时间轴还没开启（设置 → 智能服务）"}
    ok, why = aln.available()
    if not ok:
        return {"ok": False, "message": why}
    # 读转写原文：对齐吃的是 asr_text，不是优化版 asr_clean（后者是 LLM 重写的，
    # 字数和断句都对不上，拿它对齐会整条错位）。
    txt = ""
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            row = con.execute(
                "SELECT meta FROM items WHERE path=? AND chunk_idx<?"
                " ORDER BY chunk_idx LIMIT 1", (path, ix.AI_TAG_BASE)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            txt = (json.loads(row[0]).get("asr_text") or "").strip()
    except Exception:
        txt = ""
    if not txt:
        return {"ok": False, "message": "这个素材还没有转写文字，先转写再对齐"}
    lang = aln.normalize_language(st.get("asr_language"), txt)
    if lang.lower() in aln.UNSUPPORTED_LANGS:
        # 日/韩分词要额外的 nagisa / soynlp，venv 里没有 —— 与其让 ImportError
        # 炸穿整个闲置队列，不如标成「对不了」直接跳过（下次也不会再排队）。
        r = {"ok": True, "skipped": True,
             "reason": "暂不支持 %s 的逐字对齐（缺分词依赖）" % lang}
        aln.apply_result(path, r)
        aln.log("%s · 跳过：%s" % (os.path.basename(path), r["reason"]))
        # 跳过也要记一笔：不然队列啃到一首日文歌，操作日志里一片空白，
        # 用户会以为对齐根本没跑（这条踩过坑）。
        op_log("时间轴对齐", f"{os.path.basename(path)} · 跳过：{r['reason']}", level="warn")
        return r
    dur = aln._probe_duration(path)
    _STATE["align_running"] = path
    try:
        r = aln.run_worker(path, txt, lang, dur, aln.model_dir())
    finally:
        _STATE["align_running"] = None
    if r.get("ok") and not r.get("skipped"):
        try:
            aln.apply_result(path, r)
        except Exception as e:
            aln.log("%s · 写库失败：%s" % (os.path.basename(path), e))
            op_log("时间轴对齐", f"{os.path.basename(path)} · 写库失败：{e}", level="warn")
            return {"ok": False, "message": "对齐写库失败：%s" % e}
        op_log("时间轴对齐", f"{os.path.basename(path)} · {len(r.get('items') or [])} 条"
                              f" · {r.get('chunks')} 块 · {r.get('elapsed')}s")
    else:
        # 失败也要留痕，否则日志里只有成功的那几个，出问题无从查起。
        op_log("时间轴对齐", f"{os.path.basename(path)} · 对齐失败："
                            f"{r.get('message') or r.get('error') or '未知原因'}",
               level="warn")
    return r


def align_status() -> dict:
    """已对齐 / 未对齐统计 + 正在对齐的文件（设置页与播放面板都要看）。"""
    st = load_settings()
    try:
        s = aln.align_status()
    except Exception:
        s = {"total": 0, "aligned": 0, "pending": 0, "na": 0, "items": []}
    ok, why = aln.available()
    return {
        "total": s["total"], "aligned": s["aligned"], "pending": s["pending"],
        "na": s["na"], "items": s["items"][:200],
        "running": bool(_STATE.get("align_running")),
        "current": _STATE.get("align_running"),
        "enabled": bool(st.get("align_enabled")),
        "ready": ok, "reason": why,
        "model": aln.model_dir() or "",
        "download": _align_dl_view(),
    }


# 对齐模型的下载状态。1.2 G 要下几分钟，做成后台线程 + 轮询（和嵌入模型
# 在启动时那一次不同：那边可以阻塞，这边是用户在设置页点一下就得有反馈）。
_ALIGN_DL = {"running": False, "stop": False, "pct": 0, "file": "",
             "index": 0, "count": 0,
             "speed": 0, "eta": 0, "error": None, "done": False,
             "started": None, "finished": None}
_ALIGN_DL_LOCK = threading.Lock()


def _align_sources() -> list:
    """给前端选下载源用的列表：顺序就是 model_dl.SOURCES 的声明顺序（mirror 在前）。"""
    return [dict(id=k, label=v.get("label"), label_en=v.get("label_en"))
            for k, v in model_dl.SOURCES.items()]


def _align_source() -> str:
    """当前选的下载源。手改过设置文件、或那个源已经从 SOURCES 里删了，都退回默认。"""
    v = (load_settings() or {}).get("align_source")
    return v if v in model_dl.SOURCES else model_dl.DEFAULT_SOURCE


def _align_dl_view() -> dict:
    d = _ALIGN_DL
    free = model_dl.disk_free(model_dl.model_dir_for(P.DATA_DIR,
                                                     model_dl.SPEC_ALIGNER))
    need = model_dl.SPEC_ALIGNER.min_free_bytes
    return {
        "running": bool(d.get("running")), "stop": bool(d.get("stop")),
        "pct": int(d.get("pct") or 0), "file": d.get("file") or "",
        "index": int(d.get("index") or 0), "count": int(d.get("count") or 0),
        "speed": d.get("speed") or 0, "eta": d.get("eta") or 0,
        "error": d.get("error"), "done": bool(d.get("done")),
        "started": d.get("started"), "finished": d.get("finished"),
        # 下载源：国外用户走 huggingface.co 比 hf-mirror.com 快得多，
        # 和「首次下载模型」的准备窗口是同一套源（model_dl.SOURCES）。
        "source": _align_source(), "sources": _align_sources(),
        # 磁盘余量给前端提前拦一道：下载中途写满盘比直接拒绝难收拾得多。
        "disk_free": free, "disk_need": need,
        "disk_ok": (free < 0 or free >= need),
        "ready": bool(model_dl.model_ready(P.DATA_DIR, model_dl.SPEC_ALIGNER)),
    }


def align_download(action: str = "status", source: str = None) -> dict:
    """对齐模型下载：start / stop / status / source。

    为什么不是「开关一开就自动下」：1.2 G 的下载在用户不知情时启动、还占着带宽，
    体验很差。设置页开关打开后先显示一张「需要下载模型」的卡片，用户点「下载」
    才真正开始 —— 但下完之后开关就是全自动的（对齐由后台队列接管）。
    """
    global _ALIGN_DL

    if action == "status":
        return _align_dl_view()

    # 换源：只改下一次下载用哪个，不打断正在跑的那次（半截文件已经落盘了，
    # 换源重来更亏）—— 和 launcher 准备窗口里 choose_source 的做法一致。
    if action == "source":
        if source not in model_dl.SOURCES:
            return dict(_align_dl_view(), ok=False,
                        error="不认识的下载源：%s" % source)
        save_settings({"align_source": source})
        return dict(_align_dl_view(), ok=True,
                    message="已切换到%s" % (model_dl.SOURCES[source].get("label")
                                        or source))

    if action == "stop":
        with _ALIGN_DL_LOCK:
            if _ALIGN_DL.get("running"):
                _ALIGN_DL["stop"] = True
        return _align_dl_view()

    if action != "start":
        return {"ok": False, "error": "未知动作：%s" % action}

    if model_dl.model_ready(P.DATA_DIR, model_dl.SPEC_ALIGNER):
        return dict(_align_dl_view(), ok=True, message="模型已就绪")

    with _ALIGN_DL_LOCK:
        if _ALIGN_DL.get("running"):
            return dict(_align_dl_view(), ok=True, message="已经在下载了")
        v = _align_dl_view()
        if not v["disk_ok"]:
            return {"ok": False, "error": "磁盘空间不足：还需 %.1f GB，可用 %.1f GB"
                    % ((v["disk_need"]) / 1073741824.0,
                       max(0, v["disk_free"]) / 1073741824.0)}
        _ALIGN_DL.update({"running": True, "stop": False, "pct": 0,
                          "file": "", "index": 0, "count": 0,
                          "speed": 0, "eta": 0, "error": None,
                          "done": False, "started": time.time(),
                          "finished": None})

    def _work():
        dest = model_dl.model_dir_for(P.DATA_DIR, model_dl.SPEC_ALIGNER)
        os.makedirs(dest, exist_ok=True)
        # ★ 在这里取源，不是在线程外：下载真的开始那一刻的设置为准。
        src = _align_source()

        def _prog(ev):
            # ★ pct / speed / eta 都是「整个模型」的口径，由 model_dl 折算好；
            #   这里只管存下来，不要再按单文件自己算一遍。
            _ALIGN_DL["pct"] = ev.get("pct") or 0
            _ALIGN_DL["file"] = ev.get("file") or ""
            _ALIGN_DL["index"] = ev.get("index") or 0
            _ALIGN_DL["count"] = ev.get("count") or 0
            _ALIGN_DL["speed"] = ev.get("speed") or 0
            _ALIGN_DL["eta"] = ev.get("eta") or 0

        def _cancel():
            return bool(_ALIGN_DL.get("stop"))

        try:
            aln.log("对齐模型开始下载：源=%s 目标=%s" % (src, dest))
            ok = model_dl.download_model(dest, source=src,
                                         progress=_prog, cancel=_cancel,
                                         spec=model_dl.SPEC_ALIGNER)
            _ALIGN_DL["done"] = bool(ok)
            if not ok:
                _ALIGN_DL["error"] = ("已停止" if _ALIGN_DL.get("stop")
                                      else "下载未完成，可在设置页重试")
            else:
                aln.log("对齐模型下载完成：%s" % dest)
        except Exception as e:
            # ★ 一定要压短再存。requests 的原始异常能有一千多字符，直接丢给
            #   设置页会把那张卡片横向撑破（实测过：一长条红字横穿整个面板）。
            #   model_dl.brief_err 会把连接池前缀、Caused by、hex 地址那堆
            #   机器噪音剥掉，只留「超时」这类有用的部分。
            #   RuntimeError 是我们自己抛给用户看的话，不必再加类型名；
            #   别的异常（真出 bug 了）留个类型名方便排查。
            _ALIGN_DL["error"] = model_dl.brief_err(
                str(e) if isinstance(e, RuntimeError)
                else "%s: %s" % (type(e).__name__, e), 200)
            aln.log("对齐模型下载失败：%s" % e)
        finally:
            _ALIGN_DL["running"] = False
            _ALIGN_DL["finished"] = time.time()

    threading.Thread(target=_work, name="align-dl", daemon=True).start()
    return dict(_align_dl_view(), ok=True, message="已开始下载")


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
        # ★ 工作模式也照跑（范老板 2026-10-18）：这里不再等冷冻解除，
        #   闲置判据跟休闲模式一模一样，只是 last_user_req 的来源更窄 ——
        #   工作模式下只有「光标在搜索区里动」才算用户在用电脑。
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
            print(f"[智能纠偏] 用户正在用（工作模式下只看搜索区），"
                  f"等空闲 {need / 60:.0f} 分钟再继续")
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
    # 对齐跑在**独立子进程**里，主进程一份 WeMM 都不碰（它只读 asr_text、
    # 只写 meta），所以不阻止按需卸载 —— 对齐期间把 2 GB 嵌入模型还回去
    # 正是「内存管理融进同一条队列」想要的效果。
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
    # 对齐一定是本地跑的（MLX 权重 + 激活值峰值好几 G），永远算重活。
    if _STATE.get("align_running"):
        return "时间轴对齐"
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
        what = what if what in ("tag", "asr", "align", "both") else "tag"
        # 和「智能纠偏」/建索引互斥：补全走的是 oMLX 的视觉或 ASR 模型，
        # 纠偏走的是文本模型 + 本进程的 WeMM，撞一起就是几个 G 的内存。
        busy = _heavy_running(exclude="idle")
        if busy:
            return {"ok": False, "message": f"正在{busy}，等它跑完再补全"}
        if man and not man.get("stop") and not man.get("finished"):
            return {"ok": False, "message": "已经有一轮补全在跑了"}
        if what in ("tag", "both") and not (bool(st.get("idle_tag")) and bool(st.get("ai_enabled"))):
            return {"ok": False, "message": "AI 打标没开：先去「智能服务」打开「AI 描述与标签」。"}
        if what in ("asr", "both", "align") and not (bool(st.get("idle_asr")) and bool(st.get("asr_enabled"))):
            if what == "asr":
                return {"ok": False, "message": "音频转写没开：先去「智能服务」打开转写。"}
            if what == "align":
                return {"ok": False, "message": "对齐要先有转写：先去「智能服务」打开转写。"}
            what = "tag"          # 只补标签
        # ★ 2026-10-18：对齐也给了个「立即补全」入口。阶段顺序是
        #   打标 → 转写 → 对齐，所以它只在已经有转写文字时才有活干。
        if what == "align" and not (bool(st.get("align_enabled")) and aln.available()[0]):
            return {"ok": False, "message": "增强对齐时间轴没开：先去「智能服务」打开它（还要先下好对齐模型）。"}
        _IDLE["manual"] = {"what": what, "done": 0, "failed": 0,
                           "started": time.time(), "stop": False, "finished": None}
        if what == "tag":
            left = len(ix.ai_tag_status()["pending"])
        elif what == "asr":
            left = len(_asr_pending_list())
        elif what == "align":
            left = len(aln.pending_list())
        else:
            left = (len(ix.ai_tag_status()["pending"]) + len(_asr_pending_list())
                    + len(aln.pending_list()))
        _label = {"tag": "标签", "asr": "转写", "align": "对齐",
                  "both": "标签+转写+对齐"}.get(what, what)
        print(f"[立即补全] 开始（{_label}，待处理 {left} 个）")
        op_log("立即补全", f"开始补{_label}，待处理 {left} 个")
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
        "idle_align": bool(st.get("align_enabled")) and bool(st.get("asr_enabled")),
        "idle_minutes": st.get("idle_minutes", 5),
        "idle_batch": st.get("idle_batch", 10),
        "idle_for": round(idle_for, 1),
        "running": bool(_STATE.get("ai_tagging") or _STATE.get("asr_running")
                        or _STATE.get("align_running")),
        "run_tagged": _IDLE.get("run_tagged", 0),
        "run_asr": _IDLE.get("run_asr", 0),
        "run_align": _IDLE.get("run_align", 0),
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
    # 优化版（asr_clean）也要能逐字：对齐偏移是从 asr_text 算的，视觉上却默认
    # 显示优化版，两串字符下标对不上 —— 所以后端用 difflib 映射一份
    # align_items_clean 出来。懒算：第一次打开这个文件时算一次写回库里，
    # 之后 `align_items_clean_src` 与 `asr_clean_at` 一致就直接用（重新优化过
    # 会因时间戳变化而自动重算，不用另设失效逻辑）。
    if meta.get("align_items") and (meta.get("asr_clean") or "").strip() \
            and meta.get("align_items_clean") is None:
        src = int(meta.get("asr_clean_at") or 0)
        if int(meta.get("align_items_clean_src") or -1) != src:
            try:
                mapped = aln.mapped_items(path)
            except Exception:
                mapped = None
            if mapped:
                meta["align_items_clean"] = mapped
            meta["align_items_clean_src"] = src
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


def phone_kind_tally(source=None) -> dict:
    """把已配对手机（「手机来源」）的素材也算进计数。

    ★ 侧栏 / 顶栏角标原来只数 sqlite 索引里的文件，手机那几十件不在索引里，
      所以「全部素材 349」永远看不到手机；而浏览视图里明明有它们。
      这里读电脑自己那份清单缓存（`phone:<id>/manifest.json`，离线也在），
      按 kind 汇总，交给 scoped_stats 并进去。
    source 指到了别的索引源时就不算手机 —— 让「按来源筛选」保持干净。
    """
    out = {}
    try:
        recs = phone_sources_list()["sources"]
    except Exception:
        return out
    for rec in recs:
        key = "phone:" + str(rec.get("id") or "")
        if source and source != key:
            continue
        try:
            p = os.path.join(_phone_cache_dir(rec["id"]), "manifest.json")
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for it in (data.get("items") or []):
            k = it.get("kind") or "other"
            out[k] = out.get(k, 0) + 1
    return out

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
        # ★ 一个索引源都没有时也得算手机 —— 不然「只连了手机、还没加本地文件夹」
        #   的用户看到的是全 0，而素材墙里明明有货。
        pk = phone_kind_tally(source)
        return {"vectors": 0, "files": sum(pk.values()), "by_kind": pk,
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
    # ★ 再把手机那批并进来：侧栏与顶栏的角标要和浏览视图口径一致。
    pk = phone_kind_tally(source)
    extra = 0
    for k, n in pk.items():
        by_kind[k] = by_kind.get(k, 0) + n
        extra += n
    return {"vectors": vectors, "files": len(files) + extra,
            "by_kind": by_kind,
            "vectors_by_kind": kind_vectors,
            "ai_tagged": len(ai_tagged),
            "phone_files": extra}


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
                                    asr_config=asr_cfg, kinds=kinds,
                                    max_files=lic.limits().get("max_items"))
        except Exception as e:
            status = f"error: {e}"

        elapsed = round(time.time() - t0, 1)
        indexed = (result or {}).get("indexed")
        skipped = (result or {}).get("skipped")
        units = (result or {}).get("units")
        removed = (result or {}).get("removed") or 0
        failed = (result or {}).get("failed") or 0
        limited = (result or {}).get("limited") or 0
        _STATE["last_index"] = {"status": status, "paths": paths, "kinds": kinds,
                                "elapsed": elapsed, "at": time.time(),
                                "indexed": indexed, "skipped": skipped,
                                "units": units, "removed": removed, "failed": failed,
                                "limited": limited}
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
            if limited:
                # ★ 不写成「错误」：活儿干完了，只是到上限了。用户该看到的是
                #   「还能继续，但要解锁」，不是「出错了」。
                msg += (f"；免费版上限 {lic.limits().get('max_items')} 个文件，"
                        f"另有 {limited} 个没进索引")
            op_log("索引", f"{msg}，耗时 {elapsed}s")
            _STATE["progress"] = {"phase": "done", "message": msg,
                                  "indexed": indexed, "units": units,
                                  "elapsed": elapsed, "limited": limited,
                                  "updated": time.time()}
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
        # ★ 一次 stat 同时拿 size 和 mtime：原来是 sizeof，手机端要 mtime 就得再
        #   stat 一遍（20 条结果就是 20 次多余的 syscall，冷启动时还容易卡在
        #   网络盘/移动盘上）。反正两者都来自同一次 stat。
        try:
            _st = os.stat(path)
            size, _mtime = _st.st_size, _st.st_mtime
        except OSError:
            size, _mtime = 0, 0
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
                    "chunk_idx": idx, "meta": m, "size": size, "mtime": _mtime,
                    "matched_timestamp": matched_ts,
                    "name": os.path.basename(path),
                    "exists": True})
    # ★ 路线 A：同样的查询也丢给「在线的手机」，让它用自己的 EG2 算一遍，结果并进来。
    #   两边各算各的、向量空间不通也没关系 —— 只交换结果，不交换向量。
    #   手机结果的 path 用 `phone:<设备>|<手机内路径>`，缩略图/原件由 /v1/thumb、/v1/file 转手。
    if not source or (isinstance(source, str) and source.startswith("phone:")):
        # 下面循环里 `kind` 会被手机返回的类别覆盖掉，先把「只看哪一类」存一份。
        kind_filter = kind
        for rec in phone_sources_list()["sources"]:
            if not rec.get("online"):
                continue
            if isinstance(source, str) and source.startswith("phone:") and source.split(":", 1)[1] != rec["id"]:
                continue
            try:
                pr = phone_search(rec["id"], {"q": query, "top_k": top_k}, timeout=30.0)
            except Exception:
                continue
            for it in pr.get("items") or []:
                q = it.get("path") or ""
                kind = it.get("kind") or ""
                # ★ 同上：电脑这边按类别筛过（ix.search 的 kind=），手机那一路也得跟着筛，
                #   否则「只看视频」的搜索结果里会混进图片。
                #   ★ 判据是 kind_filter（用户选的那一类），不是 kind（这一条自己的类别）——
                #     写成 `if kind and ...` 的话，kind_filter 为 None 时会把手机结果全丢光。
                if kind_filter and kind != kind_filter:
                    continue
                out.append({
                    "score": round(float(it.get("score") or 0), 4),
                    "path": "phone:%s|%s" % (rec["id"], q),
                    "kind": kind, "chunk_idx": 0,
                    "meta": {"width": it.get("width") or 0, "height": it.get("height") or 0,
                             "size": it.get("size") or 0,
                             **({"duration": it.get("duration")} if it.get("duration") else {})},
                    "size": it.get("size") or 0, "matched_timestamp": None,
                    # mtime / chunks 手机端也发了（与 _mobile_pack 对称）：
                    # 时间要给「什么时候拍的」，chunks 给详情里的「索引了几个向量」。
                    # 手机没发（老版本 App）就退回 0，不能因此炸掉整条结果。
                    "mtime": it.get("mtime") or 0,
                    "chunks": int(it.get("chunks") or 0),
                    "name": it.get("name") or "", "exists": True,
                    "why": it.get("why") or "手机上 EG2 命中",
                    "source": {"kind": "phone", "device": rec["id"], "name": rec.get("name") or "手机"},
                })

    # 手机结果和电脑结果混在一起：都按相似度排，高的在前（前端默认也是这个顺序）
    out.sort(key=lambda r: r.get("score") or 0, reverse=True)
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

_SRV = {"inst": None, "host": "127.0.0.1", "port": 8231, "thread": None,
        # 「正在重开监听」标记：换绑定地址时旧实例会先被置空、新实例还没建好，
        # 中间有个空窗期。run_server 的守候循环必须认这个标记，否则它会以为
        # 「没有服务了」而返回 → main() 结束 → 进程退出，把新监听一起带走。
        "rebind": False}


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


# --------------------------------------------------------------------------
# 手机版 / 局域网（见 docs/手机版规划.md）
# --------------------------------------------------------------------------
def _bind_host() -> str:
    """监听地址：开了局域网绑 0.0.0.0，否则只绑回环。"""
    try:
        return "0.0.0.0" if MOB.lan_enabled() else "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def mobile_restart_server(on=None) -> dict:
    """局域网开关拨动后重开监听。

    ★ 绑定地址在建监听那一刻定死，改不了 —— 只能把服务停掉再架起来。
      而**不能在当前请求里直接关**：这个函数正是那个 POST 在处理，
      当场 server_close() 会把承载这次响应的连接一起掐了，手机/设置页看到的是
      「请求失败」，其实设置已经改好了。所以照抄 server_stop() 的办法：
      延 0.35 秒，在一个独立线程里动手，先让响应发出去。
    """
    host = _bind_host()
    _SRV["host"] = host
    if not server_running():
        return {"ok": True, "host": host, "running": False, "note": "服务本来就没在跑"}
    port = _SRV["port"]
    # ★ 先立旗再动手：从「旧实例置空」到「新实例建好」之间 _SRV["inst"] 是 None，
    #   run_server 的守候循环会误判成「服务全停了」而返回，进程随即退出。
    _SRV["rebind"] = True

    def _later():
        time.sleep(0.35)
        try:
            srv = _SRV.get("inst")
            if srv is not None:
                _SRV["inst"] = None
                _SRV["thread"] = None
                try:
                    srv.shutdown()
                except Exception:
                    pass
                try:
                    srv.server_close()
                except Exception:
                    pass
            # 想要 0.0.0.0 建不起来（比如被别的进程占了、权限不足）就退回回环，
            # 至少桌面端还能用，而不是整个服务没了。
            err = None
            for h in ([host] if host == "127.0.0.1" else [host, "127.0.0.1"]):
                try:
                    _s = _Server((h, port), Handler)
                except Exception as e:
                    err = e
                    continue
                _SRV["inst"] = _s
                _SRV["host"] = h
                th = threading.Thread(target=_s.serve_forever, daemon=True,
                                      name="fxseek-server")
                _SRV["thread"] = th
                th.start()
                if h != host:
                    print("[服务] %s 建不起来（%s），已退回 %s:%d" % (host, err, h, port))
                else:
                    print("[服务] 监听已切到 %s:%d" % (h, port))
                return
            print("[服务] 重开监听失败：%s" % err)
        finally:
            _SRV["rebind"] = False

    threading.Thread(target=_later, daemon=True, name="fxseek-mobile-rebind").start()
    return {"ok": True, "host": host, "running": True, "pending": True}


MOB.init(load_settings=load_settings, save_settings=save_settings,
         op_log=op_log, restart_server=mobile_restart_server)


def mobile_caps() -> dict:
    """`/v1/mobile/caps` —— 手机据此决定自己是「三级」还是「二级」。

    ★ 这是**稳定契约**：手机端要按字段名硬编码，所以只加不改。
    """
    st = load_settings()
    out = {"api_version": MOB.API_VERSION, "service": MOB.SERVICE_TYPE,
           # ★ 名字和 Bonjour 广播用同一个（这台电脑的名字，不是统一的「FXseek」）——
           #   手机「看板」上连接的那台电脑就显示它。
           "name": MOB.host_label(), "version": DEFAULT_SETTINGS["version"],
           "levels": ["L0", "L1", "L2"],
           "has_ai_desc": bool(st.get("ai_enabled")),
           "has_asr": bool(st.get("asr_enabled")),
           "has_align": bool(st.get("align_enabled"))}
    try:
        con = sqlite3.connect(ix.DB_PATH)
        r = con.execute("SELECT COUNT(*) FROM items WHERE chunk_idx < ?",
                        (ix.AI_TAG_BASE,)).fetchone()
        out["item_count"] = int(r[0]) if r else 0
        con.close()
    except Exception:
        out["item_count"] = 0
    try:
        out["sources"] = len(load_sources())
    except Exception:
        out["sources"] = 0
    return out


def mobile_state() -> dict:
    """设置页「连接」卡片要的全部信息（含当前配对码）。★ 只给回环看。"""
    port = _SRV["port"]
    st = MOB.state(port=port, name=MOB.host_label(),
                   version=DEFAULT_SETTINGS["version"],
                   items=mobile_caps().get("item_count", 0))
    st["port"] = port
    st["token_count"] = len(st.get("devices") or [])
    if st.get("enabled"):
        st.update(MOB.pair_code())
    return st


def mobile_vectors(q) -> dict:
    """`/v1/mobile/vectors?since=&dim=&limit=` —— EG2 向量的增量同步。

    这是手机「离线也能搜」的唯一途径：桌面用**和手机同一个** EG2 模型把
    全库算一遍，手机按 `rev` 增量拉走，之后断网也能语义检索。

    ★ 这里给的是 `eg2_vectors` 表，**不是** `items.vec`。
      桌面自己的检索向量是 WeMM（约 2 GB），和 EG2 **不是一个向量空间**，
      喂给手机上的 EG2 检索器只会得到噪声。两套并存，各服务各的。

    ★ 契约稳定，只加不改（手机端按字段名硬编码）。
    """
    def _int(key, dflt):
        try:
            v = int((q.get(key) or [""])[0] or 0)
            return v if v > 0 else dflt
        except Exception:
            return dflt

    con = eg2.connect(ix.DB_PATH)
    try:
        out = eg2.since(con, _int("since", 0), _int("dim", 0) or None,
                        _int("limit", 500))
        out["state"] = eg2.model_state()
        st = eg2.stats(con)
        out["built"] = st["count"]
        out["tombstones"] = st["tombstones"]
        try:
            r = con.execute("SELECT COUNT(*) FROM items WHERE chunk_idx < ?",
                            (ix.AI_TAG_BASE,)).fetchone()
            out["items"] = int(r[0]) if r else 0
        except Exception:
            out["items"] = 0
        return out
    finally:
        con.close()


def _mobile_why(item, meta):
    """给手机上的一行结果配一句「为什么它在这」——手机屏幕小，分数没意义。"""
    kind = item.get("kind")
    ts = item.get("matched_timestamp")
    if kind == "video":
        return "画面命中 %.1f 秒那一帧" % ts if ts is not None else "画面命中"
    if kind == "image":
        return "画面相似"
    if kind == "audio":
        return "音频内容相似"
    return "内容相似"


def _index_chunk_counts(paths):
    """一次 SQL 查出这些文件各有多少条索引行（手机端详情/角标要显示「几块」）。

    为什么不在 `_mobile_pack` 里逐条查：手机一次要 20 条，逐条就是 20 次
    `sqlite3.connect` + 20 次全表 COUNT，而在 do_search 里这 20 条还是紧接着
    一次完整检索跑的（缓存都还热着），纯属白花时间。改成 IN 一次查完，
    顺便绕开 SQLite 的 999 变量上限（每批 400 个）。
    """
    out = {}
    todo = [p for p in dict.fromkeys(paths) if p]
    if not todo:
        return out
    try:
        con = sqlite3.connect(ix.DB_PATH)
        try:
            for i in range(0, len(todo), 400):
                part = todo[i:i + 400]
                marks = ",".join("?" * len(part))
                for p, n in con.execute(
                        "SELECT path, COUNT(*) FROM items WHERE path IN (%s)"
                        " GROUP BY path" % marks, part):
                    out[p] = int(n or 0)
        finally:
            con.close()
    except Exception:
        pass
    return out


def _mobile_pack(x, why, chunks=None):
    """把桌面端三种检索（文字 / 图 / 音）的结果统一成手机要的那几个字段。

    三条路返回的 dict 键各不一样（`/v1/search` 有 matched_timestamp、
    以图搜图也有、以音搜素材只有指纹分），手机不该为此写三套解析，所以在这里抹平。
    """
    path = x.get("path") or ""
    ts = x.get("matched_timestamp")
    if ts is None:
        ts = x.get("ts")
    # ★ 这一段是**给手机端的硬契约**：手机把这段 JSON 直接解成 `MediaItem`，
    #   用的是编译器合成的 `init(from:)` —— 合成版**不看属性默认值**，只要键少一个、
    #   或者值是 null，整个 `SearchResponse` 就解崩、搜索页显示「返回内容看不懂」。
    #   所以下面每个键都必须**存在且非 null**，宁可在这里兜个默认值：
    #     · `mtime`  —— 缺了就取文件的真实 mtime（早先不发，手机上永远显示 0，
    #                   「详情里改了时间却排不对」就是这个坑）
    #     · `chunks`  —— 数不出就 0
    #     · `kind`    —— 指纹/声纹那条路不给 kind，按后缀推一个
    #     · `score`   —— `_linked_rows` 里 score 可能为 None，转成 0.0
    #     · `meta`    —— 宽高比在这儿；带上它，手机端就不用再额外发一次
    #                   `/v1/browse` 去回填尺寸表了（`MediaCard` 直接读 meta）
    #   客户端那版「逐键 try?」的手写解码器已经删掉了，这里别再退回去。
    mtime = x.get("mtime")
    if mtime is None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
    if chunks is None:
        chunks = x.get("chunks")
        if chunks is None:
            chunks = 0
    kind = x.get("kind")
    if not kind:
        kind = _kind_of(path) if path else "other"
    try:
        score = float(x.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    # `size` 在 MediaItem 里是非可选 Int —— 客户端手写解码器删掉之后，
    # 只要上游给个 416483.0 这样的浮点就会 valueMismatch 炸掉整条结果。
    try:
        size = int(x.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    meta = x.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    # 详情里量过的尺寸优先（`/v1/search` 的 meta 就带这些），没有就现场补一次——
    # 手机端只有缩略图能看比例，缺了宽高比每张卡片都是 4:3。
    if (not meta.get("width") or not meta.get("height")) and path and not path.startswith("phone:"):
        try:
            m = ix.probe_media_meta(path) or {}
            for k in ("width", "height", "duration"):
                if m.get(k) and not meta.get(k):
                    meta[k] = m[k]
        except Exception:
            pass
    return {
        "path": path,
        "name": x.get("name") or os.path.basename(path),
        "kind": kind,
        "score": score,
        "size": size,
        "mtime": mtime or 0,
        "chunks": int(chunks or 0),
        "ts": ts,
        "why": why,
        "exists": x.get("exists", True),
        "meta": meta,
        # 手机直接拿这个去 GET，不必自己拼参数、也不必知道 w 的上限
        "thumb": "/v1/thumb?w=320&path=" + urllib.parse.quote(path),
    }


def mobile_search(body) -> dict:
    """手机版「深度外派」检索（规划 §3.5 的 L2）。

    手机自己做 L0/L1（文件名、标签、时间），只有想问「画面里到底有什么」
    时才把查询甩给电脑。支持三种入参，正好对上手机上那三个入口：

        {q: "文字"}       文字搜
        {image: "base64"} 拍照即搜
        {audio: "base64"} 录音即搜

    这边跑的就是桌面端同一套检索，只是把回包裁剪成手机要的字段 ——
    chunk_idx / meta 那一坨手机用不上，另外补一个能直接取的缩略图地址。
    文字默认走 deep：手机既然外派了，就是奔着语义去的。
    """
    q = (body.get("q") or body.get("query") or "").strip()
    img = body.get("image") or ""
    aud = body.get("audio") or ""
    if not (q or img or aud):
        raise ValueError("至少要给一个：q（文字）/ image（base64）/ audio（base64）")
    try:
        top_k = max(1, min(int(body.get("top_k") or 20), 100))
    except (TypeError, ValueError):
        top_k = 20
    kind = body.get("kind")
    t0 = time.time()
    tier = None

    if not q and img:
        raw = img.split(",", 1)[1] if ("," in img and img.strip().startswith("data:")) else img
        try:
            from PIL import Image as _PILImage
            pic = _PILImage.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        except Exception as e:
            raise ValueError("图片解析失败：%s" % e)
        rows = search_by_image(pic, top_k=top_k, kind=kind)
        cc = _index_chunk_counts([x.get("path") for x in rows])
        items = [_mobile_pack(x, "画面像你拍的那张", cc.get(x.get("path"), 0))
                 for x in rows]
        why_used = "image"
    elif not q and aud:
        r = audio_search({"audio": aud, "top_k": top_k})
        if r.get("error"):
            raise ValueError(r["error"])
        rows = r.get("results") or []
        cc = _index_chunk_counts([x.get("path") for x in rows])
        items = [_mobile_pack(x, "听到的是同一段声音", cc.get(x.get("path"), 0))
                 for x in rows[:top_k]]
        why_used = "audio"
        # 指纹一条都没匹上时，audio_search 会带一句「这段录音区分度不够」
        if not items and r.get("message"):
            return {"query": "", "tier": None, "used": "audio", "total": 0,
                    "items": [], "note": r["message"],
                    "took_ms": int((time.time() - t0) * 1000)}
    else:
        # ★ 2026-10-12：手机端原来固定传 tier="deep"，而 deep 这条路在 indexer 里
        #   **不做任何阈值过滤** —— 于是手机上搜「宇宙」也能刷出 60 条电脑素材，
        #   分数全在 0.41~0.42，界面上一片「瞎显示」。
        #   电脑端自己的搜索框走的是 auto（自带分层闸门），这里对齐：
        #   tier 默认 auto，再补一道 threshold（和电脑端音频那路一样用 0.30）。
        r = do_search(q, top_k=top_k, kind=kind, tier=(body.get("tier") or "auto"),
                      threshold=float(body.get("threshold") or 0.30),
                      source=body.get("source"),
                      lexical=bool(body.get("lexical", True)))
        tier = r.get("tier")
        rows = r.get("results") or []
        cc = _index_chunk_counts([x.get("path") for x in rows])
        items = [_mobile_pack(x, _mobile_why(x, x.get("meta") or {}),
                              cc.get(x.get("path"), 0)) for x in rows]
        why_used = "text"

    return {"query": q, "tier": tier, "used": why_used, "total": len(items),
            "items": items, "took_ms": int((time.time() - t0) * 1000)}


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


def _work_html():
    """读取外部 work.html（工作模式的**搜索窗**页面）。"""
    p = os.path.join(HERE, "work.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


def _rope_html():
    """读取外部 rope.html（工作模式的**暂存区窗**页面：钢丝绳 + 挂着的明信片）。"""
    p = os.path.join(HERE, "rope.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


# ==========================================================================
# 工作模式（1.11 预览版）
# ==========================================================================
# 一句话：休闲模式是「把整个库摊开给你看」，工作模式是「你带着一个具体的目的来，
# 搜、挑、挂、收工走人」。
#
# 三段式：
#   搜  —— 极简搜索框，复用 /v1/search，结果在框下铺成缩略图。
#   挂  —— 把缩略图拖出来，就变成一张「带挂钩的明信片」，挂到那条曲线绳上。
#         绳子就是暂存区：**这里不复制任何文件**，只是把你选中的东西先摆出来。
#   收工 —— 点绳子左端的按钮，绳子上的明信片做一把「收牌」动画，同时把每个
#         素材各复制一份进桌面 FXseek 文件夹（按类别分子目录）。
#
# 为什么「先挂再收」而不是「点一下就直接复制到桌面」：
#   选素材这件事天然是反复的 —— 搜三次、挂七张、去掉两样，最后才定。要是每点
#   一下就落一次盘，桌面上会立刻堆出一坨同名文件，还得自己去删。挂起来是**零成本
#   的撤销**（摘下来就没了），收工才是一次性的、不可逆的那一下。
#
# 为什么在桌面建文件夹而不是「复制到剪贴板 / 发给谁」：
#   桌面文件夹是用户看得见、找得回、能继续用 Finder 处理的东西。让 FXseek 变成
#   「Finder 的上游」而不是「另一个关起门来的库」，这个功能才有意义。

# 类别 → 桌面 FXseek 里那个子目录名。用中文目录名，因为这是给人看的。
_WORK_KIND_DIR = {"image": "图片", "video": "视频", "audio": "音频", "document": "文档"}

# 唤醒键的合法写法：`+` 连接的若干修饰键 + 一个主键。
# 次序按 **Apple 自己的惯例**：⌃ Control → ⌥ Option → ⇧ Shift → ⌘ Command。
# 菜单里、快捷键设置里、官方文档里全是这个顺序；用别的顺序排出来的
# `⌘⌥Space` 会让 mac 用户看一眼就怀疑「这软件是不是不太懂 mac」。
_WORK_MODS = ("ctrl", "alt", "shift", "cmd")

# --------------------------------------------------------------------------
# 工作模式标记（★ 2026-10-18 起**不再冷冻后台**）
# --------------------------------------------------------------------------
# 用户原来的话（m14469）：「处于工作模式时，休闲模式冷冻着不占系统资源，
# 此时后台运行的 llm、asr、对齐都得暂停」—— 那条按「暂停」实现过一版。
#
# ★ 2026-10-18 范老板改口径：「工作模式下后台补全、增强对齐时间轴、智能纠偏
#   和休闲模式的对齐都要有，不要暂停它们。唯一区别是判定用户是否在活动的
#   区域是『搜索区』，『暂存区』不参与判定。」
#   所以「冷冻」整个撤掉：暂停的代价是「工作模式下一整天都不补全」，
#   而工作模式恰恰是人在电脑前、最需要结果准备好的时候。
#
# 保留 `_WORK` 这个 dict 和 `_work_frozen()` 函数名，是因为 _idle_loop /
# 智能纠偏 / work_set_mode 都在调 —— 改成恒 False，别处一行都不用动。
# 真正起作用的是**活动判定**（见 workwin `_activity_probe`）：
# 光标只有在搜索区里动，才算用户在用电脑。
_WORK = {"frozen": False, "work": False}


def _work_frozen() -> bool:
    """★ 已经没有「冷冻」这回事了 —— 保留函数名只是为了不动旧调用点。

    范老板 2026-10-18：工作模式下后台补全、增强对齐时间轴、智能纠偏、
    休闲模式那一套对齐**都要照跑**，一个都不许暂停。以前一进工作模式就把
    这几样全冻住（理由是「别抢算力」），可实际用下来，工作模式恰恰是
    「人就在电脑前、正等着素材被整理好」的时候 —— 一冻就等于永远不补全。

    唯一的区别改成**活动判定看哪儿**：
      · 光标在「搜索区」里动 → 算用户在用电脑（闲置计时清零）；
      · 「暂存区」（绳窗挂明信片那一带）不参与判定。
    判据在 workwin 的活动探针里（`_activity_probe`），app 启动时由
    `_work_init_activity()` 把这根线接过去。
    """
    return False


def _work_freeze(on: bool, why: str = "") -> dict:
    """记录「现在是不是工作模式」。★ 不再冻结任何后台活，只留个标记。"""
    was = bool(_WORK.get("work"))
    _WORK["work"] = bool(on)
    _WORK["frozen"] = False
    if was != bool(on):
        print("[工作模式] %s：后台打标 / 转写 / 对齐 / 纠偏照跑（活动只看搜索区）"
              % (why or "切换"), flush=True)
        op_log("工作模式", ("进入工作模式（后台照跑，活动判定只看搜索区）（%s）" if on
                            else "回到休闲模式（%s）") % (why or "切换"))
    return {"was": was, "now": bool(on), "changed": was != bool(on)}


def _work_init_activity():
    """启动时把「光标在搜索区里动」这根探针接到闲置计时上（工作模式用它判活动）。"""
    try:
        import workwin as _WW
        def _tap():
            # ★ 只动 last_user_req，不动 last_req：后者连前端轮询都算，
            #   拿它判闲置会永远清零（见 _QUIET_PATHS 那段注释）。
            _STATE["last_user_req"] = time.time()
        _WW.set_activity_tap(_tap)
        _work_freeze(_work_cfg()["mode"] == "work", "启动")
    except Exception as e:                              # noqa: BLE001
        print("[工作模式] 活动探针没接上：%s" % e, flush=True)


def _work_norm_hotkey(s: str) -> str:
    """把用户填的唤醒键规范化成 `alt+cmd+space` 这种形式；认不出返回 ""。

    规范化是必须的：`Cmd+Alt+Space` / `alt+cmd+space` / `space+alt+cmd` 得看成
    同一个键，否则「我明明改回来了怎么又失效了」。修饰键固定按
    **⌃⌥⇧⌘（Apple 惯例）** 排序，和系统「键盘快捷键」里显示的次序一致。
    """
    raw = (s or "").strip().lower()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return ""
    alias = {"command": "cmd", "option": "alt", "control": "ctrl",
             "⌘": "cmd", "⌥": "alt", "⌃": "ctrl", "⇧": "shift",
             "meta": "cmd", "super": "cmd"}
    parts = [alias.get(p, p) for p in parts]
    mods = [p for p in parts if p in _WORK_MODS]
    keys = [p for p in parts if p not in _WORK_MODS]
    if len(keys) != 1:
        # 主键只能有一个。`cmd+cmd+space` 这种去重后也得只剩一个修饰键。
        return ""
    if len(set(mods)) != len(mods):
        return ""
    return "+".join(sorted(set(mods), key=_WORK_MODS.index) + [keys[0]]) if mods else keys[0]


# --------------------------------------------------------------------------
# 收工：暂存区 → 桌面
# --------------------------------------------------------------------------
# ★ 2026-10-14 整条改成两段（范老板第③条）：
#     「收工」按下 → 文件搬进 App 数据目录下的**暂存区**（用户看不见）；
#     绳窗那段「明信片飞进左下角访达」的动画放完 → 绳窗回一条
#     `/v1/work/settled`，**这一刻**才真正在桌面上建出 `FXseek` 文件夹、把暂存区
#     里的东西移进去、再打上系统红色标签。
#   为什么要绕这一圈：范老板要的是「等全部明信片飞完后，电脑桌面**自动生成**
#   FXseek 文件夹」。真文件夹没法「等」另一扇窗里的动画，所以落盘那一下必须由
#   绳窗回调。顺带把老版本两个毛病一起解决了 —— 桌面上曾经画着一个假的 FXseek
#   文件夹（和真身长得一样、两个都要维护），以及桌面图标挪不动导致的「画在右下
#   角、真身却在右上角」。
#   范老板的原话：「假FXseek文件夹取消，用户点击『收工』后明信片动效飞到左下角
#   『访达』图标回收，等全部明信片飞完后，电脑桌面自动生成『FXseek』文件夹
#   （带系统红色标签，这样文件夹样式就是红色的，特别显眼）」。
#
# ★ 为什么是「移动」不是「复制」：暂存区是 App 自己造的中转站，用户眼里那些
#   文件从没在那儿存在过。留着的话每收一次工就多攒一份副本，永远没人清。


def work_stage_dir() -> str:
    """收工的**暂存区**：`<数据目录>/收工`。用户看不见，绳窗飞完就清空。"""
    d = os.path.join(P.DATA_DIR, "收工")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def work_out_dir() -> str:
    """「收工」的落点：桌面上的 `~/Desktop/FXseek`。

    ★ 2026-10-14 绕了一圈又回到桌面。中间那版把它搬进数据目录、桌面上的
      「FXseek 文件夹」靠工作模式自己画（`#dest`）—— 那时的理由是「桌面图标
      挪不动、藏不住，只能自己画一个摆右下角」。范老板第③条把假文件夹取消了：
      真文件夹就摆在桌面上、位置交给 Finder，我们不再需要「敢不敢放桌面」
      这个权衡，也就不需要那个假的。

    `FXSEEK_WORK_OUT` 仍是**开发用**后台：自动化测试要跑「收工」，总不能真往
    用户桌面上扔文件。界面上不暴露这个变量，也不写进任何文档。
    """
    forced = os.environ.get("FXSEEK_WORK_OUT", "").strip()
    if forced:
        return os.path.expanduser(forced)
    return os.path.join(os.path.expanduser("~"), "Desktop", "FXseek")


def _work_tag_red(path: str) -> None:
    """给文件夹打上 Finder 的**系统红色标签**（范老板第③条：要一眼看见）。

    ★ 走 `com.apple.metadata:_kMDItemUserTags` 这个扩展属性：不依赖 Finder 在
      不在跑、不弹任何窗口、也不用 AppleScript 去点人。值是一个二进制 plist
      `["Red\\n6"]` —— `6` 就是 Finder 里「红色」那一项的编号（系统自己写进去
      的就是这个格式，随便拿 `xattr -p` 看一个带标签的文件都能对上）。
    ★ 打不动只打一行日志：「收工」本身绝不能因为一个标签失败。
    """
    import subprocess
    import plistlib
    try:
        blob = plistlib.dumps(["Red\n6"], fmt=plistlib.FMT_BINARY)
        r = subprocess.run(["xattr", "-w", "-x",
                            "com.apple.metadata:_kMDItemUserTags",
                            blob.hex(), path],
                           check=False, capture_output=True)
        if r.returncode != 0:
            print("[work] 打红色标签没成：%s"
                  % r.stderr.decode("utf-8", "replace").strip(), flush=True)
    except Exception as e:                                       # noqa: BLE001
        print("[work] 打红色标签没成：%s" % e, flush=True)


def work_flush(out_dir: str = "") -> dict:
    """把暂存区里的东西**移动**进桌面 FXseek（按类别分子目录）。

    静默、幂等、绝不抛：它是在动画放完那一刻被调的，不该给用户弹任何错误。
    同名走 `_work_unique` 加 ` 2`，永不覆盖。
    """
    stage = work_stage_dir()
    root = os.path.expanduser(out_dir or work_out_dir())
    moved, skipped = 0, 0
    try:
        for sub in sorted(os.listdir(stage)):
            if sub.startswith("."):
                continue
            sd = os.path.join(stage, sub)
            if not os.path.isdir(sd):
                continue
            for name in sorted(os.listdir(sd)):
                if name.startswith("."):
                    continue
                src = os.path.join(sd, name)
                if not os.path.isfile(src):
                    continue
                try:
                    d = os.path.join(root, sub)
                    os.makedirs(d, exist_ok=True)
                    # ★ 先写进最后那段路径再 move：`move` 到已存在的目标会**直接
                    #   覆盖**（shutil.move 用的是 os.rename 语义），所以目标名
                    #   必须先在真磁盘上敲定。
                    shutil.move(src, _work_unique(os.path.join(d, name)))
                    moved += 1
                except Exception as e:                           # noqa: BLE001
                    print("[work] 暂存区落盘没成：%s（%s）" % (src, e), flush=True)
                    skipped += 1
    except Exception as e:                                       # noqa: BLE001
        print("[work] 扫暂存区没成：%s" % e, flush=True)
    return {"ok": True, "dir": root, "moved": moved, "skipped": skipped}


def work_settled() -> dict:
    """绳窗那段「飞进访达」的动画放完之后调这里：真落盘 + 建文件夹 + 打红标。

    ★ 文件夹**一定要建**，哪怕这次一张都没挂成功 —— 用户点过收工，桌面上就该
      冒出那个红文件夹；一个空文件夹比「什么都没有」好解释得多。
    ★ 先等正在跑的复制收尾：动画是**乐观**启动的（页面自己先飞，不等服务端，
      见 work_collect_hung 里「先放旗子再复制」那段），动画放完时复制可能还没
      做完 —— 那 `work_flush()` 就把一个空暂存区搬过去了，文件夹建出来、东西
      还压在暂存区，得等下一次收工才补上。这里等一等就堵住了这个缝。
      （服务端是单线程的，正常情况下等不到；加这一句是防以后换成多线程。）
    """
    t0 = time.time()
    while _WORK_COLLECTING["n"] > 0 and time.time() - t0 < 30.0:
        time.sleep(0.05)
    r = work_flush()
    try:
        os.makedirs(r["dir"], exist_ok=True)
    except OSError as e:
        print("[work] 建收工文件夹没成：%s" % e, flush=True)
    # ★ 不再用 `FXSEEK_WORK_OUT` 把打标签这一步挡掉：那个变量只改**落点**
    #   （测试时指向 /tmp），落到哪儿就在哪儿打标 —— 打在测试目录上无害，
    #   反倒让「建目录 + 打红标」这条链路可以自动化自测。
    _work_tag_red(r["dir"])
    print("[work] 收工落定：%s（移入 %d 项）" % (r["dir"], r["moved"]), flush=True)
    return r


def _work_cfg() -> dict:
    """把工作模式相关的设置一次读出来、顺手兜底。"""
    s = load_settings()
    mode = str(s.get("ui_mode") or "leisure").strip().lower()
    if mode not in ("leisure", "work"):
        mode = "leisure"
    hk = _work_norm_hotkey(str(s.get("work_hotkey") or "")) or DEFAULT_SETTINGS["work_hotkey"]
    out = work_out_dir()
    return {"mode": mode, "hotkey": hk, "out_dir": out}


def _work_unique(dst: str) -> str:
    """目标已存在时，在扩展名前加 ` 2` / ` 3` …… 不覆盖用户已有的东西。

    ★ 这里**绝不能**用「同名就跳过」：用户把两张不同照片都叫 `IMG_0001.jpg`
    是家常便饭，跳过等于悄悄丢文件；而覆盖更糟 —— 那是**删用户的文件**。
    """
    if not os.path.exists(dst):
        return dst
    root, ext = os.path.splitext(dst)
    for i in range(2, 1000):
        cand = "%s %d%s" % (root, i, ext)
        if not os.path.exists(cand):
            return cand
    return "%s %d%s" % (root, int(time.time()), ext)


def work_collect(paths, out_dir: str = "") -> dict:
    """「收工」第一步：把绳子上挂着的素材各复制一份进**暂存区**，按类别分目录。

    ★ 2026-10-14 起这里不再是终点 —— 暂存区里的东西要等绳窗那段飞行动画放完、
      服务端收到 `/v1/work/settled` 时才真正落到桌面的 `FXseek`（见
      `work_settled`）。

    **是复制不是移动** —— 原文件在原来的索引源里待着不动。工作模式解决的是
    「我要把这几个文件放到一起」，没道理顺手把用户的素材库搬空。

    返回 {ok, dir, copied, skipped, items[], error?}；items 里逐条给结果，
    前端拿它做失败提示（比如某个源文件已经被删了）。
    """
    # ★ 落点是**暂存区**、不是桌面（范老板第③条）：真正的落盘由 work_settled()
    #   在动画放完之后做。`out_dir` 参数留着是给自动化测试的，传了就按传的来。
    # ★ 顺手把上一批没落盘的冲掉：上一次收工要是没等到绳窗回来（动画中途 App
    #   被关掉、绳窗崩了），暂存区里还压着那批东西 —— 不清就会越压越多，而且
    #   用户再也看不到它们。这个「补落盘」是整条链路唯一的兜底。
    if not out_dir:
        try:
            work_flush()
        except Exception as e:                                   # noqa: BLE001
            print("[work] 落上一批没成：%s" % e, flush=True)
    root = os.path.expanduser(out_dir) if out_dir else work_stage_dir()
    items, copied, skipped = [], 0, 0
    for raw in (paths or []):
        p = str(raw or "")
        if not p:
            continue
        # 手机上的素材：电脑这边只有缩略图，原件要么没同步、要么在缓存目录里。
        # 不猜，直接告诉用户「先去同步原件」——猜错了就是复制出一张 320px 的缩略图
        # 冒充原图，用户过几天才发现，比直接报错恶劣得多。
        if p.startswith("phone:"):
            items.append({"src": p, "ok": False, "skipped": True,
                          "error": "这是手机上的素材，先在手机端点「把原件同步到电脑」再收工"})
            skipped += 1
            continue
        src = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(src):
            items.append({"src": p, "ok": False, "skipped": True, "error": "源文件不在了"})
            skipped += 1
            continue
        kind = _kind_of(src) or "document"
        sub = _WORK_KIND_DIR.get(kind, "其它")
        try:
            d = os.path.join(root, sub)
            os.makedirs(d, exist_ok=True)
            dst = _work_unique(os.path.join(d, os.path.basename(src)))
            # copy2 保留 mtime —— 复制出来的照片在 Finder 里也该是「拍摄那天」，
            # 不然按时间排序就全乱了（这一条是用户最容易察觉、又最难自己修的）。
            shutil.copy2(src, dst)
            items.append({"src": src, "dst": dst, "kind": kind,
                          "name": os.path.basename(dst), "ok": True})
            copied += 1
        except Exception as e:
            items.append({"src": src, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            skipped += 1
    return {"ok": True, "dir": root, "copied": copied, "skipped": skipped, "items": items}


def work_state() -> dict:
    """工作模式当前状态。前端每次唤醒都会问一次，所以保持轻。"""
    cfg = _work_cfg()
    return {"ok": True, "mode": cfg["mode"], "hotkey": cfg["hotkey"],
            "hotkey_display": _work_hotkey_display(cfg["hotkey"]),
            "out_dir": cfg["out_dir"],
            "desktop_exists": os.path.isdir(os.path.dirname(cfg["out_dir"]) or "/"),
            "hung": len(work_hung_list()),
            "idle_sec": work_idle_sec(),
            # ★ 2026-10-18：工作模式不再暂停后台，这里回报口径也改了。
            #   `bg_paused` 留着是为了老前端不炸（恒 False）；
            #   `activity_area` 说明「算不算用户在用电脑」看的是哪块区域。
            "bg_paused": False,
            "activity_area": "search",
            "search_zone": _WORK_HIT.get("search"),
            "supported": True}


def _work_hotkey_display(hk: str) -> str:
    """`alt+cmd+space` → `⌥⌘Space`（给人看的）。"""
    glyph = {"cmd": "⌘", "alt": "⌥", "ctrl": "⌃", "shift": "⇧"}
    parts = [p for p in (hk or "").split("+") if p]
    if not parts:
        return ""
    out = "".join(glyph.get(p, "") for p in parts[:-1])
    name = parts[-1]
    pretty = {"space": "Space", "tab": "Tab", "return": "Return", "enter": "Return",
              "escape": "Esc", "esc": "Esc", "delete": "Delete",
              "left": "←", "right": "→", "up": "↑", "down": "↓"}.get(name)
    if not pretty:
        pretty = name.upper() if len(name) == 1 else name[:1].upper() + name[1:]
    return out + pretty


def work_set_mode(mode: str) -> dict:
    """切模式。**只改设置 + 通知窗口层**，窗口该藏该显由 launcher 的钩子决定。

    这么分层是为了让命令行跑的 app.py（没有窗口）也能改这个值 —— 它只会把设置
    写下去，然后老老实实回一句「窗口层不在」。
    """
    m = str(mode or "").strip().lower()
    if m not in ("leisure", "work"):
        return {"ok": False, "error": "模式只能是 leisure 或 work"}
    save_settings({"ui_mode": m})
    # ★ 2026-10-18：工作模式不再暂停后台，这一步只剩「记下模式 + 写操作日志」。
    #   顺序依旧放在通知窗口层之前：日志里先出现「进入工作模式」，
    #   用户回看时才不会觉得窗口先变了而状态没变。
    _work_freeze(m == "work", "切到" + ("工作" if m == "work" else "休闲") + "模式")
    # 通知桌面外壳（浏览器里跑时没人接，是正常的）
    res = _panel_hook("ui_mode") if "ui_mode" in PANEL_HOOKS else {}
    if isinstance(res, dict) and res.get("error"):
        res = {}
    return {"ok": True, "mode": m, "frozen": False, "bg_paused": False,
            "activity_area": "search",
            **({k: v for k, v in (res or {}).items() if k != "ok"})}


def work_toggle() -> dict:
    """在两种模式之间翻一下。

    给菜单栏那一下单击用的。单独开一条路由、而不是让调用方先读 `/v1/work/state`
    再自己判断该填 leisure 还是 work —— 那中间隔着一次网络往返，用户手快连点两下
    就会两次都读到同一个旧值、于是同一个方向翻两次，**看着像没反应**。
    判断和写入放在同一个函数里，服务端单线程串行处理，就没有这个缝。
    """
    cur = (_work_cfg().get("mode") or "leisure")
    return work_set_mode("leisure" if cur == "work" else "work")


def work_set_hotkey(hotkey: str) -> dict:
    """改唤醒键。规范化失败就**不动旧值**（宁可保留能用的那个）。"""
    hk = _work_norm_hotkey(hotkey)
    if not hk:
        cur = _work_cfg()["hotkey"]
        return {"ok": False, "error": "这个键认不出来，得是「修饰键 + 一个主键」（例如 ⌥⌘Space）",
                "hotkey": cur, "hotkey_display": _work_hotkey_display(cur)}
    save_settings({"work_hotkey": hk})
    res = _panel_hook("work_hotkey") if "work_hotkey" in PANEL_HOOKS else {}
    if isinstance(res, dict) and res.get("ok") is False:
        return {"ok": False, "error": res.get("error") or "注册失败（可能被别的软件占着）",
                "hotkey": hk, "hotkey_display": _work_hotkey_display(hk)}
    return {"ok": True, "hotkey": hk, "hotkey_display": _work_hotkey_display(hk)}


# --------------------------------------------------------------------------
# 暂存区（绳子上挂着的那些）—— 两扇窗之间的共享状态
# --------------------------------------------------------------------------
# ★ 为什么放在服务端，而不是某一扇窗的 JS 里存着：
#   工作模式是**两扇窗** ——
#     · 绳窗 rope.html：只负责显示。桌面层级、穿透点击、进了工作模式就一直在
#     · 搜索窗 work.html：负责交互。屏幕最高层级、快捷键唤醒、无操作自动收
#   两个独立的 webview，JS 变量谁也够不着谁。让它们通过本机 HTTP 说话最省事也
#   最稳：一方重启另一方的状态还在，页面刷新也不丢。
_WORK_HUNG = {"seq": 0, "items": [], "fly": False}
_WORK_HUNG_LOCK = threading.Lock()
_WORK_HUNG_MAX = 60
_WORK_HIT = {"rects": None, "search": None}
# 「鼠标现在停在暂存区上」的最后一拍（时间戳）。
# ★ 为什么需要它：搜索层那个「1 分钟没人动就自动收起」的计时器只认**搜索页自己**
#   上的 pointermove/pointerdown/keydown，而绳窗是 `setIgnoresMouseEvents_(True)`
#   的、一个鼠标事件都收不到 —— 范老板把鼠标搁在暂存区上看卡片摆，搜索层到点照样
#   收走，收走之后绳窗 3 秒又把它叫回来，每 63 秒打一次摆子。绳窗把「人在」报进来，
#   搜索窗轮询 /v1/work/hung 时顺带拿走，用来重置那个计时器。
_WORK_STAGE = {"ts": 0.0}
# 正在跑的「收工」复制有几路。见 work_settled()：动画是乐观启动的，落盘前要等它。
_WORK_COLLECTING = {"n": 0}


def work_dock_pos() -> dict:
    """左下角 Dock 最左边那个「访达」图标，在**绳窗坐标系**里的落点。

    ★ 绳窗铺的是 `NSScreen.visibleFrame`（已扣掉菜单栏，也**不含 Dock**），所以
      它的 client 坐标 = 屏幕坐标 − (0, 菜单栏高度)，窗口下边界正好压在 Dock
      上沿。「访达」图标中心在屏幕上是 (≈64, 屏高 − 38)，换算过来就是
      `(64, visibleFrame 高 + 32)` —— y 比窗口还大 32px。卡片滑到那儿早被窗口
      裁掉了，看起来正是「钻进 Dock 里没了」，这也正是我们要的效果。
    ★ 为什么不再去问 Finder / `.DS_Store`：2026-10-14 实测过 `set position`、
      直接写 `.DS_Store` 的 `Iloc`、把桌面排列改成「无」、再各配一次
      `killall Finder` —— 四条路全都改不动桌面图标。而 Dock 图标的位置是**固定**
      的（访达永远排第一个），根本用不着问。
    """
    try:
        import Quartz
        fr = Quartz.NSScreen.mainScreen().visibleFrame()
        return {"ok": True, "x": 64, "y": int(float(fr.size.height) + 32),
                "from": "dock"}
    except Exception as e:                          # noqa: BLE001
        print("[app] 算访达落点没成：%s" % e, flush=True)
    # 问不到屏幕（命令行跑、没窗口）时给个常见值：900 高的屏 → 860 + 32
    return {"ok": True, "x": 64, "y": 892, "from": "fallback"}




def work_idle_sec() -> int:
    """搜索框无人操作多少秒后自动收；0 = 不自动收。"""
    try:
        v = int(_cfg().get("work_idle_sec", 60))
    except Exception:                               # noqa: BLE001
        v = 60
    return max(0, min(3600, v))


def work_take_fly() -> bool:
    """取走「刚收工了，起飞吧」这个一次性信号。

    ★ 为什么要服务端记这一下：绳窗和搜索窗是两扇窗，绳窗只看到「清单从 5 个变
      成 0 个」，它分不清这是**收工**（该飞向桌面文件夹）还是用户**一个个摘掉**的
      （什么都不该飞）。让真正发起收工的那一方留个旗子，绳窗取一次就清掉。
    """
    with _WORK_HUNG_LOCK:
        v = bool(_WORK_HUNG.get("fly"))
        _WORK_HUNG["fly"] = False
        return v


def work_stage_touch() -> dict:
    """绳窗报一声「鼠标在暂存区上」。"""
    _WORK_STAGE["ts"] = time.time()
    return {"ok": True}


def work_stage_hover(window: float = 2.0) -> bool:
    """最近 `window` 秒内有没有人把鼠标搁在暂存区上。

    绳窗是按 ~1s 节流上报的，所以判定窗口取 2s —— 比上报间隔宽一倍，网络抖一下
    也不会中途断档，同时又能在一秒左右就正确反映「鼠标移开了」。
    """
    ts = _WORK_STAGE.get("ts") or 0.0
    return bool(ts) and (time.time() - ts) < window


def work_hung_list() -> list:
    with _WORK_HUNG_LOCK:
        return [dict(it) for it in _WORK_HUNG["items"]]


def work_hang(path: str, x=None) -> dict:
    """把一件素材挂到绳子上。"""
    p = str(path or "").strip()
    if not p:
        return {"ok": False, "error": "缺少 path"}
    if p.startswith("phone:"):
        return {"ok": False, "error": "这是手机上的素材，先在手机端点「把原件同步到电脑」再挂"}
    if not os.path.exists(p):
        return {"ok": False, "error": "源文件不在了"}
    with _WORK_HUNG_LOCK:
        for it in _WORK_HUNG["items"]:
            if it["path"] == p:
                # 同一件挂两次没意义，但**也不能算错** —— 用户可能只是拖重了。
                # 报成功、回一份最新的清单，前端自己对齐。
                return {"ok": True, "dup": True, "items": [dict(i) for i in _WORK_HUNG["items"]]}
        if len(_WORK_HUNG["items"]) >= _WORK_HUNG_MAX:
            return {"ok": False, "error": "绳子挂满了（%d 个），先收工一次" % _WORK_HUNG_MAX}
        _WORK_HUNG["seq"] += 1
        item = {"id": _WORK_HUNG["seq"], "path": p, "name": os.path.basename(p),
                "kind": _kind_of(p),
                "x": float(x) if isinstance(x, (int, float)) else None}
        _WORK_HUNG["items"].append(item)
        items = [dict(i) for i in _WORK_HUNG["items"]]
    return {"ok": True, "id": item["id"], "items": items}


def work_hang_move(hid, x) -> dict:
    try:
        hid = int(hid)
    except Exception:                                       # noqa: BLE001
        return {"ok": False, "error": "id 不对"}
    with _WORK_HUNG_LOCK:
        for it in _WORK_HUNG["items"]:
            if it["id"] == hid:
                try:
                    it["x"] = float(x)
                except Exception:                           # noqa: BLE001
                    return {"ok": False, "error": "x 不对"}
                return {"ok": True}
    return {"ok": False, "error": "绳子上没这个 id"}


def work_hang_remove(hid) -> dict:
    try:
        hid = int(hid)
    except Exception:                                       # noqa: BLE001
        return {"ok": False, "error": "id 不对"}
    with _WORK_HUNG_LOCK:
        n = len(_WORK_HUNG["items"])
        _WORK_HUNG["items"] = [it for it in _WORK_HUNG["items"] if it["id"] != hid]
        gone = n - len(_WORK_HUNG["items"])
    return {"ok": True, "removed": gone, "items": work_hung_list()}


def work_hang_clear() -> dict:
    with _WORK_HUNG_LOCK:
        n = len(_WORK_HUNG["items"])
        _WORK_HUNG["items"] = []
    return {"ok": True, "removed": n, "items": []}


def work_collect_hung(out_dir: str = "", paths=None) -> dict:
    """「收工」：把绳子上挂着的全收进**暂存区**，然后清空绳子、放出「起飞」旗。

    ★ 只有**真复制成功**才清空。中途出错（磁盘满、权限不对）还把绳子清空的话，
      用户看到的是「收工了」但桌面上什么都没有，而且**挂的东西也没了**。
    ★ 2026-10-14 起「收进暂存区」不是终点：绳窗看到 `fly` 旗会放一段「明信片
      飞进左下角访达」的动画，放完回调 `/v1/work/settled`，那一刻才真正在桌面上
      建出 FXseek、把暂存区移进去、打上红标签（范老板第③条）。
      这里原来还有一句 `res["placed"] = work_folder_place()`（把桌面上的文件夹
      摆到右下角），那是「自己画一个假文件夹、再把真身也摆到同一个点」时代的
      产物 —— 真身现在由 Finder 自己摆，也不用再跟任何画出来的落点对齐，整段删。
    """
    items = work_hung_list()
    # ★ paths 是前端报的「它界面上挂着的那几条」，不给就按服务端自己那份清单来。
    #   两者不一致时**不管谁对**：这份清单只是用来决定「复制哪几个文件」，而
    #   「绳子清不清空」永远由下面那句无条件清 —— 清不干净的话绳窗会拿旧 sig
    #   把刚飞走的卡片又画回来。
    # ★ 先把「起飞」旗子放出去，再去做复制 —— 范老板第⑥条：「点击收工过很久才
    #   出现，应该是按下收工立即触发」。复制大文件要几百毫秒到几秒，等它做完才
    #   放旗子，绳窗那边就是「点了没反应」。旗子和复制互不依赖：旗子只驱动动画，
    #   「清单清不清」永远由下面那句按复制结果决定。
    with _WORK_HUNG_LOCK:
        _WORK_HUNG["fly"] = True
    # ★ 范老板第④条（2026-10-15）：「明信片回收后桌面立即出现 FXseek 文件夹，
    #   先飘旗，不要等复制完才显示」。所以这里**在复制之前**就把桌面上那个红
    #   文件夹建出来 —— 用户点下「收工」的一瞬间，桌面右上角（Finder 自己的
    #   摆放位置）就冒出红文件夹，紧接着明信片飞过去「钻」进它。等复制完再建
    #   的话，卡片早就飞没了、文件夹才慢半拍出现，顺序是反的。
    #   建目录本身是幂等的，work_settled() 里那次 makedirs 保留着当兜底。
    try:
        _d = os.path.expanduser(work_out_dir())
        os.makedirs(_d, exist_ok=True)
        _work_tag_red(_d)
    except OSError as e:
        print("[work] 收工前先建文件夹没成：%s" % e, flush=True)
    _WORK_COLLECTING["n"] += 1
    try:
        res = work_collect(paths or [it["path"] for it in items], out_dir)
    finally:
        _WORK_COLLECTING["n"] -= 1
    if res.get("ok"):
        with _WORK_HUNG_LOCK:
            _WORK_HUNG["items"] = []
    return res


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

    def _phone_file(self, device, asset_path):
        """播放手机上的素材：电脑按需去手机拉原件，边拉边转（视频就不用在内存里囤）。

        ★ 先看本地缓存：用户点过「把原件同步到电脑」（或这件素材被外派跑过打标/转写）
          之后，原件就已经躺在这台电脑上了 —— 这时候手机**关掉、拿到别处去**也照样
          能播放/预览，还能省掉一次局域网往返（4K 视频回手机要就是几秒的等待）。
          缓存路径走 `phone_original_path()`，它只读不拉，不存在的返回 None。
        """
        import urllib.request
        local = phone_original_path(device, asset_path)
        if local and os.path.exists(local):
            self._file(local)
            return
        rec = _phone_sources().get(device)
        if not rec:
            self._json(404, {"error": "没有这台手机"}); return
        url = "http://%s:%s/v1/phone/file?path=%s" % (
            rec.get("host"), rec.get("port"), urllib.parse.quote(asset_path))
        req = urllib.request.Request(url)
        tok = _phone_token()
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                self.send_header("Content-Type",
                                 resp.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                for h in ("Content-Length", "Content-Range"):
                    v = resp.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.end_headers()
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            try:
                self._json(502, {"error": "问手机要原件失败：%s" % e})
            except Exception:
                pass

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
        # ★ Authorization / X-Fxseek-Token 必须列进来：手机端带 Bearer 头发起
        #   预检时，缺了这一项浏览器/URLSession 会直接把真实请求拦下，
        #   开发者只看得到「OPTIONS 通过但请求没发出去」。
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Fxseek-Token")
        self.end_headers()

    # 局域网模式下**不需要**令牌的路径。
    # ★ /health 与 /v1/about 是「连上试试」用的：手机还没配对时要能确认
    #   「对面确实是 FXseek，版本多少」；/v1/pair/request 更得免鉴权，
    #   否则配对这件事自己就先被锁在门外了。
    _OPEN_PATHS = ("/health", "/v1/about", "/v1/pair/request")

    def _bearer(self):
        """取请求里的令牌：优先 Authorization: Bearer，其次 X-Fxseek-Token。

        为什么留第二个头：某些环境下 Authorization 会被系统代理/中间件改写，
        带一个自定义头当后备，排查时少一个变量。
        """
        h = self.headers.get("Authorization") or ""
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        return (self.headers.get("X-Fxseek-Token") or "").strip()

    def _lan_gate(self, path):
        """局域网模式下的鉴权闸门。放行返回 True，拦下并回 401 返回 False。

        ★ 只拦 `/v1/*`：静态页面（/、/settings、/vendor/*）本身不含数据，
          真正要保护的是它们背后的 /v1/settings 这类接口 —— 那些都在这道闸门后面。
        ★ 回环请求（桌面端自己的界面、菜单栏、MCP）永远放行，见 MOB.needs_auth。
        """
        if not path.startswith("/v1/"):
            return True
        if not MOB.needs_auth(self.client_address[0]):
            return True
        if path in self._OPEN_PATHS:
            return True
        try:
            dev = MOB.check_token(self._bearer())
        except Exception:
            # 校验本身炸了（比如畸形请求头）→ 按「没通过」处理。
            # 绝不能把异常放出去：BaseHTTPRequestHandler 不会兜，
            # 结果是连接被直接掐掉、客户端只看到「空响应」。
            dev = None
        if dev is None:
            self._json(401, {"error": "还没配对，或令牌已失效", "need_pair": True,
                             "service": MOB.SERVICE_TYPE})
            return False
        MOB.touch(dev)
        return True

    def _mobile_post(self, path, body):
        """手机版的那几个 POST。返回 True = 已经回过响应了。

        ★ 放在主 try 链**之外**：配对失败要回 401，而主链尾部是固定的
          200 / 400 两档，塞进去反而要绕一圈才能表达「这个码不对」。
        """
        if path == "/v1/pair/request":
            # 唯一免鉴权的写接口（见 _OPEN_PATHS）：还没配对的手机得能敲到它。
            # ★ 2026-10-13（范老板）：免费版只能绑 1 台移动终端，卡在**配对**这一步。
            #   不卡在「连上之后」是因为那样手机已经拿到令牌了，
            #   再拒绝就得额外做撤销，而且用户会以为「连上了又断」。
            #   已绑满就直接不给他令牌 —— 一次说清楚，让他去解绑。
            gate = lic.can_pair()
            if not gate.get("ok"):
                op_log("配对", "免费版终端数已满，拒绝新配对", level="warn")
                self._json(402, gate)
                return True
            r = MOB.pair_request(body.get("code"), body.get("name"),
                                 self.client_address[0], body.get("kind"))
            if r.get("ok") and r.get("device"):
                # 配对成功：把这台登记进「已绑终端」（历史绑定，离线也算）。
                # ★ explicit=True —— 重新配对是用户明确的意愿，撤销掉以前的解绑记录。
                lic.bind_terminal(r["device"].get("id"), r["device"].get("name"),
                                  explicit=True)
                r["terminals"] = lic.terminals()
            self._json(200 if r.get("ok") else 401, r)
            return True
        if path == "/v1/mobile/search":
            # 需要令牌（_lan_gate 已经拦过一道），所以放在这里而不是主 try 链：
            # 语义检索会抛「查询为空」之类的 ValueError，手机侧希望拿到 400 而不是 500。
            try:
                self._json(200, mobile_search(body))
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                op_log("检索", "手机外派检索失败：%s" % e, level="warn")
                self._json(500, {"error": "检索失败：%s" % e})
            return True
        if path in ("/v1/pair/revoke", "/v1/mobile/lan", "/v1/pair/code"):
            # 这三条改的是「谁还能连进来」，只认本机 —— 已配对的手机也不行，
            # 否则丢一台手机等于把整个局域网入口交出去。
            if not MOB.is_loopback(self.client_address[0]):
                self._json(403, {"error": "这个接口只在本机可用"})
                return True
            if path == "/v1/pair/revoke":
                r = MOB.revoke(body.get("id") or None)
            elif path == "/v1/pair/code":
                r = MOB.pair_code(force=True)
            else:
                r = MOB.set_lan(bool(body.get("on")), port=_SRV["port"],
                                name=MOB.host_label(), version=DEFAULT_SETTINGS["version"],
                                items=mobile_caps().get("item_count", 0))
            self._json(200, r)
            return True
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if not self._lan_gate(u.path):
            return
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
        elif u.path == "/v1/license":
            self._json(200, license_status())
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
        elif u.path == "/v1/align/status":
            # 设置页的「已对齐 / 未对齐」统计与下载进度都走这里。
            # ★ 必须同时在 _QUIET_PATHS 里，否则这个 8 秒一次的轮询
            #   会把闲置计时清零，自动打标/转写再也等不到空闲。
            self._json(200, align_status())
        elif u.path == "/v1/mobile/caps":
            # 手机连接后的第一件事：问「你会什么」。
            # 回到这里说明已经配对（或本机访问），所以可以放心报家底。
            caps = mobile_caps()
            caps["paired"] = MOB.check_token(self._bearer()) is not None
            self._json(200, caps)
        elif u.path == "/v1/mobile/state":
            # ★ 只给回环 —— 这个响应里有**配对码**，给了局域网里的设备
            #   等于让已配对的手机也能再拉一台陌生设备进来。
            if not MOB.is_loopback(self.client_address[0]):
                self._json(403, {"error": "这个接口只在本机可用"})
            else:
                self._json(200, mobile_state())
        elif u.path == "/v1/pair/devices":
            self._json(200, {"devices": MOB.devices(),
                             "enabled": MOB.lan_enabled()})
        elif u.path == "/v1/mobile/vectors":
            # 手机离线检索用的 EG2 向量（按 rev 增量拉）。
            self._json(200, mobile_vectors(urllib.parse.parse_qs(u.query)))
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
        elif u.path == "/v1/phone/sources":
            # 「手机来源」清单：侧边栏要拿它画那台手机的文件夹
            self._json(200, phone_sources_list())
        elif u.path == "/v1/phone/items":
            dev = qs.get("device", [""])[0]
            self._json(200, phone_items(dev, refresh=qs.get("refresh", ["1"])[0] != "0"))
        elif u.path == "/v1/phone/job":
            self._json(200, phone_jobs_view(qs.get("device", [""])[0]))
        elif u.path == "/v1/phone/sync":
            dev = qs.get("device", [""])[0]
            self._json(200, {"state": phone_sync_state(dev),
                             "cache": phone_cache_stats(dev) if dev else {}})
        elif u.path == "/v1/phone/thumb":
            dev = qs.get("device", [""])[0]
            blob = phone_thumb(dev, qs.get("path", [""])[0])
            if blob:
                # 手机素材的缩略图不会变，让浏览器也缓存一手
                self._bytes(200, blob, "image/jpeg", {"Cache-Control": "max-age=86400"})
            else:
                self._bytes(404, b"", "image/jpeg")
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
        elif u.path == "/work":
            # 工作模式的**搜索窗**。同一个 no-cache 待遇：改一行 CSS 就得能立刻看到，
            # 不然调这个界面的时间全花在「怎么没生效」上了。
            self._bytes(200, _work_html().encode(), "text/html; charset=utf-8",
                        extra={"Cache-Control": "no-cache"})
        elif u.path == "/rope":
            # 工作模式的**暂存区窗**：钢丝绳 + 挂着的明信片。只负责显示，穿透点击。
            self._bytes(200, _rope_html().encode(), "text/html; charset=utf-8",
                        extra={"Cache-Control": "no-cache"})
        elif u.path == "/v1/work/pointer":
            # 光标现在在哪（绳窗 client 坐标）+ 搜索层是不是开着（= 暂存区可编辑）。
            # 绳窗是 `setIgnoresMouseEvents_(True)` 的，永远收不到 mousemove，
            # 所以「鼠标划过明信片像拨铃铛一样摆」（范老板第⑤条）和「停在暂存区
            # 3 秒自动叫出搜索层」（第④条）都只能由宿主把坐标报给它。
            self._json(200, (_panel_hook("work_pointer") if "work_pointer" in PANEL_HOOKS
                             else {"ok": True, "on": False}))
        elif u.path == "/v1/work/dockpos":
            # 收工动画的落点：左下角 Dock 最左边那个「访达」图标（绳窗 client 坐标）。
            self._json(200, work_dock_pos())
        elif u.path == "/v1/work/stage":
            # 绳窗报「鼠标在暂存区上」（约 1s 一次）。见 _WORK_STAGE 的注释。
            self._json(200, work_stage_touch())
        elif u.path == "/v1/work/hung":
            # GET 是**偷看**：只报清单，不消费「该起飞了」那个一次性信号。
            # 搜索窗也盯着这份清单（要画悬停和 × 的位置），要是它也来消费，
            # 两扇窗就会抢那面旗子 —— 绳窗有一半概率收不到，收工就不飞了。
            self._json(200, {"ok": True, "items": work_hung_list(),
                             "hover": work_stage_hover()})
        elif u.path == "/v1/work/state":
            # 读工作模式状态。★ GET 和 POST **都得挂** —— 设置页那边是一次纯读，
            # 用的是 `fetch('/v1/work/state')`（GET），而这条路由一开始只写在
            # POST 分支里，于是拿回 404 的 `{"error":"not found"}`：页面上**一点
            # 错都不报**，只是唤醒键那格永远显示「（未设置）」、模式选中态停在
            # 默认值。这种「静默错值」比抛异常难查得多，所以两个方法都放行，
            # 别让调用方去记住该用哪个。
            self._json(200, work_state())
        elif u.path == "/v1/thumb":
            _p = qs.get("path", [""])[0]
            if _p.startswith("phone:") and "|" in _p:
                # 手机素材的封面：转手问手机要（电脑这边缓存一份）。
                # ★ 拿不到（手机不在线 / 还没缓存过）时，只要**原件已经同步到电脑**，
                #   就用本地那份现生成一张 —— 这正是用户要的「离线时只保留缓存预览」：
                #   同步过原件的素材，手机拿走、断网、关机，封面照样在。
                _dev, _ap = _p[6:].split("|", 1)
                _blob = phone_thumb(_dev, _ap)
                if not _blob:
                    _row = phone_one(_dev, _ap) or {}
                    _loc = phone_original_path(_dev, _ap, _row.get("name") or "")
                    if _loc:
                        self._thumb(_loc, w=560)
                        return
                self._bytes(200 if _blob else 404, _blob or b"", "image/jpeg",
                            {"Cache-Control": "max-age=86400"} if _blob else None)
                return
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
            _p = qs.get("path", [""])[0]
            if _p.startswith("phone:") and "|" in _p:
                _dev, _ap = _p[6:].split("|", 1)
                info = phone_one(_dev, _ap)
                self._json(200 if info else 404, info or {"error": "手机上没这条素材"})
                return
            try:
                self._json(200, file_detail(_p))
            except Exception as e:
                self._json(400, {"error": str(e)})
        elif u.path == "/v1/file":
            _p = qs.get("path", [""])[0]
            if _p.startswith("phone:") and "|" in _p:
                # 播手机上的素材：电脑按需去手机拉原件，再原样转给浏览器（Range 也转）
                self._phone_file(_p[6:].split("|", 1)[0], _p[6:].split("|", 1)[1])
                return
            # ★ 手机端会在 `streamURL` 里带上 `remux=1`：mkv/avi 这种 AVPlayer 不认的
            #   容器，让电脑先无损换个 mp4 壳再发过来（见 `_remux_to_mp4`）。
            self._file(_p, remux=(qs.get("remux", ["0"])[0] == "1"))
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
            # 绳上那只四足行走的卡通狒狒（8 帧雪碧图，tools/make_rope_baboon.py 生成）
            "rope_baboon.png": "image/png",
            # ★ 2026-10-16：菜单栏那套 18 帧剪影、双色填充后给绳上用（rope.html 引的）
            #   漏进白名单就是 404 → 背景图加载失败 → 狒狒整只不显示。别删。
            "rope_baboon18.png": "image/png",
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

    def _file(self, path, remux=False):
        """原文件响应，支持 HTTP Range（视频/音频可拖拽播放、边下边播）。

        `remux=True` 时，先看要不要换成 mp4 壳（mkv/avi 这类 AVPlayer 不认的容器）。
        转封装失败就老老实实回原文件 —— 宁可放不出画面，也不能给个 404 让人以为文件没了。
        """
        if not path or not _path_allowed(path) or not os.path.exists(path):
            self._json(404, {"error": "bad path"}); return
        if os.path.isdir(path):
            self._json(400, {"error": "is a directory"}); return
        if remux and _needs_remux(path):
            _mp4 = _remux_to_mp4(path)
            if _mp4:
                path = _mp4
        ctype = _mime_for(path)
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
        if not self._lan_gate(u.path):
            return
        if self._mobile_post(u.path, body):
            return

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
                src = body.get("source") or ""
                if isinstance(src, str) and src.startswith("phone:"):
                    # 手机来源：直接吐手机清单（在线顺手刷新缓存，离线用缓存）
                    # ★ kind 要接着往下传 —— 不传就等于顶栏的「视频 / 图片 / …」全失效。
                    res = phone_items(src.split(":", 1)[1], kind=body.get("kind"))
                else:
                    res = browse_all(body.get("kind"), body.get("limit", 300),
                                     body.get("sort", "recent"),
                                     source=src)
            elif u.path == "/v1/open":
                res = open_with_system(body.get("path", ""))
            elif u.path.startswith("/v1/mic/"):
                # 桌面版原生录音（WKWebView 从访达启动时拿不到麦克风，详见 recorder.py）。
                # 钩子在 launcher.py 的 _install_panel_hooks 里注册；浏览器里打开时没人注册，
                # 会回一句「只在桌面版里可用」，前端据此改走 getUserMedia。
                res = _panel_hook("mic_" + u.path.rsplit("/", 1)[-1].replace("-", "_"))
            elif u.path in ("/v1/panel/show", "/v1/panel/hide", "/v1/panel/panel-toggle",
                            "/v1/panel/settings",
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
            elif u.path == "/v1/phone/announce":
                # 手机连上后自报家门（见 phone_announce 的注释）。
                # 顺手把「这是哪条配对记录」认出来 —— 拉原件时要拿它的令牌。
                _ann_dev = MOB.check_token(self._bearer())
                # ★ 顶栏那几枚手机角标走的是 `mobile_devices` 的 `_LIVE` 窗口
                #   （`MOB.is_online`，60 秒），而它是靠 `MOB.touch()` 喂的 ——
                #   `touch` 原本只在全局闸门里调一次。可 `/v1/phone/announce`
                #   在 `_OPEN_PATHS` 里（见那里的注释：机器在问，别清零闲置计时），
                #   闸门直接放行、**走不到 touch**。于是出现一个很别扭的现象：
                #   手机明明连着、侧栏绿点亮着，顶栏却一枚手机图标都不显示
                #   （实测就是 badge=[] 而 phstat=on）。这里自己补一次。
                MOB.touch(_ann_dev)
                res = phone_announce(body, self.client_address[0], _ann_dev)
            elif u.path == "/v1/license/activate":
                res = license_activate(body.get("code", ""))
            elif u.path == "/v1/license/deactivate":
                res = license_deactivate()
            elif u.path == "/v1/license/share":
                # 电脑端「分享给朋友」：点一下就算一次分享。
                # ★ 不用再弹个「你确定发出去了吗」——冷却本来就是「一天一次」，
                #   多点几下不会多给天数（第二次回来就是 cooling），
                #   硬加一步确认只会让人以为按钮坏了。
                # kind: friend = 复制文案发给朋友（15 天）/ moments = 存海报发朋友圈（30 天）
                res = license_share("desktop", body.get("kind", ""))
                res["text"] = _share_text_fallback()
            elif u.path == "/v1/license/poster":
                # 现画一张海报存到桌面，返回路径给界面显示「已存到 …」。
                res = license_share_poster(body.get("path", ""))
            elif u.path == "/v1/license/buy/start":
                # 开一笔购买：本机生成订单号，拼出发货页地址交回去，让 UI 用浏览器打开
                # ★ 必须写回 res —— 这层 handler 最后统一补 elapsed_ms 再 _json，
                #   直接调 self._json() 会在下面 `res["elapsed_ms"]` 那行炸
                #   UnboundLocalError（响应其实已经发出去了，但日志里多一条
                #   traceback、还会再发一次 400，把人往沟里带）。
                res = buy_start(body.get("url", ""), body.get("price", ""))
            elif u.path == "/v1/license/buy/poll":
                # 问发货页发了没；发了就自动激活（用户一次都不用粘）
                res = buy_poll(body.get("order", ""), body.get("nonce", ""))
            elif u.path == "/v1/license/ticket":
                # 手机没连电脑时，用户把手机上那段凭证码抄过来贴这儿。
                res = license_ticket(body.get("code", ""))
            elif u.path == "/v1/license/unbind":
                res = license_unbind(body.get("id", ""))
            elif u.path == "/v1/phone/bye":
                # 手机主动断开：立刻把它标成离线，别等 60 秒的 TTL（见 phone_bye）。
                # ★ 不在这儿再查一遍令牌 —— 上面那道全局闸门已经查过了
                #   （`needs_auth` + `_OPEN_PATHS`：开了局域网就必须带令牌，
                #   回环免查）。`/v1/phone/forget`、`/v1/phone/job` 也都是这么处理的，
                #   多查一遍只会让「回环 + 旧令牌」这种自家调试场景莫名失败。
                # ★ 得把来源 IP 一起传进去：表的键是 IP，而手机报的是
                #   identifierForVendor，两边对不上要靠 IP 兜底认人。
                #   ★ 令牌是「能认就认」：回环调试场景可能带着旧令牌，认不出来
                #     也不影响侧栏那条（IP 兜底照旧生效），只是顶栏角标要多亮
                #     一会儿。所以这里 try 一下、失败给 None。
                try:
                    _bye_dev = MOB.check_token(self._bearer())
                except Exception:
                    _bye_dev = None
                res = phone_bye(body, self.client_address[0], _bye_dev)
            elif u.path == "/v1/phone/job":
                # 手机派活给电脑：{"device":…, "path":…, "what":"tag|asr|align","name":…}
                res = phone_job_add(body.get("device", ""), body.get("path", ""),
                                    body.get("what", "tag"), body.get("name", ""),
                                    body.get("text", ""))
            elif u.path == "/v1/phone/search":
                # 路线 A：电脑把「搜什么」转给手机，手机用自己的 EG2 算完把结果送回来
                res = phone_search(body.get("device", ""), body)
            elif u.path == "/v1/phone/forget":
                # 用户主动移除这台手机来源（缓存一起删；手机本身不受影响）
                # ★ 连带清掉同一台机器的**历史重复记录**（范老板 2026-10-13）：
                #   以前表的键是重装就变的 identifierForVendor，同一台手机能堆好几条。
                #   侧栏虽然去重只画一条，但只删掉那一条的话，下次自报家门时
                #   另一条老记录又会被拾起来、文件夹又冒出来。所以按机器号全清。
                did = body.get("device", "")
                table = _phone_sources()
                key, rec0 = _phone_find(table, str(did or ""))
                keys = [key] if key else []
                if isinstance(rec0, dict):
                    devid = str(rec0.get("device_id") or "")
                    for k, r in list(table.items()):
                        if not isinstance(r, dict) or k in keys:
                            continue
                        if devid and str(r.get("device_id") or "") == devid:
                            keys.append(k)
                            continue
                        # 同一 IP 上的其它残留（换过手机的那条）
                        if key and str(r.get("host") or "") == str(rec0.get("host") or ""):
                            keys.append(k)
                for k in keys:
                    table.pop(k, None)
                    shutil.rmtree(os.path.join(PHONE_CACHE_DIR,
                                               "".join(c for c in str(k)
                                                       if c.isalnum() or c in "-_")),
                                  ignore_errors=True)
                if not keys:
                    # 键不在表里也把缓存按传入值清一遍，保持老行为
                    shutil.rmtree(os.path.join(PHONE_CACHE_DIR,
                                               "".join(c for c in str(did) if c.isalnum() or c in "-_")),
                                  ignore_errors=True)
                save_settings({"phone_sources": table})
                res = {"ok": True, "removed": did, "keys": keys}
            elif u.path == "/v1/phone/sync":
                # 用户主动点「把原件同步到电脑」——不自动全量下，几百 MB 到几 GB 的事
                res = phone_sync_start(body.get("device", ""))
            elif u.path == "/v1/phone/sync/stop":
                res = phone_sync_stop(body.get("device", ""))
            elif u.path == "/v1/phone/sync/reset":
                res = phone_sync_reset(body.get("device", ""))
            elif u.path == "/v1/phone/orig/clear":
                # 只删原件缓存；缩略图/清单/它自己的素材和索引一概不动
                res = phone_orig_clear(body.get("device", ""))
            elif u.path == "/v1/phone/cache/clear":
                # 「清除缓存」把它一起管了：缩略图 + 原件缓存都删，手机上的东西不动。
                # （用户 2026-10-11 的要求：手机来源的缓存纳入「清除缓存」）
                did = body.get("device", "")
                thumbs = os.path.join(PHONE_CACHE_DIR,
                                      "".join(c for c in str(did) if c.isalnum() or c in "-_"), "thumbs")
                n = 0
                if os.path.isdir(thumbs):
                    for fn in os.listdir(thumbs):
                        try:
                            os.remove(os.path.join(thumbs, fn)); n += 1
                        except Exception:
                            pass
                o = phone_orig_clear(did)
                res = {"ok": True, "cleared": n, "orig_removed": o.get("removed", 0),
                       "orig_mb": o.get("mb", 0)}
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
            # ---- 工作模式（1.11 预览版）------------------------------------
            elif u.path == "/v1/work/state":
                res = work_state()
            elif u.path == "/v1/work/mode":
                res = work_set_mode(body.get("mode", ""))
            elif u.path == "/v1/work/toggle":
                # 菜单栏单击那一下走这里（判断 + 写入在同一个函数里，见 work_toggle）
                res = work_toggle()
            elif u.path == "/v1/work/hitrects":
                # 搜索窗报上来的「可点击矩形」（CSS 像素、窗口左上原点）。
                # 窗口层拿去追鼠标，决定这一刻要不要让点击穿透过去。
                # 没有窗口层（浏览器里跑）就记在内存里，方便调试时看。
                rects = body.get("rects")
                _WORK_HIT["rects"] = rects if isinstance(rects, list) else None
                try:
                    import workwin as _WW
                    _WW.set_hit_rects(rects)
                except Exception as e:                  # noqa: BLE001
                    print("[app] 传命中区给窗口层失败：%s" % e, flush=True)
                res = {"ok": True, "n": len(rects or [])}
            elif u.path == "/v1/work/searchzone":
                # ★ 2026-10-18：「搜索区」矩形（CSS 像素、搜索窗左上原点）。
                #   工作模式下靠它判断「用户是不是在活动」——光标落在这一块里
                #   动才算人在电脑前；「暂存区」（绳窗挂明信片那一带）不参与。
                #   页面每 600ms 报一次，只在变了的时候真发（见 work.html
                #   pushSearchZone），所以这里不用节流。
                rect = body.get("rect")
                if isinstance(rect, (list, tuple)) and len(rect) >= 4:
                    try:
                        _WORK_HIT["search"] = [round(float(v), 1) for v in rect[:4]]
                    except (TypeError, ValueError):
                        _WORK_HIT["search"] = None
                elif rect is None:
                    _WORK_HIT["search"] = None
                try:
                    import workwin as _WW
                    _WW.set_search_rect(_WORK_HIT.get("search"))
                except Exception as e:                  # noqa: BLE001
                    print("[app] 传搜索区给窗口层失败：%s" % e, flush=True)
                res = {"ok": True, "rect": _WORK_HIT.get("search")}
            elif u.path == "/v1/work/ropeedit":
                # ★ 范老板第①条（2026-10-15）：暂存区两态。绳窗自己按「鼠标
                #   在暂存区里停留满 3 秒」判定，判完把意愿报上来；层级由
                #   workwin 在主线程落（app.py 这层是 HTTP 线程，不能碰 AppKit）。
                try:
                    import workwin as _WW
                    res = _WW.set_rope_edit(bool(body.get("on")))
                except Exception as e:                  # noqa: BLE001
                    res = {"ok": False, "error": str(e)}
            elif u.path == "/v1/work/picking":
                # ★ 范老板新③：「以图搜 / 以音搜有问题」。
                #   搜索页在弹出原生文件选择框**之前**打这一枪（`on:true`），
                #   选完/取消之后再打一枪（`on:false`）。理由见 workwin
                #   `_outside_click()`：那个框属于本 app，用户点「打开」时那一记
                #   mouseDown 的坐标必然落在搜索区矩形之外，会把整层搜索误判成
                #   「点了外面」直接收走 —— 于是「选了文件回来啥也没有」。
                #   `NSApp.modalWindow()` 兜不住 NSOpenPanel（它是 sheet/异步模态，
                #   不是 runModalForWindow 那种模态窗口），所以只能由页面明说。
                try:
                    import workwin as _WW
                    res = _WW.set_picking(bool(body.get("on")))
                except Exception as e:                  # noqa: BLE001
                    res = {"ok": False, "error": str(e)}
            elif u.path == "/v1/work/ropehit":
                # 绳窗报上来的「可点击矩形」。绳窗整页是穿透点击的，只有
                # 「收工」按钮那一条要放行（范老板第⑤条：它得一直在桌面上、
                # 鼠标点得到，而不是非得搜索层开着才出现）。
                rects = body.get("rects")
                try:
                    import workwin as _WW
                    _WW.set_rope_hit_rects(rects)
                except Exception as e:                  # noqa: BLE001
                    print("[app] 传绳窗命中区失败：%s" % e, flush=True)
                res = {"ok": True, "n": len(rects or [])}
            elif u.path == "/v1/work/idle":
                try:
                    sec = int(body.get("sec", 60))
                except Exception:                       # noqa: BLE001
                    sec = 60
                sec = max(0, min(3600, sec))
                save_settings({"work_idle_sec": sec})
                res = {"ok": True, "idle_sec": work_idle_sec()}
            elif u.path == "/v1/work/hotkey":
                res = work_set_hotkey(body.get("hotkey", ""))
            elif u.path == "/v1/work/hotkey/pick":
                # 菜单栏「设置唤醒键…」走这条：**录键必须在主进程里做** ——
                # 菜单栏那边是个 Prohibited 激活策略的辅助进程，拿不到键盘焦点
                # （详见 launcher.py 里 _work_hotkey_pick 的说明）。
                _pk = (_panel_hook("work_hotkey_pick")
                       if "work_hotkey_pick" in PANEL_HOOKS else {})
                if isinstance(_pk, dict) and _pk.get("ok"):
                    res = work_set_hotkey(_pk.get("hotkey") or "")
                elif isinstance(_pk, dict) and _pk.get("cancelled"):
                    res = {"ok": False, "cancelled": True}
                else:
                    res = {"ok": False,
                           "error": ((_pk or {}).get("error")
                                     if isinstance(_pk, dict) else "") or "录键没能启动"}
            # ---- 暂存区（绳子上挂着的）----
            elif u.path == "/v1/work/hung":
                res = {"ok": True, "items": work_hung_list(), "fly": work_take_fly(),
                       "hover": work_stage_hover()}
            elif u.path == "/v1/work/hang":
                res = work_hang(body.get("path", ""), body.get("x"))
            elif u.path == "/v1/work/hang/move":
                res = work_hang_move(body.get("id"), body.get("x"))
            elif u.path == "/v1/work/hang/remove":
                res = work_hang_remove(body.get("id"))
            elif u.path == "/v1/work/hang/clear":
                res = work_hang_clear()
            elif u.path == "/v1/work/settled":
                # ★ 绳窗在「全部明信片飞进访达」那段动画放完之后调这里：这一刻
                #   才真正在桌面上建出 FXseek、把暂存区的东西移进去、打上系统
                #   红色标签（范老板第③条：「等全部明信片飞完后」）。
                res = work_settled()
            elif u.path == "/v1/work/collect":
                # 「收工」。前端可以传 paths（历史写法），不传就收绳子上挂着的全部。
                # out_dir 允许前端传，但固定那个才是常态 —— 传值这条路是留给
                # 自动化测试的，界面上不暴露。
                # ★★ 两条路必须**汇到同一个函数**。早期写成「带 paths 就直接
                #   work_collect()」，而那条岔路**不放 `fly` 旗子** —— 界面走的
                #   偏偏就是它（work.html 的 collect() 永远带 paths）。后果是：
                #   点收工绳窗什么动画都不放（卡片等下一次轮询发现清单空了，
                #   直接凭空消失），而且这批东西卡在暂存区不落桌面，要等**下一次**
                #   收工才被 work_collect() 里的兜底 work_flush() 冲出去。
                #   范老板 2026-10-14 那句「我并没有看到明信片像纸牌回收一样的
                #   动效」就是这儿，不是动画写得不好。
                res = work_collect_hung(body.get("dir", ""), body.get("paths"))
            elif u.path == "/v1/work/wake":
                # 唤醒键的唯一动作：**叫出搜索框**。
                # ★ 已经不是「切换」了 —— 范老板 2026-10-14 明确要求
                #   「搜索框已存在的情况下按快捷键无效」。原因很实在：想关掉它
                #   时人会去按 Esc 或点空白，不会去按唤醒键；而把唤醒键做成切换，
                #   手快按两下就会「出来又没了」，看着像坏了。
                res = (_panel_hook("work_wake") if "work_wake" in PANEL_HOOKS
                       else {"ok": True, "window": False,
                             "note": "窗口层不在（不是桌面版）"})
            elif u.path == "/v1/work/dismiss":
                res = (_panel_hook("work_dismiss") if "work_dismiss" in PANEL_HOOKS
                       else {"ok": True, "window": False,
                             "note": "窗口层不在（不是桌面版）"})
            elif u.path == "/v1/work/show":
                # 兼容旧名字：show = 进工作模式（叫出暂存区，不叫搜索框）
                res = (_panel_hook("work_show") if "work_show" in PANEL_HOOKS
                       else {"ok": True, "window": False,
                             "note": "窗口层不在（不是桌面版）"})
            elif u.path == "/v1/work/hide":
                res = (_panel_hook("work_hide") if "work_hide" in PANEL_HOOKS
                       else {"ok": True, "window": False,
                             "note": "窗口层不在（不是桌面版）"})
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
            elif u.path == "/v1/align/download":
                # 对齐模型的下载控制。action = start | stop | status | source
                # （默认 status，只读）。source 只在 action=source 时用。
                res = align_download(body.get("action", "status"),
                                     body.get("source"))
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
    # 服务实例存进 _SRV：菜单栏的「停止服务器 / 启动服务器」要能把它关掉再架回来。
    # ★ 监听地址由「局域网开关」决定：开了就绑 0.0.0.0（手机才连得上），
    #   关着就只绑回环。--host 显式指定过就尊重调用方。
    if host == "127.0.0.1":
        host = _bind_host()
    _SRV.update({"host": host, "port": port})
    # 局域网开着的话顺手把 Bonjour 广播挂上 —— 手机就能自动发现，
    # 不用在手机上敲一个 192.168.x.x。失败不影响服务本身。
    try:
        if MOB.lan_enabled():
            _adv = MOB.state(port=port, name=MOB.host_label(),
                             version=DEFAULT_SETTINGS["version"],
                             items=mobile_caps().get("item_count", 0))
            if _adv.get("advertise_error"):
                print("[局域网] Bonjour 广播失败：%s" % _adv["advertise_error"])
            else:
                print("[局域网] 已开放，手机可连：http://%s:%d/" % (MOB.lan_ip(), port))
    except Exception as _e:
        print("[局域网] 跳过（%s）" % _e)
    _srv = _Server((host, port), Handler)
    _SRV["inst"] = _srv
    _SRV["thread"] = threading.current_thread()
    try:
        _srv.serve_forever()
    finally:
        if _SRV.get("inst") is _srv:
            _SRV["inst"] = None
    # ★ 别急着返回 —— 「局域网开关」切绑定地址时是这个线程里的 serve_forever
    #   被 shutdown() 掉、另起了一个新实例。如果这里直接返回，run_server 就结束了、
    #   main() 就结束了、**进程退出会把刚架起来的新监听一起带走**（实测：一开局域网
    #   端口就没了）。所以守到「一个活着的服务实例都没有、也没有正在重开」为止。
    while _SRV.get("inst") is not None or _SRV.get("rebind"):
        time.sleep(0.5)


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
