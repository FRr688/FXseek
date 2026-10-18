#!/usr/bin/env python3
"""把 AI 生成的狒狒行走雪碧图切成「抠好背景的一条横帧带」。

输入：一张 4 列 × 2 行、8 帧、纯绿底的四足行走雪碧图。
输出：assets/rope_baboon.png —— 8 帧横向排列、带 alpha 的 PNG，
      rope.html 里用 background-position 逐帧播放（绳上那只狒狒）。

为什么不在页面上用 CSS 抠绿：canvas 逐帧抠会拖 CPU，而且窗口是透明的，
留一点绿边在壁纸上看得很明显。一次离线抠干净，运行时零成本。

用法：
    python3 tools/make_rope_baboon.py <sheet.png> [--cols 4] [--rows 2]
                                      [--height 96] [--out assets/rope_baboon.png]
"""
import argparse
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def key_green(im, hard=26, soft=6):
    """绿幕转 alpha。

    背景不是纯 #00FF00，而是一层**有渐变的绿**（中间亮、四角暗），
    所以不能按颜色相等去抠。这里用「绿通道比红蓝都高出多少」当判据：
    绿色背景 g-max(r,b) 常年 > 40，而狒狒身上的橄榄棕毛 g 基本 ≤ r。
    hard 以上的彻底透明，hard-soft 到 hard 之间给一个渐变的 alpha，
    这样轮廓边上不会留一圈硬绿。
    """
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    out = Image.new("RGBA", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            d = g - (r if r > b else b)
            if d >= hard:
                op[x, y] = (0, 0, 0, 0)
            elif d <= hard - soft:
                op[x, y] = (r, g, b, 255)
            else:
                a = int(255 * (hard - d) / soft)
                # 边缘像素带绿边，往回收一点：把 g 压到 max(r,b) 附近
                m = r if r > b else b
                gg = int(g * 0.35 + m * 0.65)
                op[x, y] = (r, gg, b, a)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=2)
    ap.add_argument("--height", type=int, default=96)
    ap.add_argument("--out", default=os.path.join(ROOT, "assets", "rope_baboon.png"))
    a = ap.parse_args()

    sheet = Image.open(a.sheet)
    W, H = sheet.size
    cw, ch = W // a.cols, H // a.rows

    frames = []
    for i in range(a.cols * a.rows):
        r, c = divmod(i, a.cols)
        cell = sheet.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
        frames.append(key_green(cell))

    # 取所有帧非透明像素的**并集**外框，让每帧裁到同一位置，
    # 否则每帧各自 trim 会让狒狒在绳上一跳一跳地抖。
    box = None
    for f in frames:
        bb = f.getbbox()
        if bb is None:
            continue
        box = bb if box is None else (min(box[0], bb[0]), min(box[1], bb[1]),
                                      max(box[2], bb[2]), max(box[3], bb[3]))
    if box is None:
        print("整张图全是背景色，抠失败了", file=sys.stderr)
        return 1
    print("并集外框:", box, "帧格:", (cw, ch))

    fw, fh = box[2] - box[0], box[3] - box[1]
    scale = a.height / float(fh)
    tw, th = max(1, int(round(fw * scale))), a.height

    strip = Image.new("RGBA", (tw * len(frames), th), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        strip.paste(f.crop(box).resize((tw, th), Image.LANCZOS), (i * tw, 0))

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    strip.save(a.out)
    print("写出 %s  %d 帧 × %dx%d  (共 %dx%d)"
          % (a.out, len(frames), tw, th, strip.size[0], strip.size[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
