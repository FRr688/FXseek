#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""给一个客户签一张离线许可证。

    # 永久 Pro，随机序列号
    python3 tools/make_license.py

    # 一年期，序列号写 FA 的缩写，方便日后对账
    python3 tools/make_license.py --days 365 --serial FA

    # 看看手里这个码是什么（不验签，只是解包给人看）
    python3 tools/make_license.py --inspect FXK1-XXXXX-…

私钥默认从 `~/.fxseek-keys/private.key` 读，要换用 `--key`。

★ 签发记录请自己留一份（`--log` 会往 `keys/issues.tsv` 追加一行：时间/序列号/到期），
  不然半年后来个客户说「我的码不灵了」，你连他是不是买过都不知道。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license as lic  # noqa: E402

DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".fxseek-keys", "private.key")


def _pack(tier: str, days: int, serial_raw: bytes) -> bytes:
    if len(serial_raw) != 6:
        raise ValueError("序列号必须是 6 字节")
    issue_days = int((time.time() - lic._EPOCH) // lic._DAY)
    return (bytes([1, 1 if tier == lic.TIER_PRO else 0])
            + int(issue_days).to_bytes(2, "big")
            + int(days).to_bytes(2, "big")
            + serial_raw)


def make(key_path: str, tier: str, days: int, serial: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with open(key_path, "rb") as f:
        raw = f.read()
    if len(raw) != 32:
        raise SystemExit(f"私钥长度不对（{len(raw)} 字节，应为 32）：{key_path}")
    sk = Ed25519PrivateKey.from_private_bytes(raw)

    # 载荷里的 6 字节序列号一律是随机的 —— 它只是「区分不同客户」的把手，
    # 不承载任何信息。想对账请看 `--log` 里那一行（那才是你记的账）。
    # （★ 早先版本试过把 `--serial` 的字母数字编成字节，结果 base32 字母表里
    #   `0/1/8/9` 跟 `O/I/B/g` 撞车、长度还不是 6 的整数倍，白白多两个坑。）
    raw_serial = secrets.token_bytes(6)

    payload = _pack(tier, days, raw_serial)
    sig = sk.sign(payload)
    body = lic._b32_nopad(payload + sig)
    groups = [body[i:i + 5] for i in range(0, len(body), 5)]
    return f"{lic._CODE_PREFIX}-" + "-".join(groups)


def main() -> int:
    ap = argparse.ArgumentParser(description="签发 FXseek 离线许可证")
    ap.add_argument("--key", default=DEFAULT_KEY, help=f"私钥路径（默认 {DEFAULT_KEY}）")
    ap.add_argument("--days", type=int, default=0, help="有效天数，0=永久（默认）")
    ap.add_argument("--serial", default="", help="客户序列号（字母数字，最多 8 位）")
    ap.add_argument("--tier", default=lic.TIER_PRO, choices=[lic.TIER_FREE, lic.TIER_PRO])
    ap.add_argument("--log", default="", help="把签发记录追加到这个 TSV")
    ap.add_argument("--inspect", default="", help="解包一个已有的码，不解签")
    a = ap.parse_args()

    if a.inspect:
        try:
            raw = lic._b32_dec(a.inspect)
        except Exception as e:
            print(f"解不出来：{e}", file=sys.stderr)
            return 2
        if len(raw) < lic._PAYLOAD_LEN:
            print("太短，不成码", file=sys.stderr)
            return 2
        info = lic._unpack(raw[:lic._PAYLOAD_LEN])
        sig_ok = bool(lic._verify(a.inspect))
        if info:
            info["expires"] = (time.strftime("%Y-%m-%d", time.localtime(info["expires_at"]))
                               if info["expires_at"] else "永久")
        print(json.dumps({"payload": info, "signature_ok": sig_ok},
                         ensure_ascii=False, indent=2))
        return 0

    if not os.path.exists(a.key):
        print(f"找不到私钥 {a.key}\n先跑 tools/gen_keys.py 生成一对。", file=sys.stderr)
        return 2

    code = make(a.key, a.tier, a.days, a.serial)
    print(code)
    print(file=sys.stderr)
    print(f"档位={a.tier}  有效={'永久' if not a.days else str(a.days) + ' 天'}"
          f"  长度={len(code)}", file=sys.stderr)

    # ★ 把码里**真正**那个序列号也解出来落账。
    #   码里那 6 字节是随机生成的，`--serial` 只是你自己写给人看的备注 ——
    #   两个都得记：将来有码流出去，`--inspect` 只能解出码里那个随机序列号，
    #   光有备注列就对不上人。这一列就是用来「拿码找人」的。
    import base64 as _b64
    body = code.replace("FXK1-", "").replace("-", "")
    raw = _b64.b32decode(body + "=" * (-len(body) % 8))
    embedded = _b64.b32encode(raw[6:12]).decode().rstrip("=")

    if a.log:
        os.makedirs(os.path.dirname(os.path.abspath(a.log)), exist_ok=True)
        new = not os.path.exists(a.log)
        with open(a.log, "a", encoding="utf-8") as f:
            if new:
                f.write("issued_at\tlabel\tembedded_serial\ttier\tdays\tcode\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{a.serial or '(none)'}\t"
                    f"{embedded}\t{a.tier}\t{a.days or 'perp'}\t{code}\n")
        print(f"已记一笔到 {a.log}  （码内序列号 {embedded}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
