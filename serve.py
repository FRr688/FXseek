#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
WeMM-Embedding-2B-Apple-Silicon-MLX 本地 embedding HTTP 服务 (OpenAI 兼容)

★ 纯标准库实现（http.server），不引入 fastapi/uvicorn —— 保持 venv 干净可打包。
★ 只依赖 wemm_app/venv 独立环境，不碰 oMLX，不改原模型目录。

启动：
    wemm_app/venv/cpython-3.11/bin/python3.11 serve.py --port 8231
    # 或复用 embed.py 的加载逻辑
    wemm_app/venv/cpython-3.11/bin/python3.11 embed.py --serve --port 8231

接口（OpenAI 兼容）：
    GET  /health                    -> {"status":"ok",...}
    GET  /v1/models                 -> 模型列表
    POST /v1/embeddings             -> 向量
        {
          "model": "wemm-embedding-2b",
          "input": "一段文本",                 # 纯文本
          "dimensions": 512,                   # 可选，套娃截断
          "encoding_format": "float"           # 或 "base64"
        }
      多模态（文本+图片）：
        {
          "input": [{"type":"text","text":"描述"},
                    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}],
          "dimensions": 2048
        }
        input 也支持数组批量：["文本1", "文本2"]
"""
import argparse
import base64
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import embed as we  # 复用 embed.py 的 load_model / embed


_STATE = {"model": None, "processor": None, "model_path": None}
SERVICE_NAME = "wemm-embedding-2b"


def _decode_image_url(url: str):
    """把 data:base64 或 http(s) URL 转成 PIL.Image。"""
    from PIL import Image
    if url.startswith("data:"):
        b64 = url.split(",", 1)[1]
        raw = base64.b64decode(b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    if url.startswith("file://"):
        return Image.open(url[7:]).convert("RGB")
    if os.path.exists(url):
        return Image.open(url).convert("RGB")
    # 远程 URL（本机网络可能受限）
    import urllib.request
    with urllib.request.urlopen(url, timeout=20) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def _split_input(item):
    """把一条 input 解析成 (text, image, video)。"""
    if isinstance(item, str):
        return item, None, None
    if isinstance(item, dict):
        t = item.get("type")
        if t == "text" or "text" in item:
            return item.get("text", ""), None, None
        if t in ("image_url", "image"):
            u = item.get("image_url", {})
            url = u.get("url") if isinstance(u, dict) else u
            return None, _decode_image_url(url), None
        if t == "video_url":
            u = item.get("video_url", {})
            url = u.get("url") if isinstance(u, dict) else u
            return None, None, url
    raise ValueError(f"unsupported input item: {item!r}")


def _handle_embeddings(body: dict) -> dict:
    import mlx.core as mx

    model, processor = _STATE["model"], _STATE["processor"]
    inp = body.get("input")
    if inp is None:
        raise ValueError("missing 'input'")
    dim = body.get("dimensions") or body.get("dim")
    if isinstance(inp, str) or isinstance(inp, dict):
        items = [inp]
    else:
        items = list(inp)

    # 单条 input 里可能是 [text, image] 混合消息，合并成一次多模态编码
    data = []
    for i, item in enumerate(items):
        if isinstance(item, list):  # 消息数组形态
            text = None
            image = None
            for sub in item:
                t, im, _ = _split_input(sub)
                if t is not None:
                    text = (text + " " + t) if text else t
                if im is not None:
                    image = im
            vec = we.embed(model, processor, text=text, image=image,
                           instruction=we.QUERY_INSTRUCTION, dim=dim)
        else:
            text, image, video = _split_input(item)
            vec = we.embed(model, processor, text=text, image=image, video=video,
                           instruction=we.QUERY_INSTRUCTION, dim=dim)
        mx.eval(vec)
        data.append(vec.tolist())

    fmt = body.get("encoding_format", "float")
    out_data = []
    for i, v in enumerate(data):
        if fmt == "base64":
            import struct
            packed = struct.pack(f"<{len(v)}f", *v)
            out_data.append({"object": "embedding", "index": i,
                             "embedding": base64.b64encode(packed).decode()})
        else:
            out_data.append({"object": "embedding", "index": i, "embedding": v})

    return {
        "object": "list",
        "data": out_data,
        "model": body.get("model", SERVICE_NAME),
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def _handle_rerank(body: dict) -> dict:
    """Jina/Cohere 风格重排：query vs documents 余弦相似度排序。

    用检索模型的标准姿势：query 加 QUERY 指令，documents 加 DOC 指令，
    两者在同一向量空间比余弦，效果显著优于裸文本。
    """
    import mlx.core as mx

    model, processor = _STATE["model"], _STATE["processor"]
    query = body.get("query")
    docs = body.get("documents")
    if query is None or not docs:
        raise ValueError("'query' and non-empty 'documents' required")
    top_n = body.get("top_n")
    return_documents = body.get("return_documents", False)
    dim = body.get("dimensions")

    def one(item, instr):
        if isinstance(item, str):
            vec = we.embed(model, processor, text=item, instruction=instr, dim=dim)
        elif isinstance(item, dict):
            t = item.get("text") or item.get("content") or ""
            img = None
            if item.get("image") or item.get("image_url"):
                u = item.get("image") or item.get("image_url")
                url = u.get("url") if isinstance(u, dict) else u
                img = _decode_image_url(url)
            vec = we.embed(model, processor, text=t, image=img, instruction=instr, dim=dim)
        else:
            raise ValueError(f"unsupported item: {item!r}")
        mx.eval(vec)
        return vec

    vq = one(query, we.QUERY_INSTRUCTION)
    scored = []
    for i, d in enumerate(docs):
        vd = one(d, we.DOC_INSTRUCTION)
        score = float(mx.sum(vq * vd))  # 双方已 L2 归一，点积即余弦
        item = {"index": i, "relevance_score": score}
        if return_documents:
            item["document"] = {"text": d} if isinstance(d, str) else d
        scored.append(item)

    scored.sort(key=lambda x: x["relevance_score"], reverse=True)
    if top_n:
        scored = scored[:top_n]
    return {
        "model": body.get("model", SERVICE_NAME),
        "usage": {"total_tokens": 0},
        "results": scored,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] " + (fmt % args) + "\n")

    def _send(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"status": "ok", "model": SERVICE_NAME,
                             "model_path": _STATE["model_path"],
                             "backend": "mlx", "device": "apple-silicon"})
        elif self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [
                {"id": SERVICE_NAME, "object": "model", "owned_by": "local"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            t0 = time.time()
        except Exception as e:
            self._send(400, {"error": {"message": str(e)}})
            return

        if self.path.startswith("/v1/rerank"):
            try:
                res = _handle_rerank(body)
                res["usage"]["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
                self._send(200, res)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send(400, {"error": {"message": str(e), "type": type(e).__name__}})
            return

        if not self.path.startswith("/v1/embeddings"):
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            res = _handle_embeddings(body)
            res["usage"]["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            self._send(200, res)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(400, {"error": {"message": str(e), "type": type(e).__name__}})


def run_server(model_path, host="127.0.0.1", port=8231):
    """可编程入口（供 embed.py --serve 复用）。"""
    print(f"加载模型: {model_path}")
    model, processor = we.load_model(model_path)
    _STATE.update({"model": model, "processor": processor, "model_path": model_path})
    print(f"模型加载完成 ✅  服务启动: http://{host}:{port}")
    print(f"  POST /v1/embeddings   POST /v1/rerank   GET /health   GET /v1/models")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="WeMM-Embedding 本地 embedding 服务")
    ap.add_argument("--model", default=we.DEFAULT_MODEL)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8231)
    args = ap.parse_args()
    run_server(args.model, args.host, args.port)


if __name__ == "__main__":
    main()
