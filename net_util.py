#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""按目标地址分流代理：本机/局域网直连，公网才走系统代理。

为什么需要这一层
----------------
macOS 上装了 DevSidecar / Clash / Surge 这类加速工具后，系统代理会被设成
本机端口（本项目实测 http://127.0.0.1:31188），而 Python 的 urllib / requests
默认**对所有主机**都套用它 —— `urllib.request.getproxies()` 会返回那个代理，
连 `proxy_bypass("127.0.0.1")` 都是 False。后果是：

  * 请求本机 oMLX（http://127.0.0.1:9977）也会被绕去代理，白多一跳；
  * 代理自己带超时（DevSidecar 实测 60 秒）。4 分钟的歌转写本来就要
    60 秒以上，于是稳定撞成
    `HTTP 504: DevSidecar: no response from upstream`；
  * 局域网里的推理机同理。

公网请求（OpenAI / Gemini 等官方 API）反而**需要**这层代理才能通，
所以不能一刀切关掉代理，只能按目标地址分流。
"""

import ipaddress
import urllib.parse
import urllib.request

_LOCAL_NAMES = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def is_local_url(url: str) -> bool:
    """True = 本机或私有网络地址（这些不该走系统代理）。

    覆盖：localhost / *.local / 127.0.0.0/8 / ::1 / 私有网段 10.* 192.168.*
    172.16-31.* / 链路本地 169.254.*（含云元数据地址）/ fc00::/7 / fe80::/10。
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    host = host.strip().strip("[]").lower()
    if not host or host in _LOCAL_NAMES or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False          # 普通域名 → 交给系统代理
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def urlopen_smart(req, timeout=None):
    """urlopen 的替身：目标是本机/局域网就直连，否则照旧走系统代理。"""
    url = ""
    if isinstance(req, str):
        url = req
    else:
        url = getattr(req, "full_url", "") or getattr(req, "url", "") or ""
    if is_local_url(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
