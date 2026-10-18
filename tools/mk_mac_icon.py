#!/usr/bin/env python3
"""重建 macOS 版 AppIcon.icns 的底色（跟手机版同一套做法）。

用法：
    python3 tools/mk_mac_icon.py "#191221"            # 就地重建 assets/AppIcon.icns
    python3 tools/mk_mac_icon.py "#191221" /tmp/x.icns

做法：
- 先把 icns 拆成 iconset（iconutil），**每一个尺寸都就地改**，不重新缩放 ——
  这样圆角边缘的抗锯齿跟原来一模一样。
- 只在「与外界连通、且颜色还是原来的 #0B0815」的那片区域上换色；
  猴脸五官用的是同一个色值，但被画稿包住、不与外界连通，所以不会被误伤。
- alpha 通道原样保留（macOS 图标靠它做圆角 + 投影），RGB 是直通 alpha、没有预乘，
  所以直接改 RGB 即可，半透明的边缘像素会自动跟着走。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from collections import deque

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICNS = os.path.join(HERE, "assets", "AppIcon.icns")
OLD = np.array([11, 8, 21])          # 原本的底色 #0B0815
TOL = 3


def recolor(arr, new):
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3]
    H, W = alpha.shape
    near = np.abs(rgb - OLD).max(axis=2) <= TOL

    seen = np.zeros((H, W), bool)
    dq = deque()
    border = [(x, 0) for x in range(W)] + [(x, H - 1) for x in range(W)] + \
             [(0, y) for y in range(H)] + [(W - 1, y) for y in range(H)]
    for x, y in border:
        if near[y, x] and not seen[y, x]:
            seen[y, x] = True; dq.append((x, y))
    while dq:
        x, y = dq.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < W and 0 <= ny < H and near[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True; dq.append((nx, ny))

    mask = seen | ((alpha < 255) & near)      # 连通平底 + 那圈半透明边缘
    out = arr.copy()
    out[:, :, :3][mask] = new
    return out, int(mask.sum())


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    new = np.array([int(sys.argv[1].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
    out_icns = sys.argv[2] if len(sys.argv) > 2 else ICNS

    if not os.path.exists(ICNS):
        sys.exit("找不到 %s" % ICNS)

    with tempfile.TemporaryDirectory() as td:
        iconset = os.path.join(td, "App.iconset")
        subprocess.run(["iconutil", "-c", "iconset", ICNS, "-o", iconset], check=True)
        total = 0
        for name in sorted(os.listdir(iconset)):
            if not name.endswith(".png"):
                continue
            p = os.path.join(iconset, name)
            im = Image.open(p).convert("RGBA")
            arr, n = recolor(np.asarray(im), new)
            total += n
            Image.fromarray(arr, "RGBA").save(p)
        tmp_icns = os.path.join(td, "AppIcon.icns")
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", tmp_icns], check=True)
        shutil.copy2(tmp_icns, out_icns)
    print("%s 底色 → #%02X%02X%02X（各尺寸共换 %d 像素，alpha 原样保留）"
          % (out_icns, *new, total))


if __name__ == "__main__":
    main()
