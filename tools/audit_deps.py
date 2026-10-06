#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""依赖体检：确认「运行时真正 import 的东西」一个都不缺。

为什么需要它：这个 venv 是**手工瘦身过**的（删过一堆用不上的包）。
瘦身很容易误伤 —— 真实教训：某次清理把 pdfminer 的代码删了、只留下
`pdfminer_six-*.dist-info` 元数据，于是 pdfplumber import 失败、PDF 文本提取
静默降级到 textutil，而且 `pip install` 还因为元数据在而拒绝重装，很久没人发现。

用法：
    ./venv/cpython-3.11/bin/python3.11 tools/audit_deps.py

检查两件事：
  1) 业务模块 + 关键三方包能不能 import；
  2) site-packages 里有没有「元数据在、代码没了」的空壳（这种最容易骗过人）。
"""
import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

BUSINESS = ["paths", "indexer", "embed", "ai_desc", "asr_desc", "fingerprint",
            "fastsearch", "voice_intent", "recorder", "model_dl", "seed",
            "serve", "mcp_server", "mcp_agents", "tray_helper"]

THIRD = ["webview", "AppKit", "Foundation", "AVFoundation", "PyObjCTools",
         "requests", "certifi", "numpy", "PIL", "mlx", "mlx.core", "mlx_vlm",
         "transformers", "tokenizers", "safetensors", "huggingface_hub",
         "openpyxl", "xlrd", "olefile", "pptx", "pdfplumber", "pdfminer",
         "jinja2", "cryptography", "bs4", "lxml", "yaml"]


def import_check(mods, label):
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            bad.append("%s → %s: %s" % (m, type(e).__name__, e))
    print("  %s：%d/%d 可用" % (label, len(mods) - len(bad), len(mods)))
    for b in bad:
        print("    ✗ %s" % b)
    return bad


def hollow_metadata():
    """site-packages 里「RECORD 记着，但顶层包/模块已经不存在」的 dist-info。"""
    sp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))),
                      "lib", "python3.11", "site-packages")
    if not os.path.isdir(sp):
        return []
    out = []
    for d in sorted(os.listdir(sp)):
        if not d.endswith(".dist-info"):
            continue
        rec = os.path.join(sp, d, "RECORD")
        tops, present = set(), set()
        if os.path.exists(rec):
            for line in open(rec, encoding="utf-8", errors="ignore"):
                p = line.split(",")[0].strip()
                if not p:
                    continue
                top = p.split("/")[0]
                if top.endswith(".py"):
                    top = top[:-3]
                if top and not top.startswith("..") and top != "__pycache__" and "dist-info" not in top:
                    tops.add(top)
        for t in tops:
            if os.path.exists(os.path.join(sp, t)) or os.path.exists(os.path.join(sp, t + ".py")):
                present.add(t)
        if tops and not present:
            out.append(d)
    return out


def main():
    print("== 依赖体检 ==")
    bad = import_check(BUSINESS, "业务模块")
    bad += import_check(THIRD, "三方包")
    hb = hollow_metadata()
    print("  空壳元数据：%d 个%s" % (len(hb), ("（%s …）" % ", ".join(hb[:5])) if hb else ""))
    if bad or hb:
        print("\n⚠️ 有问题：先把缺的装回来再瘦身 ——")
        print("   ./venv/cpython-3.11/bin/python3.11 -m pip install --force-reinstall --no-deps <包名>")
        print("   （元数据还在时 pip 会说 already satisfied，所以必须加 --force-reinstall --no-deps）")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
