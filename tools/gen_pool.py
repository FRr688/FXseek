#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""批量签一批**永久**专业版激活码，灌进发货页的池子。

    # 首次：签 500 个，写 /tmp/fxpool.json
    ./venv/cpython-3.11/bin/python3 tools/gen_pool.py --n 500 --out /tmp/fxpool.json

    # 补货：把新签的 500 个**接在**现有池子后面
    ./venv/cpython-3.11/bin/python3 tools/gen_pool.py --n 500 --append --out /tmp/fxpool.json

    # 灌进 Cloudflare KV（在 发货页/ 目录下跑）
    wrangler kv key put --namespace-id <POOL的id> codes --path /tmp/fxpool.json

为什么要「先签好一池子」而不是让发货页现场签（见 发货页/README.md 第一节）：
    现场签要把私钥放上 Cloudflare，私钥一泄漏、离线码又吊不销，等于全完。
    而 19.9 买断的码本来就不分彼此，池子里的码和现场签出来的在买家眼里没有区别。

★ 私钥默认 `~/.fxseek-keys/private.key`，**这个文件永远不该离开本机**。
★ 每批都会往 `--log` 追加一行（默认 keys/issues.tsv），半年后有人来说
  「我的码不灵了」，你至少能查到那批是什么时候签的。

★ 这个脚本跟 make_license.py 用的是**同一套** `_pack` + Ed25519 签名，
  所以签出来的码是真 App 认的。别手搓。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # wemm_app/
sys.path.insert(0, HERE)                            # tools/
import license as lic  # noqa: E402
from make_license import DEFAULT_KEY, make  # noqa: E402

DEFAULT_LOG = os.path.join(os.path.dirname(HERE), "keys", "issues.tsv")


def main() -> int:
    ap = argparse.ArgumentParser(description="批量签发 FXseek 永久激活码")
    ap.add_argument("--n", type=int, default=100, help="签几个（默认 100）")
    ap.add_argument("--key", default=DEFAULT_KEY, help=f"私钥路径（默认 {DEFAULT_KEY}）")
    ap.add_argument("--out", default="/tmp/fxpool.json", help="输出的 JSON 路径")
    ap.add_argument("--append", action="store_true",
                    help="接在 --out 现有内容后面（补货用），而不是覆盖")
    ap.add_argument("--days", type=int, default=0, help="有效天数，0=永久（默认）")
    ap.add_argument("--txt", default="",
                    help="另存一份**一行一个**的纯文本（默认不存）。爱发电那类平台的"
                         "「自动发货」框只吃一行一个的裸码，不吃 JSON —— 有这份就直接"
                         "整段粘过去，不用手动从 JSON 里抠引号和逗号。")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help=f"签发记录 TSV（默认 {DEFAULT_LOG}，传空串则不记）")
    ap.add_argument("--dry-run", action="store_true", help="只报数，不真签不写文件")
    a = ap.parse_args()

    if not os.path.exists(a.key):
        raise SystemExit(
            f"找不到私钥：{a.key}\n"
            f"  第一次用先跑：{os.path.join(HERE, 'gen_keys.py')}")
    if a.n <= 0 or a.n > 10000:
        raise SystemExit("--n 得在 1..10000 之间")

    old = []
    if a.append and os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            try:
                old = json.load(f)
            except Exception as e:
                raise SystemExit(f"--out 里不是合法的 JSON（{e}），不敢往后面接：{a.out}")
        if not isinstance(old, list):
            raise SystemExit(f"--out 里不是个数组：{a.out}")

    if a.dry_run:
        print(f"[dry-run] 私钥 {a.key} 在；池子里现有 {len(old)} 个；将要再签 {a.n} 个")
        return 0

    codes, t0 = [], time.time()
    for i in range(a.n):
        codes.append(make(a.key, lic.TIER_PRO, a.days, ""))
        if (i + 1) % 100 == 0:
            print(f"  已签 {i + 1}/{a.n} …", flush=True)

    merged = old + codes

    def _ensure_parent(p):
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)

    tmp = a.out + ".tmp"
    _ensure_parent(tmp)                      # open() 只建文件不建目录
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    os.replace(tmp, a.out)

    # 纯文本副本：爱发电的「自动发货」是**一行一个**吃的，给它喂 JSON
    # 会把 {" 和 ", 一起发到买家手上。这份是给人工贴进后台用的。
    if a.txt:
        _ensure_parent(a.txt)
        with open(a.txt, "w", encoding="utf-8") as f:
            f.write("\n".join(codes) + "\n")
        print(f"  纯文本：{a.txt}（{len(codes)} 行，可直接整段粘进「自动发货」框）")

    if a.log:
        os.makedirs(os.path.dirname(a.log), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        new = not os.path.exists(a.log)
        with open(a.log, "a", encoding="utf-8") as f:
            if new:
                f.write("time\tbatch\tcount\ttier\tdays\tout\n")
            f.write(f"{stamp}\t{int(t0)}\t{a.n}\tpro\t{a.days or 'perpetual'}\t{a.out}\n")

    print(f"已签 {a.n} 个（池子 {len(old)} → {len(merged)}），用时 {time.time() - t0:.1f}s")
    print(f"  写到：{a.out}")
    print(f"  示例：{codes[0][:22]}…（别把整池子贴到任何聊天窗口里）")
    print()
    print("接下来（在 发货页/ 目录下）：")
    print(f"  wrangler kv key put --namespace-id <POOL的id> codes --path {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
