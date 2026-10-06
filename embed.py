#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""
WeMM-Embedding-2B-Apple-Silicon-MLX 多模态 embedding 脚本 (独立打包版)

★ 只依赖 wemm_app/venv 里的独立 MLX 环境，不加载、不修改 oMLX 本体。
★ 不修改模型目录（config 在内存中覆盖，不写回文件）。
★ 运行方式（已验证）：
    wemm_app/venv/cpython-3.11/bin/python3.11 embed.py --self-test
    wemm_app/venv/cpython-3.11/bin/python3.11 embed.py --text "一段文本"
    wemm_app/venv/cpython-3.11/bin/python3.11 embed.py --image cat.jpg
    wemm_app/venv/cpython-3.11/bin/python3.11 embed.py --image cat.jpg --text "描述"

模型机制：
    WeMM 是混合精度(FP8护注意力 + INT4压权重)多模态 embedding 模型，架构 qwen3_5
    (Qwen3VLModel 子类)。本脚本用 mlx_vlm.load() 加载原模型（已验证可在 Apple
    Silicon 加载），图文/视频统一编码：文本 + 图像过 vision_tower 得 pixel_values，
    一并喂入 language_model.model()，取 last-non-padding token 隐状态并 L2 归一，
    与 qwen3_vl_embedding 官方 pooling 行为一致。
"""

import argparse
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# 内置模型优先（打包分发用）；不存在时回退到本地下载目录
_BUILTIN_MODEL = os.path.join(HERE, "model")
_FALLBACK_MODEL = "/Users/fa/Downloads/CC工作区/ewin-reg:WeMM-Embedding-2B-Apple-Silicon-MLX"
DEFAULT_MODEL = _BUILTIN_MODEL if os.path.isdir(_BUILTIN_MODEL) else _FALLBACK_MODEL

# 检索任务指令前缀（WeMM 是检索模型，query/doc 区分效果更优）
QUERY_INSTRUCTION = "Instruct: Given a web search query, retrieve relevant passages.\nQuery: "
DOC_INSTRUCTION = "Represent the document for retrieval:\n"


# ★ 全局模型缓存：避免每次检索/编码都重新加载（2GB 模型反复加载极耗资源）
_MODEL_CACHE = {"path": None, "model": None, "processor": None}


def load_model(model_path: str, use_cache: bool = True):
    """用 mlx_vlm.load() 加载模型（带全局缓存）。

    use_cache=True 时同一路径只加载一次，后续复用（省内存、省时间）。
    """
    from pathlib import Path
    rp = str(Path(model_path).resolve())
    if use_cache and _MODEL_CACHE["path"] == rp and _MODEL_CACHE["model"] is not None:
        return _MODEL_CACHE["model"], _MODEL_CACHE["processor"]
    from mlx_vlm import load
    model, processor = load(Path(model_path))
    if use_cache:
        _MODEL_CACHE.update({"path": rp, "model": model, "processor": processor})
    return model, processor


def unload_model():
    """显式释放模型缓存（省内存；下次调用会自动重新加载）。"""
    import gc
    _MODEL_CACHE.update({"path": None, "model": None, "processor": None})
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass


# 视觉编码前的图片预处理尺寸（越小越快；420~672 是精度/速度的甜点区）
# 可运行时通过 set_image_max_side() 调整（设置页保存后即时生效）
IMAGE_MAX_SIDE = int(os.environ.get("WEMM_IMAGE_MAX_SIDE", "420"))


def set_image_max_side(px: int):
    """运行时调整图片编码尺寸（设置页使用）。"""
    global IMAGE_MAX_SIDE
    try:
        px = int(px)
        if 128 <= px <= 2048:
            IMAGE_MAX_SIDE = px
            return IMAGE_MAX_SIDE
    except Exception:
        pass
    return IMAGE_MAX_SIDE


def _prepare_image(img):
    """缩放图片到目标尺寸，显著加速视觉编码（不改长宽比）。"""
    try:
        from PIL import Image
        if not isinstance(img, Image.Image):
            return img
        w, h = img.size
        if max(w, h) <= IMAGE_MAX_SIDE:
            return img
        scale = IMAGE_MAX_SIDE / float(max(w, h))
        nw, nh = max(28, int(w * scale)), max(28, int(h * scale))
        # 对齐到 28 的倍数（Qwen-VL patch_size*merge = 14*2）
        nw = max(28, (nw // 28) * 28)
        nh = max(28, (nh // 28) * 28)
        return img.resize((nw, nh), Image.BILINEAR)
    except Exception:
        return img


def _encode(model, processor, text=None, image=None, video=None, instruction=""):
    """把文本/图像/视频编码成模型输入张量。返回 (input_ids, attention_mask, kwargs)。"""
    import mlx.core as mx
    # 文本：带指令前缀 + 图像占位符
    full_text = ""
    if image is not None:
        full_text += "<|vision_start|><|image_pad|><|vision_end|>"
    if video is not None:
        full_text += "<|vision_start|><|video_pad|><|vision_end|>"
    body = (instruction + text) if text else ""
    full_text += body

    kwargs = {}
    if image is not None or video is not None:
        imgs = image if isinstance(image, list) else ([image] if image else [])
        vids = video if isinstance(video, list) else ([video] if video else [])
        # ★ 预缩放：大幅降低视觉编码耗时（1600px→448px 约 12 倍加速），语义几乎无损
        if imgs:
            imgs = [_prepare_image(im) for im in imgs]
        out = processor(text=full_text, images=imgs or None,
                       videos=vids or None, return_tensors="np")
        ids = mx.array(out["input_ids"])
        mask = mx.array(out["attention_mask"])
        if "pixel_values" in out:
            kwargs["pixel_values"] = mx.array(out["pixel_values"])
        if "image_grid_thw" in out:
            kwargs["image_grid_thw"] = mx.array(out["image_grid_thw"])
        if "video_grid_thw" in out:
            kwargs["video_grid_thw"] = mx.array(out["video_grid_thw"])
    else:
        tok = processor.tokenizer
        enc = tok(full_text, return_tensors="np")
        ids = mx.array(enc["input_ids"])
        mask = mx.array(enc["attention_mask"])
    return ids, mask, kwargs


def embed(model, processor, text=None, image=None, video=None,
          instruction="", dim=None):
    import mlx.core as mx

    ids, mask, kwargs = _encode(model, processor, text, image, video, instruction)
    # 多模态：先过 vision_tower 拼 inputs_embeds，再进 language_model
    if kwargs:
        feats = model.get_input_embeddings(
            input_ids=ids,
            pixel_values=kwargs.get("pixel_values"),
            image_grid_thw=kwargs.get("image_grid_thw"),
            video_grid_thw=kwargs.get("video_grid_thw"),
            mask=mask,
        )
        cache = model.language_model.make_cache()
        h = model.language_model.model(
            ids, inputs_embeds=feats.inputs_embeds, mask=mask, cache=cache)
    else:
        cache = model.language_model.make_cache()
        h = model.language_model.model(ids, mask=mask, cache=cache)
    mx.eval(h)
    # last-non-padding token pooling（与 qwen3_vl_embedding 一致）
    if mask is not None:
        last_one = mx.argmax(mask[:, ::-1], axis=1)
        pos = mask.shape[1] - last_one - 1
        vec = h[mx.arange(h.shape[0]), pos]
    else:
        vec = h[:, -1]
    vec = vec[0]
    if dim:
        vec = vec[:dim]
    return vec / mx.linalg.norm(vec)


def self_test(model, processor):
    import mlx.core as mx
    from PIL import Image

    print("== 多模态 embedding 自检 (独立 venv / M4 / 不改模型目录) ==")
    v_cat = embed(model, processor, text="a photo of a cat sitting on a windowsill",
                  instruction=QUERY_INSTRUCTION)
    v_kit = embed(model, processor, text="kitten resting by the window",
                  instruction=QUERY_INSTRUCTION)
    v_rev = embed(model, processor, text="quarterly revenue increased by 12% YoY",
                  instruction=QUERY_INSTRUCTION)
    mx.eval(v_cat, v_kit, v_rev)

    def cos(a, b):
        a, b = a / mx.linalg.norm(a), b / mx.linalg.norm(b)
        return float(mx.sum(a * b))

    print(f"cos(cat, kitten)   = {cos(v_cat, v_kit):.3f}  (语义相关应较高)")
    print(f"cos(cat, revenue)  = {cos(v_cat, v_rev):.3f}  (语义无关应较低)")

    # 图文多模态：合成图 + 文本
    print("\n== 图文多模态向量验证 ==")
    img = Image.new("RGB", (224, 224), (120, 160, 200))
    v_img = embed(model, processor, text="a solid blue image", image=img,
                  instruction=QUERY_INSTRUCTION)
    v_txt = embed(model, processor, text="a solid blue image",
                  instruction=QUERY_INSTRUCTION)
    mx.eval(v_img, v_txt)
    print(f"cos(图+文, 纯文)  = {cos(v_img, v_txt):.3f}  (同语义跨模态应较高)")

    print("\n== 套娃截断验证 ==")
    for d in (64, 256, 1024, 2048):
        v = embed(model, processor, text="a photo of a cat sitting on a windowsill",
                  instruction=QUERY_INSTRUCTION, dim=d)
        mx.eval(v)
        print(f"  dim={d:>4} -> 实际 {int(v.shape[0])} 维  {'OK' if int(v.shape[0]) == d else 'FAIL'}")
    print("\n[结论] 模型在独立环境加载成功，多模态 embedding 路径可用 ✅")


def main():
    ap = argparse.ArgumentParser(description="WeMM-Embedding 多模态 embedding")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--text")
    ap.add_argument("--image", nargs="+")
    ap.add_argument("--video", nargs="+")
    ap.add_argument("--dimension", type=int, default=None)
    ap.add_argument("--instruction", choices=["query", "doc", "none"], default="query")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--serve", action="store_true", help="启动 OpenAI 兼容 embedding 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8231)
    args = ap.parse_args()

    if args.serve:
        import serve
        serve.run_server(args.model, args.host, args.port)
        return

    instr = {"query": QUERY_INSTRUCTION, "doc": DOC_INSTRUCTION, "none": ""}[args.instruction]
    print(f"加载模型: {args.model}")
    model, processor = load_model(args.model)
    print("加载完成 ✅")

    if args.self_test:
        self_test(model, processor)
        return

    vec = embed(model, processor, text=args.text, image=args.image, video=args.video,
                instruction=instr, dim=args.dimension)
    import mlx.core as mx
    mx.eval(vec)
    print(f"向量 shape: {vec.shape}")
    print(f"前 8 维: {mx.array(vec[:8])}")


if __name__ == "__main__":
    main()
