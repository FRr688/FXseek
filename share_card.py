#!/usr/bin/env python3
"""分享海报渲染 + 分享文案 —— 电脑端「分享给朋友」和命令行出图共用这一份。

★ 为什么放进程序目录（而不是留在仓库根目录）：
  电脑端设置里那个「保存分享海报」按钮要**在 App 里**现画一张，
  而 App 只跑 `Contents/Resources/app/` 里的白名单文件（见 tools/build_app.sh）。
  留两份实现迟早会飘（改了一份忘了另一份，海报上的版本号/体积就对不上），
  所以真身放这里，根目录的 `make_share_card.py` 只是命令行入口。

为什么要出图：QQ 空间会屏蔽正文里的外链，但**图片不会被文字审核**，
朋友扫码也比复制粘贴长链接靠谱。

为什么主码用镜像：国内直连 github.com 下载 350 MB 的 dmg 基本龟速甚至断流
（范老板 2026-10-13 明确要求「国内镜像源为主，也附加上 github 原地址」）。

踩坑记录（改版式前先读）：
- ⚠️ emoji 在 Hiragino Sans GB 里没有字形，会渲染成豆腐块 —— 所有图标都得自己画线稿。
- ⚠️ 版式高度要留够，否则底部那行会跟警告框叠在一起（第一版就叠了）。
- ⚠️ 副二维码 118px 时手机扫不动，实测要 ≥150px。
- ⚠️ 画圆角图标不能只调 `rounded_rectangle(outline=None)`（什么都不做），
  要用 `Image.new("L", ...)` 做圆角蒙版再 `paste`。
- ⚠️ 版本号唯一来源是 app.py 里那行 "version"（跟 tools/release.sh 看齐），
  别写死，否则发新包时海报上的二维码和体积全是旧的。
"""
import os
import re
import time

import qrcode
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M
from PIL import Image, ImageDraw, ImageFont

REPO = "FRr688/FXseek"

# ---------- 镜像：2026-10-13 实测（HEAD Range 请求）----------
#   ghproxy.net        206  1.3s   ← 最快，用作主码
#   hk.gh-proxy.com    206  2.2s
#   ghfast.top         206  2.8s   （旧海报用的就是它）
#   gh-proxy.com / ghproxy.cc / github.moeyy.xyz / gitproxy.click  当时不通（000/401）
# 这几个会同时印在海报的「备用」那一行上，改这里海报跟着变。
MIRROR = "https://ghproxy.net/"
MIRROR_ALTS = ("ghfast.top", "hk.gh-proxy.com")

W, H = 1080, 2088   # ★ 2026-10-18：底部加两行镜像直链，2000 → 2088
BG_TOP, BG_BOT = (250, 250, 252), (243, 242, 250)
INK = (22, 24, 32)
SUB = (108, 114, 132)
FAINT = (152, 158, 176)
BRAND1, BRAND2, BRAND3 = (99, 102, 241), (168, 85, 247), (236, 72, 153)
CARD_BD = (231, 233, 242)

F = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def _app_version() -> str:
    """从 app.py 里读版本号（跟 tools/release.sh 同一个来源）。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        src = open(os.path.join(here, "app.py"), encoding="utf-8").read()
        return re.search(r'"version"\s*:\s*"([^"]+)"', src).group(1)
    except Exception:
        return "1.1.0"


def _dmg_size_mb(version: str) -> int:
    """从真实 dmg 算体积；取不到就回落 351（1.1.0 实测值）。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(here, "dist", "FXseek-%s.dmg" % version)
        return round(os.path.getsize(p) / 1048576)
    except OSError:
        return 351


def urls(version: str, mirror: str = MIRROR) -> dict:
    """海报上的三条地址。

    ★ 2026-10-18 改过口径：主码 **不再钉死版本**，改成「镜像 + releases/latest」。
      钉死的好处是扫码即下、不用过网页；坏处是每发一版海报就作废（1.1.1 一出来，
      旧海报的码就指向一个 404）。现在主码永远跟着最新 Release 走，版本号写在下面的
      小字里；页面上的下载按钮如果因为没梯子点不动，就复制纸上印的那条资产直链。
    """
    direct = "https://github.com/%s/releases/download/v%s/FXseek-%s.dmg" % (REPO, version, version)
    latest = "https://github.com/%s/releases/latest" % REPO
    return {"direct": direct,
            "mirror": mirror + latest,
            "mirror_direct": mirror + direct,
            "github": latest}


def share_text(version: str | None = None, mirror: str = MIRROR) -> str:
    """「复制分享文案」用的那段话。

    ★ 刻意**不带裸露外链**：微信/QQ 空间对正文里的 http(s) 链接会屏蔽或降权，
      真正能直达的是随文案一起发出去的那张海报（图片不过文字审核）。
      所以正文只留一句引导 + 版本号，链接塞在二维码里。
    """
    v = version or _app_version()
    return (
        "分享一个我自己在用的本地素材库：FXseek\n"
        "\n"
        "图片、视频、音频、文档都能用大白话搜 —— 不用记文件名，\n"
        "说「气球」「狗在跑」就能找出来；还能哼一段找那首歌、丢一张图找相似的。\n"
        "\n"
        "全程本地跑，不上传、不注册，非商业用途免费。\n"
        "macOS 13+ / Apple 芯片，约 %d MB（v%s）。\n"
        "\n"
        "👉 扫下面那张海报上的二维码就能下（国内镜像，秒开）。"
    ) % (_dmg_size_mb(v), v)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _font(size, bold=False):
    try:
        return ImageFont.truetype(F, size, index=1 if bold else 0)
    except Exception:
        return ImageFont.truetype(F, size)


def render(out_path=None, version=None, size_mb=None, mirror=MIRROR) -> dict:
    """画一张海报存到 out_path（默认 ~/Desktop/FXseek-分享图.png）。

    返回 {path, version, size_mb, url_mirror, url_github, text}。
    在 App 里跑是给「保存分享海报」用的；命令行跑就是出图发朋友圈。
    """
    VER = version or _app_version()
    SIZE_MB = size_mb if size_mb is not None else _dmg_size_mb(VER)
    U = urls(VER, mirror)

    if out_path is None:
        out_path = os.path.join(os.path.expanduser("~"), "Desktop", "FXseek-分享图.png")
    out_path = os.path.abspath(os.path.expanduser(out_path))

    def font(size, bold=False):
        return _font(size, bold)

    def tw(d, text, f):
        b = d.textbbox((0, 0), text, font=f)
        return b[2] - b[0]

    def center(d, y, text, f, fill):
        w = tw(d, text, f)
        d.text(((W - w) // 2, y), text, font=f, fill=fill)
        return w

    def brand_at(t):
        if t < 0.55:
            u, a, b = t / 0.55, BRAND1, BRAND2
        else:
            u, a, b = (t - 0.55) / 0.45, BRAND2, BRAND3
        return tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3))

    def shadow_card(img, d, box, radius=32, fill=(255, 255, 255), layers=3):
        x0, y0, x1, y1 = box
        for i in range(layers, 0, -1):
            a = 10 - i * 2
            ImageDraw.Draw(img).rounded_rectangle(
                [x0 - i, y0 + i // 2 + 2, x1 + i, y1 + i // 2 + 4], radius=radius + i,
                fill=(BG_BOT[0] - a, BG_BOT[1] - a, BG_BOT[2] - a))
        d.rounded_rectangle(box, radius=radius, fill=fill, outline=CARD_BD, width=2)

    def qr_img(data, px, ecc=ERROR_CORRECT_H, border=2):
        q = qrcode.QRCode(error_correction=ecc, box_size=16, border=border)
        q.add_data(data)
        q.make(fit=True)
        return q.make_image(fill_color=(18, 20, 28), back_color="white").convert("RGB") \
            .resize((px, px), Image.NEAREST)

    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(
            int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    for x in range(W):
        d.line([(x, 0), (x, 11)], fill=brand_at(x / W))

    M = 70

    # ---------- 品牌区 ----------
    lx, ly, ls = M, 74, 74
    icon_grad = Image.new("RGB", (ls, ls))
    gd = ImageDraw.Draw(icon_grad)
    for i in range(ls):
        gd.line([(i, 0), (i, ls)], fill=brand_at(i / ls))
    mask = Image.new("L", (ls, ls), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, ls - 1, ls - 1], radius=19, fill=255)
    img.paste(icon_grad, (lx, ly), mask)
    cx_, cy_, r_ = lx + ls // 2 - 4, ly + ls // 2 - 4, 18
    d.ellipse([cx_ - r_, cy_ - r_, cx_ + r_, cy_ + r_], outline=(255, 255, 255), width=7)
    d.line([(cx_ + r_ - 3, cy_ + r_ - 3), (cx_ + r_ + 13, cy_ + r_ + 13)],
           fill=(255, 255, 255), width=8)
    d.text((lx + ls + 22, ly + 2), "FXseek", font=font(58, True), fill=INK)
    d.text((lx + ls + 26, ly + 68), "本地多模态素材库", font=font(27), fill=FAINT)

    # ---------- 主标题 ----------
    center(d, 208, "用大白话，搜你的素材", font(62, True), INK)
    center(d, 292, "图片 · 视频 · 音频 · 文档 · 30+ 格式", font(33), SUB)
    uw, ux = 168, (W - 168) // 2
    for i in range(uw):
        d.line([(ux + i, 358), (ux + i, 366)], fill=brand_at(i / uw))

    # ---------- 四个亮点 ----------
    FEATS = [("搜画面", "说出画面就能找到它", "search"),
             ("听声音", "哼一段，找出那段音频", "wave"),
             ("以图搜图", "丢一张图，找相似素材", "image"),
             ("手机直连", "手机相册直连电脑库", "phone")]
    CW = (W - 2 * M - 26) // 2
    CH = 148
    FY0, FGAP_Y = 408, 20

    def icon(kind, x, y, s=46):
        c = BRAND2
        if kind == "search":
            r = s * 0.36
            d.ellipse([x, y, x + 2 * r, y + 2 * r], outline=c, width=5)
            d.line([(x + 2 * r - 4, y + 2 * r - 4), (x + s, y + s)], fill=c, width=6)
        elif kind == "wave":
            for i, hh in enumerate([0.30, 0.62, 1.0, 0.66, 0.36]):
                bx = x + i * (s / 5.0) + s / 10.0
                d.line([(bx, y + s / 2 - s * hh / 2), (bx, y + s / 2 + s * hh / 2)],
                       fill=c, width=6)
        elif kind == "image":
            d.rounded_rectangle([x, y + s * 0.10, x + s, y + s * 0.90],
                                radius=9, outline=c, width=5)
            d.ellipse([x + s * 0.16, y + s * 0.24, x + s * 0.38, y + s * 0.46], fill=c)
            d.line([(x + s * 0.10, y + s * 0.82), (x + s * 0.42, y + s * 0.48),
                    (x + s * 0.66, y + s * 0.74), (x + s * 0.82, y + s * 0.60),
                    (x + s * 0.92, y + s * 0.72)], fill=c, width=4, joint="curve")
        elif kind == "phone":
            d.rounded_rectangle([x + s * 0.14, y, x + s * 0.86, y + s],
                                radius=10, outline=c, width=5)
            d.line([(x + s * 0.38, y + s * 0.16), (x + s * 0.62, y + s * 0.16)],
                   fill=c, width=4)
            d.ellipse([x + s * 0.42, y + s * 0.82, x + s * 0.58, y + s * 0.91], fill=c)

    for i, (title, desc, kind) in enumerate(FEATS):
        col, row = i % 2, i // 2
        x = M + col * (CW + 26)
        y = FY0 + row * (CH + FGAP_Y)
        shadow_card(img, d, [x, y, x + CW, y + CH], radius=24)
        icon(kind, x + 32, y + CH // 2 - 23)
        d.text((x + 104, y + 38), title, font=font(31, True), fill=INK)
        f_desc = font(21)
        line, lines = "", []
        for ch in desc:
            if tw(d, line + ch, f_desc) > CW - 130:
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
        for j, ln in enumerate(lines):
            d.text((x + 104, y + 80 + j * 30), ln, font=f_desc, fill=SUB)

    # ---------- 主二维码（国内镜像）----------
    QC, QY = 560, 780
    qx = (W - QC) // 2
    shadow_card(img, d, [qx, QY, qx + QC, QY + QC], radius=36)
    PAD = 30
    img.paste(qr_img(U["mirror"], QC - PAD * 2, ERROR_CORRECT_H), (qx + PAD, QY + PAD))
    for ax, ay in [(qx, QY), (qx + QC, QY), (qx, QY + QC), (qx + QC, QY + QC)]:
        d.rounded_rectangle([ax - 6, ay - 6, ax + 6, ay + 6], radius=3, fill=BRAND2)
    bt, fb = "国内镜像 · 秒下", font(25, True)
    bw = tw(d, bt, fb) + 44
    bx = (W - bw) // 2
    d.rounded_rectangle([bx, QY - 22, bx + bw, QY + 22], radius=22, fill=BRAND2)
    d.text((bx + 22, QY - 15), bt, font=fb, fill=(255, 255, 255))

    yl = QY + QC + 34
    center(d, yl, "微信 / QQ 扫码，直接下载", font(38, True), INK)
    center(d, yl + 62,
           "macOS 13+ · Apple 芯片（M 系列）· 约 %d MB · v%s" % (SIZE_MB, VER),
           font(28), SUB)

    # ---------- 副二维码（GitHub 原地址）----------
    gy, GH = yl + 132, 150
    shadow_card(img, d, [M, gy, M + GH + 28, gy + GH + 28], radius=20, layers=2)
    img.paste(qr_img(U["github"], GH, ERROR_CORRECT_M, border=1), (M + 14, gy + 14))
    gx = M + GH + 28 + 30
    d.text((gx, gy + 22), "GitHub 原地址（备用 / 看更新）", font=font(30, True), fill=INK)
    d.text((gx, gy + 70), "github.com/%s" % REPO, font=font(25), fill=SUB)
    d.text((gx, gy + 108), "镜像不通时手动换：", font=font(20), fill=FAINT)
    d.text((gx, gy + 134), " · ".join(MIRROR_ALTS), font=font(20), fill=FAINT)

    # ---------- 首次打开提醒 ----------
    by, bh = gy + GH + 28 + 40, 124
    shadow_card(img, d, [M, by, W - M, by + bh], radius=22, fill=(255, 248, 238), layers=2)
    d.rounded_rectangle([M, by, W - M, by + bh], radius=22, fill=(255, 248, 238),
                        outline=(248, 212, 162), width=2)
    tx, ty, ts = M + 44, by + 40, 32
    d.line([(tx + ts // 2, ty), (tx, ty + ts)], fill=(212, 128, 28), width=5)
    d.line([(tx, ty + ts), (tx + ts, ty + ts)], fill=(212, 128, 28), width=5)
    d.line([(tx + ts // 2, ty), (tx + ts, ty + ts)], fill=(212, 128, 28), width=5)
    d.line([(tx + ts // 2, ty + 10), (tx + ts // 2, ty + 20)], fill=(212, 128, 28), width=5)
    d.ellipse([tx + ts // 2 - 3, ty + 23, tx + ts // 2 + 3, ty + 29], fill=(212, 128, 28))
    d.text((M + 100, by + 26), "第一次打开请「右键 → 打开」", font=font(32, True),
           fill=(178, 98, 18))
    d.text((M + 100, by + 72), "应用未做 Apple 公证，双击会被拦一次，属正常",
           font=font(24), fill=(150, 110, 70))

    # ---------- 镜像直链（网页上的下载按钮指回 github.com，国内没梯子会卡）----------
    ly = by + bh + 26
    center(d, ly, "按钮打不开？把下面这行复制到浏览器（走镜像，直接下）", font(22), SUB)
    center(d, ly + 38, "ghproxy.net/https://github.com/%s/releases/download/v%s/FXseek-%s.dmg"
           % (REPO, VER, VER), font(19), (75, 85, 99))

    # ---------- 底部 ----------
    center(d, H - 76, "全程本地运行 · 不上传 · 不注册 · 非商业用途免费",
           font(27, True), BRAND2)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # ⚠️ 关键：用临时文件 + 原子替换。用户点「保存海报」时如果目标文件正被预览打开，
    #    直接 save(同名) 会写坏或报 PermissionError；os.replace 是原子的。
    tmp = out_path + ".tmp-%d" % int(time.time() * 1000)
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, out_path)
    return {"path": out_path, "version": VER, "size_mb": SIZE_MB,
            "url_mirror": U["mirror"], "url_github": U["github"],
            "text": share_text(VER, mirror)}


if __name__ == "__main__":
    import json
    import sys
    r = render(sys.argv[1] if len(sys.argv) > 1 else None)
    print("已生成:", r["path"], "v%s · %d MB" % (r["version"], r["size_mb"]))
    print("主码（镜像）:", r["url_mirror"])
    print("副码（GitHub）:", r["url_github"])
    print(json.dumps(r, ensure_ascii=False))
