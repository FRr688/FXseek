#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 FR. All rights reserved.
# 本文件是 FXseek 的一部分：非商业用途免费，商业用途需取得授权（见仓库根目录 COMMERCIAL.md）。

"""HEIC / HEIF 图像支持（Apple 设备默认拍照格式）。

为什么单独一层
--------------
`.heic` 一直写在图片扩展名白名单里（`indexer.IMAGE_EXT`），但 **Pillow 自己不含
HEIF 解码器** —— 需要 `pillow-heif` 这个扩展。没装时 `Image.open()` 会抛
`UnidentifiedImageError`，而索引入口 `iter_units()` 是「打开失败就跳过」的写法，
于是 iPhone 拍的照片会**静默不进库**：搜不到、没缩略图、漫步时光里也看不到。

注册是**进程级**的：任何一个会 `Image.open()` 的入口 import 本模块一次即可。
没装扩展时静默降级（行为与从前完全一致），不影响任何其它格式。
"""

HEIF_OK = False
HEIF_ERR = ""

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception as _e:          # 未安装 / 版本不兼容 / 平台无 wheel
    HEIF_ERR = str(_e)
