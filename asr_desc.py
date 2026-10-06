#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FXseek. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
音频转写（ASR）—— 对接 OpenAI 兼容 /v1/audio/transcriptions（如 oMLX 的 Qwen3-ASR）

用途：把音频内容转成文字，写入索引 meta，让「按歌词/说话内容搜索」成为可能。
依赖：仅标准库。
"""
import io
import json
import os
import urllib.error
import urllib.request
import uuid


class ASRError(Exception):
    pass


def _ffmpeg_bin() -> str:
    """拿 ffmpeg 的**绝对路径**：应用自带的 bin/ > $FFMPEG > 系统 PATH 与常见位置
    （解析逻辑跟 indexer 同一份，别自己再写一遍）。

    ★ 为什么不能直接写 `"ffmpeg"`：双击图标启动的打包版走 LaunchServices，
    进程 PATH 里根本没有 `/opt/homebrew/bin`，于是报
    `[Errno 2] No such file or directory: 'ffmpeg'` —— 明明应用自己带了一份。
    （`paths.py` 现在也会把 `bin/` 补进 PATH，这里是第二道保险：拿到绝对路径。）"""
    try:
        import indexer as ix
        return ix.FFMPEG
    except Exception:
        return "ffmpeg"


def _transcribe_audio_file(base_url: str, api_key: str, model: str,
                           audio_bytes: bytes, filename: str,
                           language: str = "", timeout: int = 300) -> str:
    """调用 /v1/audio/transcriptions，返回转写文本。"""
    base = base_url.rstrip("/")
    url = (base + "/audio/transcriptions") if base.endswith("/v1") \
        else (base + "/v1/audio/transcriptions")

    boundary = "----dshAsr" + uuid.uuid4().hex
    # 构造 multipart/form-data
    buf = io.BytesIO()
    def field(name, value):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(f"{value}\r\n".encode())
    def file_field(name, fname, data, ctype):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")

    field("model", model)
    if language:
        field("language", language)
    field("response_format", "json")
    file_field("file", filename, audio_bytes, "audio/mpeg")
    buf.write(f"--{boundary}--\r\n".encode())

    body = buf.getvalue()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
            txt = d.get("text") or ""
            # 有些服务返回 segments
            if not txt and d.get("segments"):
                txt = " ".join(s.get("text", "") for s in d["segments"] if s.get("text"))
            return txt.strip()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise ASRError(f"HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise ASRError(f"无法连接 {url}（{e.reason}）")
    except Exception as e:
        raise ASRError(str(e))


def transcribe(base_url: str, api_key: str, model: str, audio_path: str,
               language: str = "") -> dict:
    """转写本地音频文件。返回 {text, model}。"""
    if not os.path.exists(audio_path):
        raise ASRError("音频文件不存在")
    with open(audio_path, "rb") as f:
        data = f.read()
    if not data:
        raise ASRError("音频文件为空")
    txt = _transcribe_audio_file(base_url, api_key, model, data,
                                 os.path.basename(audio_path), language)
    return {"text": txt, "model": model}


def test_connection(base_url: str, api_key: str, model: str) -> dict:
    """测试 ASR 连通性（用 1 秒静音/正弦波）。"""
    import subprocess, tempfile
    ff = _ffmpeg_bin()
    try:
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "test.mp3")
            subprocess.run([ff, "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", "-y", fp],
                           capture_output=True, timeout=20)
            if not os.path.exists(fp):
                return {"ok": False, "message": f"ffmpeg 生成测试音频失败（用的 {ff}）"}
            r = transcribe(base_url, api_key, model, fp, language="")
            return {"ok": True, "message": f"连接成功，模型「{model}」响应正常",
                    "text": r["text"]}
    except FileNotFoundError:
        return {"ok": False,
                "message": f"本机找不到 ffmpeg（试过 {ff}），没法生成测试音频"}
    except ASRError as e:
        return {"ok": False, "message": f"转写失败：{e}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
